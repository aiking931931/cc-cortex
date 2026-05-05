"""Tests for concinno.guards.action_phase_signal_guard module."""

from __future__ import annotations

from concinno.core.state_store import StateStore
from concinno.guards.action_phase_signal_guard import (
    ActionPhase,
    ActionPhaseSignalGuard,
    classify_phase,
)
from concinno.guards.base import GuardContext

# ── helpers ──────────────────────────────────────────────


def _ctx(
    *,
    tool_name: str,
    tool_input: dict | None = None,
    session_id: str = "phase-test-session",
    cache_dir: str = "",
    hook_event: str = "PostToolUse",
) -> GuardContext:
    return GuardContext(
        tool_name=tool_name,
        tool_input=tool_input or {},
        session_id=session_id,
        cache_dir=cache_dir,
        hook_event=hook_event,
    )


# ── classify_phase: pure classification ──────────────────


def test_classify_todowrite_is_a1_plan():
    ctx = _ctx(tool_name="TodoWrite")
    assert classify_phase(ctx, {}) == ActionPhase.A1_PLAN


def test_classify_edit_is_a2_execute():
    ctx = _ctx(tool_name="Edit")
    assert classify_phase(ctx, {}) == ActionPhase.A2_EXECUTE


def test_classify_write_is_a2_execute():
    ctx = _ctx(tool_name="Write")
    assert classify_phase(ctx, {}) == ActionPhase.A2_EXECUTE


def test_classify_notebookedit_is_a2_execute():
    ctx = _ctx(tool_name="NotebookEdit")
    assert classify_phase(ctx, {}) == ActionPhase.A2_EXECUTE


def test_classify_read_at_session_start_is_a0_orient():
    ctx = _ctx(tool_name="Read")
    state = {"counts": {}, "bash_count": 0}
    assert classify_phase(ctx, state) == ActionPhase.A0_ORIENT


def test_classify_grep_at_session_start_is_a0_orient():
    ctx = _ctx(tool_name="Grep")
    assert classify_phase(ctx, {}) == ActionPhase.A0_ORIENT


def test_classify_glob_at_session_start_is_a0_orient():
    ctx = _ctx(tool_name="Glob")
    assert classify_phase(ctx, {}) == ActionPhase.A0_ORIENT


def test_classify_read_after_edit_is_unclassified():
    # Once A2 has happened, subsequent reads aren't orient.
    ctx = _ctx(tool_name="Read")
    state = {
        "counts": {ActionPhase.A2_EXECUTE.value: 2},
        "bash_count": 0,
    }
    assert classify_phase(ctx, state) == ActionPhase.UNCLASSIFIED


def test_classify_read_after_three_orients_is_unclassified():
    # Orient window caps at 3 reads.
    ctx = _ctx(tool_name="Read")
    state = {
        "counts": {ActionPhase.A0_ORIENT.value: 3},
        "bash_count": 0,
    }
    assert classify_phase(ctx, state) == ActionPhase.UNCLASSIFIED


# ── classify_phase: Bash subdivision ─────────────────────


def test_classify_bash_pytest_is_a3_verify():
    ctx = _ctx(
        tool_name="Bash",
        tool_input={"command": "pytest tests/foo.py -x"},
    )
    assert classify_phase(ctx, {}) == ActionPhase.A3_VERIFY


def test_classify_bash_ruff_is_a3_verify():
    ctx = _ctx(
        tool_name="Bash",
        tool_input={"command": "ruff check src/"},
    )
    assert classify_phase(ctx, {}) == ActionPhase.A3_VERIFY


def test_classify_bash_go_test_is_a3_verify():
    ctx = _ctx(
        tool_name="Bash",
        tool_input={"command": "go test ./..."},
    )
    assert classify_phase(ctx, {}) == ActionPhase.A3_VERIFY


def test_classify_bash_git_commit_is_a4_adapt():
    ctx = _ctx(
        tool_name="Bash",
        tool_input={"command": "git commit -m 'fix'"},
    )
    assert classify_phase(ctx, {}) == ActionPhase.A4_ADAPT


def test_classify_bash_git_commit_dry_run_is_unclassified():
    # --dry-run is NOT an adapt signal (doesn't actually commit).
    ctx = _ctx(
        tool_name="Bash",
        tool_input={"command": "git commit --dry-run"},
    )
    assert classify_phase(ctx, {}) == ActionPhase.UNCLASSIFIED


def test_classify_bash_unknown_is_unclassified():
    ctx = _ctx(
        tool_name="Bash",
        tool_input={"command": "ls -la"},
    )
    assert classify_phase(ctx, {}) == ActionPhase.UNCLASSIFIED


