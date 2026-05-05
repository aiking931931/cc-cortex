"""Integration tests for ZIQ outcome bus wiring (plan §60 4.4.0).

Verifies that production guards / autotuned modules emit Outcome events
through the bus when their tunable is exercised. One test per wired
tunable; subscribers count emits and verify reward shape.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from concinno.ziq_outcome_bus import Outcome, ZIQOutcomeBus, get_bus


@pytest.fixture(autouse=True)
def _isolated_bus(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pin_file = tmp_path / "ziq_pinned.json"
    monkeypatch.setenv("CONCINNO_ZIQ_PIN_FILE", str(pin_file))
    monkeypatch.delenv("CONCINNO_ZIQ_BUS_DISABLED", raising=False)
    monkeypatch.delenv("CONCINNO_ZIQ_BUS_MAX_HZ", raising=False)
    ZIQOutcomeBus._reset_for_testing()
    yield
    ZIQOutcomeBus._reset_for_testing()


# ── 1. escalation.circuit_threshold ─────────────────────────────


def test_escalation_circuit_threshold_emits_on_failure(
    tmp_path: Path,
) -> None:
    from concinno.escalation import LLMEscalator

    seen: list[Outcome] = []
    get_bus().subscribe("escalation.circuit_threshold", seen.append)
    esc = LLMEscalator(cache_dir=str(tmp_path / "esc"), circuit_threshold=3)
    # Manually exercise the breaker recorder
    esc._breaker_record_failure("haiku")
    esc._breaker_record_failure("haiku")
    esc._breaker_record_failure("haiku")
    assert any(s.tunable == "escalation.circuit_threshold" for s in seen)
    # Last failure tripped the breaker
    assert seen[-1].metadata["tripped"] is True
    assert seen[-1].metadata["consecutive_failures"] == 3


def test_escalation_circuit_threshold_emits_on_recovery(
    tmp_path: Path,
) -> None:
    from concinno.escalation import LLMEscalator

    seen: list[Outcome] = []
    get_bus().subscribe("escalation.circuit_threshold", seen.append)
    esc = LLMEscalator(cache_dir=str(tmp_path / "esc"), circuit_threshold=5)
    esc._breaker_record_failure("opus")
    esc._breaker_record_failure("opus")
    esc._breaker_record_success("opus")
    # Recovery emit should be present
    recovery = [s for s in seen if "recovered_after" in s.metadata]
    assert len(recovery) == 1
    assert recovery[0].reward == 1.0
    assert recovery[0].metadata["recovered_after"] == 2


# ── 2. delivery.gate.max_iterations ─────────────────────────────


def test_delivery_gate_max_iterations_emits_on_pass() -> None:
    from concinno.delivery._base import (
        ExitCriteria,
        VerificationResult,
    )
    from concinno.delivery.gate import DeliveryGate

    seen: list[Outcome] = []
    get_bus().subscribe("delivery.gate.max_iterations", seen.append)
    gate = DeliveryGate()
    # Build a passing result (empty criteria → all_passed=True)
    criteria = ExitCriteria(task="test", task_id="t1")
    result = VerificationResult(criteria=criteria)
    # all_passed is True when no failures
    assert result.all_passed
    gate.should_retry(result, max_iterations=10, current_iteration=2)
    assert len(seen) == 1
    assert seen[0].metadata["outcome"] == "passed"
    assert seen[0].metadata["current_iteration"] == 2
    # used_ratio = 0.2 → reward = 0.9
    assert seen[0].reward == pytest.approx(0.9)


def test_delivery_gate_max_iterations_emits_on_exhausted() -> None:
    from concinno.delivery._base import (
        Criterion,
        CriterionType,
        ExitCriteria,
        VerificationResult,
    )
    from concinno.delivery.gate import DeliveryGate

    seen: list[Outcome] = []
    get_bus().subscribe("delivery.gate.max_iterations", seen.append)
    gate = DeliveryGate()
    # Failing primary criterion → all_passed=False
    failing = Criterion(
        description="d",
        criterion_type=CriterionType.PRIMARY,
        passed=False,
        evidence="failed",
    )
    criteria = ExitCriteria(
        task="test", task_id="t1", criteria=[failing]
    )
    result = VerificationResult(criteria=criteria)
    assert not result.all_passed
    gate.should_retry(result, max_iterations=5, current_iteration=5)
    # Cap exhausted = reward 0
    exhausted = [s for s in seen if s.metadata.get("outcome") == "exhausted"]
    assert len(exhausted) == 1
    assert exhausted[0].reward == 0.0


# ── 3. consecutive_fail_gate.max_fails ──────────────────────────


def test_consecutive_fail_gate_emits_on_trip(tmp_path: Path) -> None:
    from concinno.core.state_store import StateStore
    from concinno.sentinel import _NS, gate_consecutive_fail

    sid = "session_test_x"
    store_dir = str(tmp_path / "sentinel")
    store = StateStore(store_dir)
    # Seed 4 consecutive failures with no signature → raw fallback path
    calls = [{"ok": False, "tool": "X"} for _ in range(4)]
    store.write(_NS, sid, {"calls": calls})

    seen: list[Outcome] = []
    get_bus().subscribe("consecutive_fail_gate.max_fails", seen.append)
    result = gate_consecutive_fail(sid, store_dir, max_fails=3)
    assert result is not None
    assert any(s.metadata["tripped"] is True for s in seen)


# ── 4. sentinel_gate.max_repeats ────────────────────────────────


def test_sentinel_gate_emits_on_subthreshold(tmp_path: Path) -> None:
    from concinno.core.state_store import StateStore
    from concinno.sentinel import _NS, gate_sentinel

    sid = "session_test_y"
    store_dir = str(tmp_path / "sentinel2")
    store = StateStore(store_dir)
    # Seed 2 consecutive same-file Edits (sub-threshold for max_repeats=5)
    target = str(tmp_path / "f.py")
    calls = [{"tool": "Edit", "path": target} for _ in range(2)]
    store.write(_NS, sid, {"calls": calls})

    seen: list[Outcome] = []
    get_bus().subscribe("sentinel_gate.max_repeats", seen.append)
    result = gate_sentinel(
        sid, "Edit", {"file_path": target}, store_dir, max_repeats=5
    )
    assert result is None  # sub-threshold = no deny
    assert any(s.metadata["tripped"] is False for s in seen)


# ── 5. microcompact.token_budget_soft ───────────────────────────


def test_microcompact_emits_under_soft_budget(tmp_path: Path) -> None:
    """When current tokens are under the soft budget, emit success."""
    from concinno.cache.microcompact import Microcompactor

    seen: list[Outcome] = []
    get_bus().subscribe("microcompact.token_budget_soft", seen.append)

    mc = Microcompactor(
        cache_dir=str(tmp_path / "mc"),
        token_budget_soft=10000,
        token_budget_hard=20000,
    )
    edits = mc.evaluate_token_budget_trigger(current_tokens=5000)
    assert edits == []
    assert any(
        s.tunable == "microcompact.token_budget_soft"
        and not s.metadata.get("overflowed")
        for s in seen
    )


def test_microcompact_emits_on_hard_overflow(tmp_path: Path) -> None:
    from concinno.cache.microcompact import Microcompactor

    seen_soft: list[Outcome] = []
    seen_hard: list[Outcome] = []
    get_bus().subscribe("microcompact.token_budget_soft", seen_soft.append)
    get_bus().subscribe("microcompact.token_budget_hard", seen_hard.append)

    mc = Microcompactor(
        cache_dir=str(tmp_path / "mc2"),
        token_budget_soft=1000,
        token_budget_hard=2000,
    )
    mc.evaluate_token_budget_trigger(current_tokens=3500)
    # Hard overflow → both soft and hard get overflowed=True signal
    assert any(s.metadata.get("overflowed") for s in seen_hard)
    assert any(s.metadata.get("overflowed") for s in seen_soft)


# ── 6. registry sanity: COMPRESS_BREAKEVEN_TOKENS registered ────


def test_compress_breakeven_tunable_in_registry() -> None:
    from concinno.ziq_autotune_registry import (
        TUNABLE_REGISTRY,
        describe,
    )

    assert "field_read.compress_breakeven_tokens" in TUNABLE_REGISTRY
    spec = describe("field_read.compress_breakeven_tokens")
    assert spec.preset == 2500
    assert spec.vmin == 1500.0
    assert spec.vmax == 4000.0
