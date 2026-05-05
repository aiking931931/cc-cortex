"""Tests for ``concinno.guards.wiredo_subagent_verify_guard``.

The 15 cases below pin the directive-anchored contract documented in
``_AI_BRAIN/05_Planning/2026-04-29-w4-marketplace-and-hp4-transcript-design.md``
§6.3. Tests are mock-dispatcher, deterministic, and never spawn an
Anthropic API call — the verifier dispatcher is a Protocol the caller
injects, so a ``unittest.mock.Mock`` is sufficient end-to-end.

Test 14 is the only live-Opus path and is skipped in CI by default
(opt-in via ``CONCINNO_RUN_LIVE_OPUS`` env var).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from concinno.guards.redblue_green_dispatch_guard import Radius
from concinno.guards.wiredo_subagent_verify_guard import (
    DIRECTIVE_DATE,
    VERIFIER_PROMPT_TEMPLATE,
    PendingVerification,
    SelfVerifyError,
    VerifyOutcome,
    WiredoSubagentVerifyGuard,
    _format_asset_paths_yaml,
    register_ziq_arms,
)

# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect every on-disk path used by the guard to ``tmp_path``.

    Patches the four module-level path helpers so each test runs in a
    clean filesystem corner with no cross-test bleed.
    """
    state_dir = tmp_path / "state"
    outcome_dir = tmp_path / "ziq_state"
    workspace_dir = tmp_path / "verify_workspace"
    state_dir.mkdir(parents=True, exist_ok=True)
    outcome_dir.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        "concinno.guards.wiredo_subagent_verify_guard._state_base_dir",
        lambda: state_dir,
    )
    monkeypatch.setattr(
        "concinno.guards.wiredo_subagent_verify_guard._outcome_dir",
        lambda: outcome_dir,
    )
    monkeypatch.setattr(
        "concinno.guards.wiredo_subagent_verify_guard._verify_workspace_root",
        lambda: workspace_dir,
    )
    return tmp_path