def test_classify_bash_segment_leading_verify():
    # cd && pytest — verify verb on the SECOND segment.
    ctx = _ctx(
        tool_name="Bash",
        tool_input={"command": "cd src && pytest -x"},
    )
    assert classify_phase(ctx, {}) == ActionPhase.A3_VERIFY


def test_classify_unknown_tool_is_unclassified():
    ctx = _ctx(tool_name="WebFetch")
    assert classify_phase(ctx, {}) == ActionPhase.UNCLASSIFIED


def test_classify_agent_is_unclassified():
    ctx = _ctx(tool_name="Agent")
    assert classify_phase(ctx, {}) == ActionPhase.UNCLASSIFIED


# ── ActionPhaseSignalGuard: PostToolUse counting ─────────


def test_guard_counts_one_call(tmp_path):
    cache = str(tmp_path)
    g = ActionPhaseSignalGuard(summary_interval=10)
    ctx = _ctx(tool_name="Edit", cache_dir=cache)
    res = g.on_post_tool(ctx)
    # Single call, well below summary interval → no advisory.
    assert res is None
    state = StateStore(cache).read("action_phase", ctx.session_id)
    assert state["total"] == 1
    assert state["counts"][ActionPhase.A2_EXECUTE.value] == 1


def test_guard_emits_summary_at_interval(tmp_path):
    cache = str(tmp_path)
    g = ActionPhaseSignalGuard(summary_interval=5)
    ctx = _ctx(tool_name="Edit", cache_dir=cache)

    last = None
    for _ in range(5):
        last = g.on_post_tool(ctx)

    # The 5th call should emit a summary.
    assert last is not None
    assert last.advisory is True
    assert "Action phases" in last.context
    assert "A2=5" in last.context


def test_guard_summary_phrasing_includes_all_phases(tmp_path):
    cache = str(tmp_path)
    # interval=2 would clamp to floor 5 — use 5 explicitly so the
    # summary fires deterministically.
    g = ActionPhaseSignalGuard(summary_interval=5)

    # Mix Edit + TodoWrite + Bash to populate multiple phases.
    g.on_post_tool(_ctx(tool_name="Edit", cache_dir=cache))
    g.on_post_tool(_ctx(tool_name="TodoWrite", cache_dir=cache))
    g.on_post_tool(_ctx(tool_name="Edit", cache_dir=cache))
    g.on_post_tool(_ctx(
        tool_name="Bash",
        tool_input={"command": "pytest tests/x.py"},
        cache_dir=cache,
    ))
    res = g.on_post_tool(_ctx(
        tool_name="Bash",
        tool_input={"command": "git commit -m 'x'"},
        cache_dir=cache,
    ))

    assert res is not None
    msg = res.context
    assert "A0=" in msg
    assert "A1=" in msg
    assert "A2=" in msg
    assert "A3=" in msg
    assert "A4=" in msg


def test_guard_check_is_noop():
    g = ActionPhaseSignalGuard()
    ctx = _ctx(tool_name="Edit", cache_dir="", hook_event="PreToolUse")
    assert g.check(ctx) is None


def test_guard_skips_when_no_session(tmp_path):
    g = ActionPhaseSignalGuard()
    ctx = _ctx(
        tool_name="Edit",
        cache_dir=str(tmp_path),
        session_id="",
    )
    assert g.on_post_tool(ctx) is None


def test_guard_clamps_summary_interval():
    # interval=1 should clamp to 5 (floor).
    g_low = ActionPhaseSignalGuard(summary_interval=1)
    assert g_low._interval == 5  # noqa: SLF001
    # interval=999 should clamp to 30 (ceiling).
    g_high = ActionPhaseSignalGuard(summary_interval=999)
    assert g_high._interval == 30  # noqa: SLF001


def test_guard_orient_window_persists_across_calls(tmp_path):
    cache = str(tmp_path)
    g = ActionPhaseSignalGuard(summary_interval=20)
    ctx_read = _ctx(tool_name="Read", cache_dir=cache)

    # 3 reads at session start — all classified A0.
    for _ in range(3):
        g.on_post_tool(ctx_read)

    state = StateStore(cache).read("action_phase", ctx_read.session_id)
    assert state["counts"][ActionPhase.A0_ORIENT.value] == 3

    # 4th read is past the orient window — UNCLASSIFIED.
    g.on_post_tool(ctx_read)
    state = StateStore(cache).read("action_phase", ctx_read.session_id)
    assert state["counts"][ActionPhase.A0_ORIENT.value] == 3
    assert state["counts"].get(ActionPhase.UNCLASSIFIED.value, 0) == 1
