"""Tests for concinno.cognitive.review_router.

The router blends a hand-coded SPS structural prior with the existing
ZIQ FTRL outcome-learning stack. These tests cover:

* SPS prior routing for the documented signal patterns.
* Cold-start vs FTRL takeover threshold behaviour.
* JSONL audit + ZIQ tuner integration.
* Meta-MAR ground-truth sampling on every Mth Chaotic decision.
* Cost-adjusted reward arithmetic.
* Lazy-import contract with the sibling RBG dispatch guard.
* Inline 4-perspective MAR dispatching 4 redteam-role spawns.
* Feature flag short-circuit.
* ``RoutingDecision.chosen_reason`` explainability.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Redirect every persistent path the router touches into ``tmp_path``.

    Each test gets a fresh:
    * ``CONCINNO_REVIEW_ROUTER_OUTCOME_DIR`` for the JSONL audit log.
    * ``CONCINNO_ZIQ_TUNER_DIR`` for the FTRL persistence.
    * ``CONCINNO_REDTEAM_LEDGER_DIR`` for the spawn ledger.
    * ``HOME`` so any fallback path lands inside tmp.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv(
        "CONCINNO_REVIEW_ROUTER_OUTCOME_DIR",
        str(tmp_path / "outcomes"),
    )
    monkeypatch.setenv(
        "CONCINNO_ZIQ_TUNER_DIR",
        str(tmp_path / "ziq_tuners"),
    )
    monkeypatch.setenv("CONCINNO_ZIQ_AUTOTUNE", "1")

    # Reset ZIQ tuner cache between tests so the router's tuner is fresh.
    from concinno import ziq_autotune_registry

    ziq_autotune_registry.clear_cache()

    yield tmp_path

    ziq_autotune_registry.clear_cache()


@pytest.fixture
def router():
    """Return a fresh ReviewRouter (re-import friendly)."""
    from concinno.cognitive import review_router as rr_mod

    importlib.reload(rr_mod)
    return rr_mod.ReviewRouter()


def _make_signal(**overrides):
    """Build a TaskSignal with low-noise defaults, overrideable per test."""
    from concinno.cognitive.review_router import TaskSignal

    base = {
        "irreversible": False,
        "pre_action": True,
        "radius": "medium",
        "ship_gate": False,
        "open_exploration": False,
        "time_pressed": False,
        "single_claim": False,
    }
    base.update(overrides)
    return TaskSignal(**base)


def _fake_dispatcher() -> MagicMock:
    """A dispatcher that returns a stable response."""
    mock = MagicMock()
    mock.dispatch.return_value = '{"summary": "ok", "findings": []}'
    return mock


# ── Routing tests ──────────────────────────────────────────────────


def test_irreversible_pre_action_routes_to_rbg(router) -> None:
    from concinno.cognitive.review_router import ReviewMethod

    signal = _make_signal(irreversible=True, pre_action=True, single_claim=True)
    decision = router.route(signal)

    assert decision.method == ReviewMethod.REDBLUE_GREEN_ONLY
    assert "irreversible_pre_action" in decision.chosen_reason


def test_post_failure_exploration_routes_to_mar(router) -> None:
    from concinno.cognitive.review_router import ReviewMethod

    signal = _make_signal(
        pre_action=False,
        open_exploration=True,
        single_claim=False,
    )
    decision = router.route(signal)
    assert decision.method == ReviewMethod.MAR_ONLY


def test_chaotic_ship_gate_routes_to_parallel_both(router) -> None:
    from concinno.cognitive.review_router import ReviewMethod

    signal = _make_signal(radius="chaotic", ship_gate=True, single_claim=False)
    decision = router.route(signal)
    assert decision.method == ReviewMethod.PARALLEL_BOTH


def test_time_pressed_routes_to_lightest(router) -> None:
    from concinno.cognitive.review_router import ReviewMethod

    # Nothing else screams "irreversible"; time_pressed should bias to MAR (cheapest).
    signal = _make_signal(time_pressed=True, single_claim=False)
    decision = router.route(signal)
    assert decision.method == ReviewMethod.MAR_ONLY


def test_cold_start_uses_sps_prior_only(router) -> None:
    from concinno.cognitive.review_router import ReviewMethod

    signal = _make_signal(irreversible=True, pre_action=True, single_claim=True)
    decision = router.route(signal)

    # No FTRL data yet → posterior all zero, cost_adjusted=False.
    assert all(v == 0.0 for v in decision.ftrl_posterior_score.values())
    assert decision.cost_adjusted is False
    # SPS prior alone picks RBG.
    assert decision.method == ReviewMethod.REDBLUE_GREEN_ONLY


def test_ftrl_takes_over_after_threshold(router) -> None:
    """After ftrl_takeover_after_n_samples (30 default) FTRL posterior is read."""
    from concinno.cognitive.review_router import ReviewMethod

    signal = _make_signal(time_pressed=True, single_claim=False)

    # Synthesise 35 outcome records to push FTRL above the 30-sample threshold.
    for _ in range(35):
        router.record_outcome(
            method=ReviewMethod.MAR_ONLY,
            signal=signal,
            outcome=1.0,
            token_cost=500,
        )

    decision = router.route(signal)
    assert decision.cost_adjusted is True
    # FTRL posterior should be non-zero for at least the rewarded arm.
    assert decision.ftrl_posterior_score[ReviewMethod.MAR_ONLY] > 0.0


def test_record_outcome_writes_jsonl_and_updates_ftrl(
    router,
    _isolated_env: Path,
) -> None:
    from concinno.cognitive.review_router import ReviewMethod

    signal = _make_signal(time_pressed=True)
    router.record_outcome(
        method=ReviewMethod.MAR_ONLY,
        signal=signal,
        outcome=1.0,
        token_cost=2000,
    )

    audit_dir = _isolated_env / "outcomes"
    assert audit_dir.exists()
    files = list(audit_dir.glob("*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["method"] == "mar_only"
    assert record["outcome_raw"] == 1.0
    # cost-adjusted = 1.0 / max(1, 2000/1000) = 0.5
    assert record["outcome_adjusted"] == pytest.approx(0.5)

    # The ZIQ tuner has at least one observation now.
    from concinno.ziq_autotune_registry import get_tuner

    tuner = get_tuner("review_method.route")
    assert tuner.n >= 1


def test_meta_mar_every_n_chaotic(router) -> None:
    """Every 10th chaotic decision triggers parallel_both ground-truth."""
    from concinno.cognitive.review_router import ReviewMethod

    signal = _make_signal(
        irreversible=True,
        pre_action=True,
        radius="chaotic",
        single_claim=True,  # → would route to RBG, NOT parallel_both
    )

    dispatcher = _fake_dispatcher()
    rbg_module = pytest.importorskip(
        "concinno.guards.redblue_green_dispatch_guard",
    )
    # Make RBG return a stable verdict so we can count calls.
    dispatcher.dispatch.return_value = (
        '{"summary": "ok", "findings": [], "verdict": "accept"}'
    )

    # Run 10 chaotic decisions; the 10th should ALSO trigger PARALLEL_BOTH.
    for _ in range(9):
        # Force decision != parallel_both (the SPS prior already does so).
        sps_decision = router.route(signal)
        assert sps_decision.method != ReviewMethod.PARALLEL_BOTH

    # Check counter behaviour by inspecting internal state.
    assert router._chaotic_decision_count == 0  # route() doesn't increment

    # Use execute() to drive the counter; we patch _dispatch to track meta calls.
    captured: list[bool] = []
    original_dispatch = router._dispatch

    def _spy(method, sig, ctx, disp, *, meta_mar=False):
        captured.append(meta_mar)
        # Return a stub instead of really invoking RBG.
        return {"stub": True, "method": method.value, "meta_mar": meta_mar}

    router._dispatch = _spy  # type: ignore[assignment]

    for _ in range(10):
        router.execute(signal, decision_context="ctx", dispatcher=dispatcher)

    router._dispatch = original_dispatch  # type: ignore[assignment]

    # Among the 10 executions the 10th one should have ALSO scheduled a
    # meta_mar=True dispatch (so we expect exactly one True flag).
    assert captured.count(True) == 1
    # Sanity: rbg_module exists, even if we did not really dispatch through it.
    assert rbg_module is not None


def test_cost_adjusted_reward(router, _isolated_env: Path) -> None:
    from concinno.cognitive.review_router import ReviewMethod

    signal = _make_signal(time_pressed=True)
    # Same outcome but 5× cost should halve+ the reward.
    router.record_outcome(
        method=ReviewMethod.MAR_ONLY,
        signal=signal,
        outcome=1.0,
        token_cost=1000,
    )
    router.record_outcome(
        method=ReviewMethod.MAR_ONLY,
        signal=signal,
        outcome=1.0,
        token_cost=5000,
    )

    files = list((_isolated_env / "outcomes").glob("*.jsonl"))
    assert len(files) == 1
    records = [
        json.loads(line)
        for line in files[0].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    cheap, expensive = records
    assert cheap["outcome_adjusted"] == pytest.approx(1.0)
    assert expensive["outcome_adjusted"] == pytest.approx(1.0 / 5.0)


def test_redblue_green_arm_skipped_when_module_missing(
    monkeypatch: pytest.MonkeyPatch,
    router,
) -> None:
    """Even with the module present, ImportError → NotImplementedError."""
    from concinno.cognitive import review_router as rr_mod

    pytest.importorskip("concinno.guards.redblue_green_dispatch_guard")

    # Force an ImportError inside _dispatch_redblue_green by injecting a
    # broken module entry. We restore on teardown.
    import sys

    sentinel = object()
    rbg_name = "concinno.guards.redblue_green_dispatch_guard"
    saved = sys.modules.get(rbg_name, sentinel)
    sys.modules[rbg_name] = None  # type: ignore[assignment]
    try:
        signal = _make_signal(
            irreversible=True,
            pre_action=True,
            single_claim=True,
        )
        with pytest.raises(NotImplementedError, match="S1' module not landed"):
            router._dispatch_redblue_green(
                signal,
                "ctx",
                _fake_dispatcher(),
            )
    finally:
        if saved is sentinel:
            sys.modules.pop(rbg_name, None)
        else:
            sys.modules[rbg_name] = saved  # type: ignore[assignment]
        importlib.reload(rr_mod)


def test_mar_4perspective_inline_dispatches_4_redteam_roles(
    router,
    monkeypatch: pytest.MonkeyPatch,
    _isolated_env: Path,
) -> None:
    """The inline MAR fan-out fires exactly 4 dispatcher.dispatch calls."""
    from concinno.cognitive.review_router import ReviewMethod
    from concinno.redteam_spawn_guard import reset_ledger

    reset_ledger(cache_dir=str(_isolated_env / "ledger"))
    monkeypatch.setenv("CONCINNO_REDTEAM_MAX_SPAWNS_PER_EVENT", "20")

    dispatcher = _fake_dispatcher()
    signal = _make_signal(
        pre_action=False,
        open_exploration=True,
        single_claim=False,
    )

    # Route lands on MAR_ONLY (via SPS).
    decision = router.route(signal)
    assert decision.method == ReviewMethod.MAR_ONLY

    # Drive the inline fan-out directly.
    results = router._dispatch_mar_4perspective(
        signal,
        "decision_context",
        dispatcher,
    )
    assert set(results.keys()) == {"engineer", "user", "attacker", "auditor"}

    # 4 dispatch calls, all role="redteam".
    assert dispatcher.dispatch.call_count == 4
    for call in dispatcher.dispatch.call_args_list:
        assert call.kwargs.get("role") == "redteam"
        assert call.kwargs.get("model") == "opus"


def test_feature_disabled_short_circuits(
    router,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the feature flag is off, execute() bypasses routing entirely."""
    from concinno.cognitive import review_router as rr_mod
    from concinno.cognitive.review_router import ReviewMethod

    monkeypatch.setattr(rr_mod, "_feature_enabled", lambda: False)

    captured: list[ReviewMethod] = []

    def _spy(method, sig, ctx, disp, *, meta_mar=False):
        captured.append(method)
        return {"stub": True}

    router._dispatch = _spy  # type: ignore[assignment]

    signal = _make_signal(
        irreversible=True,
        pre_action=True,
        radius="chaotic",
        single_claim=True,
    )
    router.execute(signal, decision_context="ctx", dispatcher=_fake_dispatcher())

    # Even though SPS would route to RBG, the disabled path falls back to MAR_ONLY.
    assert captured == [ReviewMethod.MAR_ONLY]


