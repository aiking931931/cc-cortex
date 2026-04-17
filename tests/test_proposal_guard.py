"""Tests for proposal_guard — side-effect analysis enforcement."""


from concinno.guards.base import GuardAction, GuardContext
from concinno.proposal_guard import (
    ProposalGuard,
    _is_planning_file,
    check_proposal,
)

# ── _is_planning_file ──────────────────────────────────────


class TestIsPlanningFile:
    def test_task_pool(self):
        assert _is_planning_file("_AI_BRAIN/06_Handoffs/concinno/task-pool.md")

    def test_handoff(self):
        assert _is_planning_file("_AI_BRAIN/06_Handoffs/psyche/交接_數位人格.md")

    def test_planning_dir(self):
        assert _is_planning_file("_AI_BRAIN/05_Planning/Hook.md")

    def test_handoff_english(self):
        assert _is_planning_file("docs/handoff-notes.md")

    def test_normal_code_file(self):
        assert not _is_planning_file("src/concinno/sentinel.py")

    def test_empty(self):
        assert not _is_planning_file("")

    def test_none_like(self):
        assert not _is_planning_file("")

    def test_windows_path(self):
        assert _is_planning_file("E:\\Cursor\\_AI_BRAIN\\05_Planning\\test.md")


# ── check_proposal ─────────────────────────────────────────


class TestCheckProposal:
    def test_non_write_tool(self):
        assert check_proposal("Read", {"file_path": "task-pool.md"}) is None

    def test_non_planning_file(self):
        assert check_proposal("Write", {
            "file_path": "src/main.py",
            "content": "⬜ new task",
        }) is None

    def test_no_proposal_marker(self):
        assert check_proposal("Write", {
            "file_path": "task-pool.md",
            "content": "just updating existing docs without new proposals",
        }) is None

    def test_short_content(self):
        assert check_proposal("Write", {
            "file_path": "task-pool.md",
            "content": "⬜",
        }) is None

    def test_proposal_without_sideeffect_deny(self):
        result = check_proposal("Write", {
            "file_path": "task-pool.md",
            "content": "⬜ 新增 RAG v2 模組\n說明：重寫 RAG 引擎",
        })
        assert result is not None
        assert "side-effect" in result.lower() or "副作用" in result

    def test_proposal_with_sideeffect_allow(self):
        result = check_proposal("Write", {
            "file_path": "task-pool.md",
            "content": "⬜ 新增 RAG v2 模組\n風險：可能影響現有搜尋 API",
        })
        assert result is None

    def test_edit_proposal_without_sideeffect(self):
        result = check_proposal("Edit", {
            "file_path": "_AI_BRAIN/06_Handoffs/concinno/task-pool.md",
            "new_string": "Phase 3: 新功能開發\n| 任務 | 狀態 |\n| P1 | ⬜ |",
        })
        assert result is not None

    def test_edit_proposal_with_risk(self):
        result = check_proposal("Edit", {
            "file_path": "_AI_BRAIN/06_Handoffs/concinno/task-pool.md",
            "new_string": (
                "Phase 3: 新功能開發\n| 任務 | 狀態 |\n| P1 | ⬜ |\n"
                "| risk | breaking change 可能影響 API |"
            ),
        })
        assert result is None

    def test_trade_off_counts(self):
        result = check_proposal("Write", {
            "file_path": "task-pool.md",
            "content": "⬜ 新模組\ntrade-off: 增加 bundle size",
        })
        assert result is None

    def test_side_effect_english(self):
        result = check_proposal("Write", {
            "file_path": "task-pool.md",
            "content": "⬜ add new feature\nside-effect: may slow startup",
        })
        assert result is None

    def test_impact_counts(self):
        result = check_proposal("Write", {
            "file_path": "task-pool.md",
            "content": "⬜ 新任務\n影響：需要更新 API",
        })
        assert result is None

    def test_notebook_edit(self):
        result = check_proposal("NotebookEdit", {
            "file_path": "05_Planning/plan.ipynb",
            "new_source": "⬜ Phase 99 新增超級功能",
        })
        assert result is not None


# ── ProposalGuard (BaseGuard adapter) ──────────────────────


class TestProposalGuard:
    def _make_ctx(self, tool_name, tool_input):
        return GuardContext(
            tool_name=tool_name,
            tool_input=tool_input,
            session_id="test",
            cache_dir="/tmp/test",
            hook_event="PreToolUse",
        )

    def test_deny_on_missing_sideeffect(self):
        guard = ProposalGuard()
        ctx = self._make_ctx("Write", {
            "file_path": "task-pool.md",
            "content": "⬜ 新增模組 X\n說明：做一件新事",
        })
        result = guard.check(ctx)
        assert result is not None
        assert result.action == GuardAction.DENY
        assert "L2" in result.context

    def test_allow_with_sideeffect(self):
        guard = ProposalGuard()
        ctx = self._make_ctx("Write", {
            "file_path": "task-pool.md",
            "content": "⬜ 新增模組 X\n副作用：可能增加啟動時間 50ms",
        })
        result = guard.check(ctx)
        assert result is None

    def test_allow_non_planning(self):
        guard = ProposalGuard()
        ctx = self._make_ctx("Write", {
            "file_path": "src/main.py",
            "content": "⬜ some marker in code",
        })
        result = guard.check(ctx)
        assert result is None

    def test_guard_metadata(self):
        guard = ProposalGuard()
        assert guard.name == "proposal_guard"
        assert guard.step_back_reason == "new proposal missing side-effect analysis"
