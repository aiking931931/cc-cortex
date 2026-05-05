"""Integration tests for sub-agent K wave-2 ZIQ outcome wires.

Plan v1 line 60 — 9 of 12 wires landed (3 skipped: gaia.meta_arm has
no select_arm consumer; judge.arm has no module; escalation.enable_few_shot
not present in escalation.py). Each test exercises the production
callsite and asserts the bus saw the expected outcome.
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


# ── 1. knowledge.ftrl_threshold ─────────────────────────────────────


def test_knowledge_ftrl_threshold_emits(tmp_path: Path) -> None:
    import json

    from concinno.knowledge import get_pending_promotions

    seen: list[Outcome] = []
    get_bus().subscribe("knowledge.ftrl_threshold", seen.append)

    learnings = tmp_path / "learnings.json"
    # Recent + count high enough that ftrl_weight clears 5.0.
    learnings.write_text(
        json.dumps(
            {
                "learnings": [
                    {
                        "id": "x",
                        "count": 100,
                        "last_seen": "2026-04-28T00:00:00+00:00",
                        "promoted": False,
                        "correction_text": "x",
                        "pattern_key": "p1",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    out = get_pending_promotions(
        str(learnings), use_ftrl=True, ftrl_threshold=5.0
    )
    assert isinstance(out, list)
    assert len(seen) >= 1
    assert seen[-1].metadata.get("mode") == "ftrl"


# ── 2. knowledge.pattern_threshold ──────────────────────────────────


def test_knowledge_pattern_threshold_emits(tmp_path: Path) -> None:
    import json

    from concinno.knowledge import detect_skill_candidates

    seen: list[Outcome] = []
    get_bus().subscribe("knowledge.pattern_threshold", seen.append)

    learnings = tmp_path / "l.json"
    learnings.write_text(
        json.dumps(
            {
                "learnings": [
                    {"pattern_key": "pk1", "count": 5, "correction_text": "a"},
                    {"pattern_key": "pk1", "count": 5, "correction_text": "b"},
                ]
            }
        ),
        encoding="utf-8",
    )
    drive_state = tmp_path / "drive.json"
    drive_state.write_text("{}", encoding="utf-8")

    detect_skill_candidates(
        str(learnings), str(drive_state), pattern_threshold=3
    )
    assert any(
        s.tunable == "knowledge.pattern_threshold" for s in seen
    )
    assert "candidates_added" in seen[-1].metadata


# ── 3. fewshot.min_token_len ────────────────────────────────────────


def test_fewshot_min_token_len_emits() -> None:
    from concinno.fewshot import FewshotBank, FewshotCase

    seen: list[Outcome] = []
    get_bus().subscribe("fewshot.min_token_len", seen.append)
    bank = FewshotBank(
        cases=[
            FewshotCase(
                id="c1",
                description="alpha beta gamma",
                response="r1",
            )
        ],
        min_token_len=3,
    )
    bank.retrieve("alpha")
    assert any(s.tunable == "fewshot.min_token_len" for s in seen)
    assert "top_score" in seen[-1].metadata


# ── 4. parallel_dispatch.max_fork_depth ─────────────────────────────


def test_parallel_dispatch_fork_depth_emits_on_success() -> None:
    """Successful fork emits succeeded=True outcome."""
    from concinno.agent.fork_context import (
        FileStateCache,
        SubagentContext,
        create_cache_safe_params,
    )
    from concinno.agent.parallel_dispatch import (
        ParallelDispatcher,
        SpawnRequest,
    )

    seen: list[Outcome] = []
    get_bus().subscribe("parallel_dispatch.max_fork_depth", seen.append)

    base = create_cache_safe_params(
        system_prompt="sys",
        tool_defs=[{"name": "Read"}],
        parent_session_id="sess",
        fork_depth=0,
    )
    parent = SubagentContext(
        params=base,
        file_state=FileStateCache(),
        parent_transcript_ref="ref",
    )

    disp = ParallelDispatcher(
        parent_context=parent, max_fork_depth=5
    )
    req = SpawnRequest(prompt="run x", subagent_type=None)
    plan = disp.plan(req)
    assert plan.path == "fork"
    assert any(s.metadata.get("outcome") == "forked" for s in seen)


def test_parallel_dispatch_fork_depth_emits_on_exhaustion() -> None:
    """Hitting the cap (parent depth >= max_fork_depth) emits failure."""
    from concinno.agent.fork_context import (
        FileStateCache,
        ForkDepthExceeded,
        SubagentContext,
        create_cache_safe_params,
    )
    from concinno.agent.parallel_dispatch import (
        ParallelDispatcher,
        SpawnRequest,
    )

    seen: list[Outcome] = []
    get_bus().subscribe("parallel_dispatch.max_fork_depth", seen.append)

    # Parent at depth=0 (so fork-in-fork check passes); cap=0 so any
    # fork attempt immediately exceeds the cap.
    base = create_cache_safe_params(
        system_prompt="sys",
        tool_defs=[{"name": "Read"}],
        parent_session_id="sess",
        fork_depth=0,
    )
    parent = SubagentContext(
        params=base,
        file_state=FileStateCache(),
        parent_transcript_ref="ref",
    )
    disp = ParallelDispatcher(parent_context=parent, max_fork_depth=0)
    req = SpawnRequest(prompt="overflow", subagent_type=None)
    with pytest.raises(ForkDepthExceeded):
        disp.plan(req)
    assert any(
        s.metadata.get("outcome") == "fork_depth_exceeded" for s in seen
    )


# ── 5. reflexion.max_words ──────────────────────────────────────────


def test_reflexion_max_words_emits_on_post_tool() -> None:
    from concinno.guards.base import GuardContext
    from concinno.guards.reflexion_guard import ReflexionGuard

    seen: list[Outcome] = []
    get_bus().subscribe("reflexion.max_words", seen.append)

    guard = ReflexionGuard(max_words=80, injection_ttl_calls=2)
    ctx = GuardContext(
        hook_event="PostToolUse",
        tool_name="Edit",
        tool_input={"file_path": "/tmp/x.py"},
        tool_result="String not found in file",
        cache_dir=str(__import__("tempfile").mkdtemp()),
        session_id="sess1",
    )
    guard.on_post_tool(ctx)
    assert any(s.tunable == "reflexion.max_words" for s in seen)


# ── 6. reflexion.injection_ttl_calls ────────────────────────────────


def test_reflexion_ttl_emits_on_replay() -> None:
    import tempfile

    from concinno.core.state_store import StateStore
    from concinno.guards.base import GuardContext
    from concinno.guards.reflexion_guard import ReflexionGuard

    seen: list[Outcome] = []
    get_bus().subscribe("reflexion.injection_ttl_calls", seen.append)

    cache_dir = tempfile.mkdtemp()
    sid = "sess_ttl"
    store = StateStore(cache_dir)
    store.write(
        "reflexion",
        sid,
        {"why_failed": "Reflexion: prior fail", "ttl_remaining": 2},
    )

    guard = ReflexionGuard(max_words=80, injection_ttl_calls=2)
    ctx = GuardContext(
        hook_event="PreToolUse",
        tool_name="Bash",
        tool_input={"command": "ls"},
        cache_dir=cache_dir,
        session_id=sid,
    )
    result = guard.check(ctx)
    assert result is not None
    assert any(
        s.tunable == "reflexion.injection_ttl_calls" for s in seen
    )


# ── 7+8. tot.max_branches + tot.convergence_pct ─────────────────────


def test_tot_emits_both_tunables() -> None:
    from concinno.cognitive.router import ComplexityDomain
    from concinno.cognitive.tot_branch_explorer import plan_branches

    seen_branches: list[Outcome] = []
    seen_conv: list[Outcome] = []
    get_bus().subscribe("tot.max_branches", seen_branches.append)
    get_bus().subscribe("tot.convergence_pct", seen_conv.append)

    plan_branches(
        complexity=ComplexityDomain.COMPLEX,
        budget_consumed_pct=0.2,
        max_branches=3,
        convergence_pct=0.5,
    )
    assert any(
        s.tunable == "tot.max_branches" for s in seen_branches
    )
    assert any(
        s.tunable == "tot.convergence_pct" for s in seen_conv
    )


def test_tot_convergence_path_emits() -> None:
    from concinno.cognitive.router import ComplexityDomain
    from concinno.cognitive.tot_branch_explorer import plan_branches

    seen: list[Outcome] = []
    get_bus().subscribe("tot.max_branches", seen.append)

    plan_branches(
        complexity=ComplexityDomain.CHAOTIC,
        budget_consumed_pct=0.7,
        max_branches=3,
        convergence_pct=0.5,
    )
    assert any(s.metadata.get("converged") is True for s in seen)


# ── 9. action_phase.summary_interval ────────────────────────────────


def test_action_phase_summary_interval_emits() -> None:
    import tempfile

    from concinno.guards.action_phase_signal_guard import (
        ActionPhaseSignalGuard,
    )
    from concinno.guards.base import GuardContext

    seen: list[Outcome] = []
    get_bus().subscribe("action_phase.summary_interval", seen.append)

    cache_dir = tempfile.mkdtemp()
    sid = "sess_action"
    guard = ActionPhaseSignalGuard(summary_interval=5)
    # Drive 5 calls to hit the interval.
    for _ in range(5):
        ctx = GuardContext(
            hook_event="PostToolUse",
            tool_name="Edit",
            tool_input={"file_path": "/tmp/x.py"},
            cache_dir=cache_dir,
            session_id=sid,
        )
        guard.on_post_tool(ctx)
    assert any(
        s.tunable == "action_phase.summary_interval" for s in seen
    )