def test_ziq_register_round_trip(router) -> None:
    """The review_method.route arm is registered with the ZIQ stack."""
    from concinno.ziq_autotune_registry import describe, list_targets

    targets = list_targets()
    assert "review_method.route" in targets

    spec = describe("review_method.route")
    assert spec.kind == "discrete"
    assert spec.choices is not None
    expected_choices = (
        "mar_only",
        "redblue_green_only",
        "mar_first_then_rbg",
        "rbg_first_then_mar",
        "parallel_both",
    )
    assert set(spec.choices) == set(expected_choices)


def test_routing_decision_explainability(router) -> None:
    """RoutingDecision.chosen_reason is non-empty + names the dominant pattern."""
    signal = _make_signal(irreversible=True, pre_action=True, single_claim=True)
    decision = router.route(signal)
    assert decision.chosen_reason
    assert "irreversible_pre_action" in decision.chosen_reason
    assert decision.method.value in decision.chosen_reason


def test_no_signal_falls_back_to_mar(router) -> None:
    """A signal with zero matched patterns lands on MAR_ONLY (cheapest safe)."""
    # Construct a signal that matches NOTHING in SPS_PRIOR. The
    # ``multi_claim_survey`` pattern fires when single_claim=False — so
    # we set single_claim=True to suppress it, and disable every other
    # signal. Result: only ``single_claim_verification`` matches → RBG.
    # To get the truly-no-match case we also disable that branch by
    # making the signal route from the empty match table directly.
    from concinno.cognitive import review_router as rr_mod
    from concinno.cognitive.review_router import ReviewMethod

    # Inject a no-pattern signal by monkey-patching matched_patterns.
    original = rr_mod._matched_patterns
    rr_mod._matched_patterns = lambda signal: []  # type: ignore[assignment]
    try:
        signal = _make_signal(single_claim=True)
        decision = router.route(signal)
        assert decision.method == ReviewMethod.MAR_ONLY
        assert "no_pattern" in decision.chosen_reason
    finally:
        rr_mod._matched_patterns = original  # type: ignore[assignment]