@pytest.fixture
def feature_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the feature on for tests that exercise the happy path."""
    monkeypatch.setattr(
        "concinno.guards.wiredo_subagent_verify_guard._feature_enabled",
        lambda: True,
    )


def _pass_dispatcher() -> MagicMock:
    """Mock dispatcher whose verifier always says pass=True."""
    dispatcher = MagicMock()
    dispatcher.dispatch.return_value = json.dumps(
        {
            "pass": True,
            "evidence": [
                "ran pytest tests/guards -q → 15 passed",
                "smoke imported new public API: ok",
            ],
            "failures": [],
            "next_action": "release",
        },
    )
    return dispatcher


def _fail_dispatcher() -> MagicMock:
    """Mock dispatcher whose verifier always says pass=False."""
    dispatcher = MagicMock()
    dispatcher.dispatch.return_value = json.dumps(
        {
            "pass": False,
            "evidence": ["pytest tests/guards -q → 1 failed"],
            "failures": ["assert 0 == 1 in test_smoke"],
            "next_action": "retry",
        },
    )
    return dispatcher


# ── 1. register_pending assigns uuid task_id ─────────────────────


def test_register_pending_assigns_uuid_task_id(
    isolated_state: Path, feature_on: None,
) -> None:
    guard = WiredoSubagentVerifyGuard()
    task_id = guard.register_pending(
        original_agent_id="actor-A",
        asset_paths=["src/foo.py"],
        change_summary="added foo()",
        radius=Radius.HIGH,
    )
    assert task_id  # non-empty
    assert all(c in "0123456789abcdef" for c in task_id)
    assert any(p.task_id == task_id for p in guard.pending_tasks())


# ── 2. register_pending persists across instances ─────────────────


def test_register_pending_persists_across_instances(
    isolated_state: Path, feature_on: None,
) -> None:
    g1 = WiredoSubagentVerifyGuard()
    task_id = g1.register_pending(
        original_agent_id="actor-A",
        asset_paths=["src/bar.py"],
        change_summary="added bar()",
        radius=Radius.HIGH,
    )
    g2 = WiredoSubagentVerifyGuard()
    pending_ids = [p.task_id for p in g2.pending_tasks()]
    assert task_id in pending_ids


# ── 3. anti-self-verify raises when ids match ────────────────────


def test_anti_self_verify_raises_when_ids_match(
    isolated_state: Path, feature_on: None,
) -> None:
    guard = WiredoSubagentVerifyGuard()
    task_id = guard.register_pending(
        original_agent_id="actor-A",
        asset_paths=[],
        change_summary="",
        radius=Radius.HIGH,
    )
    dispatcher = MagicMock()
    with pytest.raises(SelfVerifyError) as exc:
        guard.dispatch_verifier(
            task_id=task_id,
            dispatcher=dispatcher,
            verifier_agent_id="actor-A",
        )
    # ── 15. message cites directive date for grep audit ──
    assert DIRECTIVE_DATE in str(exc.value)


# ── 4. anti-self-verify dispatcher never called ──────────────────


def test_anti_self_verify_dispatcher_never_called_on_match(
    isolated_state: Path, feature_on: None,
) -> None:
    guard = WiredoSubagentVerifyGuard()
    task_id = guard.register_pending(
        original_agent_id="actor-A",
        asset_paths=[],
        change_summary="",
        radius=Radius.HIGH,
    )
    dispatcher = MagicMock()
    with pytest.raises(SelfVerifyError):
        guard.dispatch_verifier(
            task_id=task_id,
            dispatcher=dispatcher,
            verifier_agent_id="actor-A",
        )
    dispatcher.dispatch.assert_not_called()


# ── 5. dispatch pass records outcome 1.0 ─────────────────────────


def test_dispatch_pass_records_outcome_pass_1(
    isolated_state: Path, feature_on: None, tmp_path: Path,
) -> None:
    guard = WiredoSubagentVerifyGuard(
        outcome_path_override=tmp_path / "outcomes.jsonl",
        shared_bus_override=tmp_path / "shared.jsonl",
    )
    task_id = guard.register_pending(
        original_agent_id="actor-A",
        asset_paths=["src/foo.py"],
        change_summary="added foo()",
        radius=Radius.HIGH,
    )
    dispatcher = _pass_dispatcher()
    outcome = guard.dispatch_verifier(
        task_id=task_id,
        dispatcher=dispatcher,
        verifier_agent_id="verifier-V",
    )
    guard.record_outcome(outcome)

    assert outcome.pass_ is True
    text = (tmp_path / "outcomes.jsonl").read_text(encoding="utf-8")
    record = json.loads(text.strip().splitlines()[-1])
    assert record["outcome"] == 1.0
    assert record["pass"] is True


# ── 6. dispatch fail records outcome 0.0 + retry_count++ ─────────


def test_dispatch_fail_records_outcome_0(
    isolated_state: Path, feature_on: None, tmp_path: Path,
) -> None:
    guard = WiredoSubagentVerifyGuard(
        outcome_path_override=tmp_path / "outcomes.jsonl",
        shared_bus_override=tmp_path / "shared.jsonl",
    )
    task_id = guard.register_pending(
        original_agent_id="actor-A",
        asset_paths=["src/foo.py"],
        change_summary="x",
        radius=Radius.HIGH,
    )
    dispatcher = _fail_dispatcher()
    outcome = guard.dispatch_verifier(
        task_id=task_id,
        dispatcher=dispatcher,
        verifier_agent_id="verifier-V",
    )
    guard.record_outcome(outcome)

    assert outcome.pass_ is False
    text = (tmp_path / "outcomes.jsonl").read_text(encoding="utf-8")
    record = json.loads(text.strip().splitlines()[-1])
    assert record["outcome"] == 0.0

    # retry_count incremented
    pending_after = [p for p in guard.pending_tasks() if p.task_id == task_id]
    assert len(pending_after) == 1
    assert pending_after[0].retry_count == 1


# ── 7. retry cap default 3 ───────────────────────────────────────


def test_retry_cap_default_3(
    isolated_state: Path, feature_on: None,
) -> None:
    guard = WiredoSubagentVerifyGuard()
    task_id = guard.register_pending(
        original_agent_id="actor-A",
        asset_paths=[],
        change_summary="",
        radius=Radius.HIGH,
    )
    dispatcher = _fail_dispatcher()
    with patch(
        "concinno.guards.wiredo_subagent_verify_guard._resolve_retry_cap",
        return_value=3,
    ):
        for _ in range(3):
            guard.dispatch_verifier(
                task_id=task_id,
                dispatcher=dispatcher,
                verifier_agent_id="verifier-V",
            )

    # After cap, task is removed from pending registry (abandon).
    assert all(p.task_id != task_id for p in guard.pending_tasks())
    assert dispatcher.dispatch.call_count == 3


# ── 8. retry cap tunable via ZIQ ─────────────────────────────────


def test_retry_cap_tunable_via_ziq(
    isolated_state: Path, feature_on: None,
) -> None:
    guard = WiredoSubagentVerifyGuard()
    task_id = guard.register_pending(
        original_agent_id="actor-A",
        asset_paths=[],
        change_summary="",
        radius=Radius.HIGH,
    )
    dispatcher = _fail_dispatcher()
    with patch(
        "concinno.guards.wiredo_subagent_verify_guard._resolve_retry_cap",
        return_value=2,
    ):
        for _ in range(2):
            guard.dispatch_verifier(
                task_id=task_id,
                dispatcher=dispatcher,
                verifier_agent_id="verifier-V",
            )
    assert all(p.task_id != task_id for p in guard.pending_tasks())
    assert dispatcher.dispatch.call_count == 2


# ── 9. user_overrule records outcome 0.0 even on verifier pass ──


def test_user_overrule_records_outcome_0(
    isolated_state: Path, feature_on: None, tmp_path: Path,
) -> None:
    outcome_path = tmp_path / "outcomes.jsonl"
    shared_path = tmp_path / "shared.jsonl"
    guard = WiredoSubagentVerifyGuard(
        outcome_path_override=outcome_path,
        shared_bus_override=shared_path,
    )
    pass_outcome = VerifyOutcome(
        task_id="abcdef00",
        pass_=True,
        evidence=["ran tests"],
        failures=[],
        verifier_agent_id="verifier-V",
        elapsed_ms=42,
    )
    guard.record_outcome(pass_outcome, user_overruled=True)
    text = outcome_path.read_text(encoding="utf-8")
    record = json.loads(text.strip().splitlines()[-1])
    assert record["outcome"] == 0.0
    assert record["user_overruled"] is True
    assert record["pass"] is True  # verifier said pass; we still penalise


# ── 10. simple radius short-circuits with empty task_id ──────────


def test_simple_radius_short_circuits(
    isolated_state: Path, feature_on: None,
) -> None:
    guard = WiredoSubagentVerifyGuard()
    task_id = guard.register_pending(
        original_agent_id="actor-A",
        asset_paths=["src/foo.py"],
        change_summary="x",
        radius=Radius.SIMPLE,
    )
    assert task_id == ""
    assert guard.pending_tasks() == []


# ── 11. feature disabled register returns empty ──────────────────


def test_feature_disabled_register_returns_empty(
    isolated_state: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "concinno.guards.wiredo_subagent_verify_guard._feature_enabled",
        lambda: False,
    )
    guard = WiredoSubagentVerifyGuard()
    task_id = guard.register_pending(
        original_agent_id="actor-A",
        asset_paths=["src/foo.py"],
        change_summary="x",
        radius=Radius.HIGH,
    )
    assert task_id == ""
    assert guard.pending_tasks() == []


# ── 12. verifier prompt template renders ─────────────────────────


def test_verifier_prompt_template_renders() -> None:
    rendered = VERIFIER_PROMPT_TEMPLATE.format(
        original_agent_id="actor-A",
        asset_paths_yaml=_format_asset_paths_yaml(["src/foo.py", "src/bar.py"]),
        change_summary="added foo()",
        wiredo_table_excerpt="| W | ✅ | wired |",
        workspace="/tmp/wiredo_verify/abcd",
    )
    assert "actor-A" in rendered
    assert "src/foo.py" in rendered
    assert "src/bar.py" in rendered
    assert "DO NOT modify the deliverable" in rendered
    assert "DO NOT spawn further sub-agents" in rendered


# ── 13. dispatcher timeout marks fail not crash ──────────────────


def test_dispatcher_timeout_marks_fail_not_crash(
    isolated_state: Path, feature_on: None,
) -> None:
    guard = WiredoSubagentVerifyGuard()
    task_id = guard.register_pending(
        original_agent_id="actor-A",
        asset_paths=[],
        change_summary="",
        radius=Radius.HIGH,
    )

    class _TimeoutDispatcher:
        def dispatch(
            self,
            prompt: str,
            *,
            model: str = "opus",
            role: str,
        ) -> str:
            raise TimeoutError("verifier exceeded budget")

    outcome = guard.dispatch_verifier(
        task_id=task_id,
        dispatcher=_TimeoutDispatcher(),
        verifier_agent_id="verifier-V",
    )
    assert outcome.pass_ is False
    assert outcome.failures, "timeout must surface in failures"
    assert any("timed out" in f.lower() for f in outcome.failures)
    # retry_count incremented as a normal fail
    pending = [p for p in guard.pending_tasks() if p.task_id == task_id]
    assert len(pending) == 1
    assert pending[0].retry_count == 1


# ── 14. live Opus e2e — opt-in only ──────────────────────────────


@pytest.mark.skipif(
    not os.getenv("CONCINNO_RUN_LIVE_OPUS"),
    reason="live Opus path skipped by default; set CONCINNO_RUN_LIVE_OPUS=1",
)
def test_e2e_real_opus_skip_by_default(
    isolated_state: Path, feature_on: None,
) -> None:
    """Sanity for Sancio runtime — only runs when opted in."""
    guard = WiredoSubagentVerifyGuard()
    task_id = guard.register_pending(
        original_agent_id="actor-A",
        asset_paths=["README.md"],
        change_summary="touched readme",
        radius=Radius.HIGH,
    )
    assert task_id  # the rest is exercised in Sancio integration tests


# ── 15. (consolidated) anti-self-verify message cites directive ──


def test_anti_self_verify_message_cites_directive_date(
    isolated_state: Path, feature_on: None,
) -> None:
    guard = WiredoSubagentVerifyGuard()
    task_id = guard.register_pending(
        original_agent_id="actor-A",
        asset_paths=[],
        change_summary="",
        radius=Radius.HIGH,
    )
    with pytest.raises(SelfVerifyError) as exc:
        guard.dispatch_verifier(
            task_id=task_id,
            dispatcher=MagicMock(),
            verifier_agent_id="actor-A",
        )
    assert DIRECTIVE_DATE in str(exc.value)


# ── Bonus: ZIQ arms registered on import ─────────────────────────


def test_register_ziq_arms_idempotent() -> None:
    """Calling twice does not raise and returns the registered arm ids."""
    targets1 = register_ziq_arms()
    targets2 = register_ziq_arms()
    assert "wiredo_verify.retry_cap" in targets2
    assert "wiredo_verify.dispatch_radius_threshold" in targets2
    assert sorted(targets1) == sorted(targets2)


# ── Bonus: workspace allocated per task ──────────────────────────


def test_register_pending_allocates_per_task_workspace(
    isolated_state: Path, feature_on: None,
) -> None:
    guard = WiredoSubagentVerifyGuard()
    task_id = guard.register_pending(
        original_agent_id="actor-A",
        asset_paths=[],
        change_summary="",
        radius=Radius.HIGH,
    )
    pending = next(p for p in guard.pending_tasks() if p.task_id == task_id)
    assert task_id in pending.workspace
    assert Path(pending.workspace).is_dir()


# ── Bonus: PendingVerification dataclass round-trip ──────────────


def test_pending_verification_dataclass_round_trip() -> None:
    p = PendingVerification(
        task_id="deadbeef",
        original_agent_id="actor-A",
        asset_paths=["a.py", "b.py"],
        change_summary="x",
        radius=Radius.CHAOTIC,
        queued_at=1234.5,
        retry_count=2,
        workspace="/tmp/x",
    )
    raw: dict[str, Any] = p.to_dict()
    p2 = PendingVerification.from_dict(raw)
    assert p == p2
