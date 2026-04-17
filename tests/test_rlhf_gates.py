"""Tests for RLHF side-effect gates.

Four guards targeting RLHF-induced biases:
- OverflowGate (B1): attention overflow → block non-critical agents
- OrientationGate (B2/B3): action bias → force cost analysis before long ops
- HonestyGate (A5/C1): loss aversion → detect euphemisms masking errors
- MultiPathGate (B4/B5): premature convergence → force ≥3 alternatives
"""

from __future__ import annotations

from concinno.guards.base import GuardAction, GuardContext

# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════


def _ctx(
    tool_name: str = "Bash",
    tool_input: dict | None = None,
    cache_dir: str = "",
    hook_event: str = "PreToolUse",
    tool_result: str = "",
) -> GuardContext:
    return GuardContext(
        tool_name=tool_name,
        tool_input=tool_input or {},
        session_id="test1234",
        cache_dir=cache_dir,
        hook_event=hook_event,
        tool_result=tool_result,
    )


# ═══════════════════════════════════════════════════════════════════
# OverflowGate
# ═══════════════════════════════════════════════════════════════════


class TestOverflowGate:
    """B1: Attention overflow — block non-critical Agent spawns."""

    def test_non_agent_passes(self):
        from concinno.overflow_gate import OverflowGate

        gate = OverflowGate()
        result = gate.check(_ctx(tool_name="Read"))
        assert result is None

    def test_critical_agent_always_passes(self, monkeypatch):
        from concinno.overflow_gate import OverflowGate
        from concinno.token_zone import Zone

        monkeypatch.setattr(
            "concinno.overflow_gate._get_zone", lambda: Zone.RED
        )
        gate = OverflowGate()
        ctx = _ctx(
            tool_name="Agent",
            tool_input={"description": "write handoff summary", "prompt": "save state"},
        )
        result = gate.check(ctx)
        assert result is None

    def test_yellow_zone_blocks_exploratory_agent(self, monkeypatch):
        from concinno.overflow_gate import OverflowGate
        from concinno.token_zone import Zone

        monkeypatch.setattr(
            "concinno.overflow_gate._get_zone", lambda: Zone.YELLOW
        )
        gate = OverflowGate()
        ctx = _ctx(
            tool_name="Agent",
            tool_input={"description": "research alternatives", "prompt": "explore options"},
        )
        result = gate.check(ctx)
        assert result is not None
        assert result.action == GuardAction.DENY
        assert "overflow" in result.reason.lower()

    def test_yellow_zone_blocks(self, monkeypatch):
        from concinno.overflow_gate import OverflowGate
        from concinno.token_zone import Zone

        monkeypatch.setattr(
            "concinno.overflow_gate._get_zone", lambda: Zone.YELLOW
        )
        gate = OverflowGate()
        ctx = _ctx(
            tool_name="Agent",
            tool_input={"description": "explore codebase", "prompt": "find patterns"},
        )
        result = gate.check(ctx)
        assert result is not None
        assert result.action == GuardAction.DENY

    def test_green_zone_allows(self, monkeypatch):
        from concinno.overflow_gate import OverflowGate
        from concinno.token_zone import Zone

        monkeypatch.setattr(
            "concinno.overflow_gate._get_zone", lambda: Zone.GREEN
        )
        gate = OverflowGate()
        ctx = _ctx(
            tool_name="Agent",
            tool_input={"description": "explore codebase", "prompt": "find patterns"},
        )
        result = gate.check(ctx)
        assert result is None

    def test_burst_detection(self, tmp_path, monkeypatch):
        from concinno.overflow_gate import OverflowGate
        from concinno.token_zone import Zone

        monkeypatch.setattr(
            "concinno.overflow_gate._get_zone", lambda: Zone.GREEN
        )
        gate = OverflowGate(burst_max=3, burst_window_s=60)
        cache = str(tmp_path)

        # First 3 spawns should pass (recording timestamps)
        for i in range(3):
            ctx = _ctx(
                tool_name="Agent",
                tool_input={"description": f"task {i}", "prompt": f"do thing {i}"},
                cache_dir=cache,
            )
            result = gate.check(ctx)
            assert result is None, f"spawn {i} should pass"

        # 4th spawn should be denied (burst)
        ctx = _ctx(
            tool_name="Agent",
            tool_input={"description": "task 3", "prompt": "do thing 3"},
            cache_dir=cache,
        )
        result = gate.check(ctx)
        assert result is not None
        assert result.action == GuardAction.DENY
        assert "burst" in result.reason.lower()

    def test_guard_metadata(self):
        from concinno.overflow_gate import OverflowGate

        gate = OverflowGate()
        assert gate.name == "overflow_gate"
        assert gate.category.name == "QUALITY"
        assert gate.step_back_reason != ""


# ═══════════════════════════════════════════════════════════════════
# OrientationGate
# ═══════════════════════════════════════════════════════════════════


class TestOrientationGate:
    """B2/B3: Action bias — force cost analysis before long ops."""

    def test_non_bash_passes(self):
        from concinno.orientation_gate import OrientationGate

        gate = OrientationGate()
        result = gate.check(_ctx(tool_name="Read"))
        assert result is None

    def test_short_command_passes(self):
        from concinno.orientation_gate import OrientationGate

        gate = OrientationGate()
        result = gate.check(_ctx(tool_input={"command": "ls -la"}))
        assert result is None

    def test_deploy_blocked_without_planning(self, tmp_path):
        from concinno.orientation_gate import OrientationGate

        gate = OrientationGate()
        ctx = _ctx(
            tool_input={"command": "python deploy.py"},
            cache_dir=str(tmp_path),
        )
        result = gate.check(ctx)
        assert result is not None
        assert result.action == GuardAction.DENY
        assert "deploy" in result.reason.lower()

    def test_npm_install_blocked(self, tmp_path):
        from concinno.orientation_gate import OrientationGate

        gate = OrientationGate()
        ctx = _ctx(
            tool_input={"command": "npm install express mongoose"},
            cache_dir=str(tmp_path),
        )
        result = gate.check(ctx)
        assert result is not None
        assert result.action == GuardAction.DENY

    def test_docker_build_blocked(self, tmp_path):
        from concinno.orientation_gate import OrientationGate

        gate = OrientationGate()
        ctx = _ctx(
            tool_input={"command": "docker build -t myapp ."},
            cache_dir=str(tmp_path),
        )
        result = gate.check(ctx)
        assert result is not None
        assert result.action == GuardAction.DENY

    def test_background_command_still_denied_without_planning(self, tmp_path):
        """Background is resource management, not planning evidence."""
        from concinno.orientation_gate import OrientationGate

        gate = OrientationGate()
        ctx = _ctx(
            tool_input={"command": "npm install", "run_in_background": True},
            cache_dir=str(tmp_path),
        )
        result = gate.check(ctx)
        assert result is not None
        assert result.action == GuardAction.DENY

    def test_planning_evidence_clears_gate(self, tmp_path):
        from concinno.orientation_gate import OrientationGate

        gate = OrientationGate()
        cache = str(tmp_path)

        # Simulate planning evidence via on_post_tool
        post_ctx = _ctx(
            tool_name="Write",
            tool_input={
                "content": "The cost is ~5 minutes. Alternative: use cached build."
            },
            cache_dir=cache,
            hook_event="PostToolUse",
            tool_result="file written",
        )
        gate.on_post_tool(post_ctx)

        # Now deploy should pass
        ctx = _ctx(
            tool_input={"command": "python deploy.py"},
            cache_dir=cache,
        )
        result = gate.check(ctx)
        assert result is None

    def test_git_clone_blocked(self, tmp_path):
        from concinno.orientation_gate import OrientationGate

        gate = OrientationGate()
        ctx = _ctx(
            tool_input={"command": "git clone https://github.com/big/repo.git"},
            cache_dir=str(tmp_path),
        )
        result = gate.check(ctx)
        assert result is not None
        assert result.action == GuardAction.DENY

    def test_vite_build_blocked(self, tmp_path):
        from concinno.orientation_gate import OrientationGate

        gate = OrientationGate()
        ctx = _ctx(
            tool_input={"command": "vite build"},
            cache_dir=str(tmp_path),
        )
        result = gate.check(ctx)
        assert result is not None
        assert result.action == GuardAction.DENY

    def test_guard_metadata(self):
        from concinno.orientation_gate import OrientationGate

        gate = OrientationGate()
        assert gate.name == "orientation_gate"
        assert gate.category.name == "QUALITY"


# ═══════════════════════════════════════════════════════════════════
# HonestyGate
# ═══════════════════════════════════════════════════════════════════


class TestHonestyGate:
    """A5/C1: Loss aversion — detect euphemisms masking errors."""

    def test_non_write_passes(self):
        from concinno.honesty_gate import HonestyGate

        gate = HonestyGate()
        result = gate.check(_ctx(tool_name="Read"))
        assert result is None

    def test_no_euphemism_passes(self, tmp_path):
        from concinno.honesty_gate import HonestyGate

        gate = HonestyGate()
        ctx = _ctx(
            tool_name="Write",
            tool_input={
                "file_path": "report.md",
                "content": "The deployment failed with error: connection refused on port 3000.",
            },
            cache_dir=str(tmp_path),
        )
        result = gate.check(ctx)
        assert result is None

    def test_euphemism_without_errors_passes(self, tmp_path):
        from concinno.honesty_gate import HonestyGate

        gate = HonestyGate()
        cache = str(tmp_path)
        ctx = _ctx(
            tool_name="Write",
            tool_input={
                "file_path": "notes.md",
                "content": "The result was slightly off from expectations "
                "but this is normal variance.",
            },
            cache_dir=cache,
        )
        # No prior errors recorded — should pass
        result = gate.check(ctx)
        assert result is None

    def test_euphemism_with_recent_errors_denied(self, tmp_path):
        from concinno.honesty_gate import HonestyGate

        gate = HonestyGate()
        cache = str(tmp_path)

        # Phase 1: Record errors via on_post_tool
        post_ctx = _ctx(
            tool_name="Bash",
            cache_dir=cache,
            hook_event="PostToolUse",
            tool_result="ERROR: connection refused\nTraceback: ...\nFailed to connect",
        )
        gate.on_post_tool(post_ctx)

        # Phase 2: Write with euphemisms → should be denied
        write_ctx = _ctx(
            tool_name="Write",
            tool_input={
                "file_path": "status.md",
                "content": "The connection was slightly off but mostly works fine now.",
            },
            cache_dir=cache,
        )
        result = gate.check(write_ctx)
        assert result is not None
        assert result.action == GuardAction.DENY
        assert "euphemism" in result.reason.lower()

    def test_chinese_euphemism_detected(self, tmp_path):
        from concinno.honesty_gate import HonestyGate

        gate = HonestyGate()
        cache = str(tmp_path)

        # Record error
        post_ctx = _ctx(
            tool_name="Bash",
            cache_dir=cache,
            hook_event="PostToolUse",
            tool_result="錯誤：連線失敗\nERROR: timeout",
        )
        gate.on_post_tool(post_ctx)

        # Write with Chinese euphemism
        write_ctx = _ctx(
            tool_name="Write",
            tool_input={
                "file_path": "report.md",
                "content": "連線結果略有偏差，但基本上沒問題，已經差不多可以使用了。",
            },
            cache_dir=cache,
        )
        result = gate.check(write_ctx)
        assert result is not None
        assert result.action == GuardAction.DENY

    def test_errors_decay_after_clean_calls(self, tmp_path):
        from concinno.honesty_gate import HonestyGate

        gate = HonestyGate()
        cache = str(tmp_path)

        # Record errors
        post_ctx = _ctx(
            tool_name="Bash",
            cache_dir=cache,
            hook_event="PostToolUse",
            tool_result="ERROR: something failed",
        )
        gate.on_post_tool(post_ctx)

        # Simulate 5 clean tool calls
        for _ in range(5):
            clean_ctx = _ctx(
                tool_name="Read",
                cache_dir=cache,
                hook_event="PostToolUse",
                tool_result="file contents: all good, no problems here",
            )
            gate.on_post_tool(clean_ctx)

        # Now euphemism should pass (errors decayed)
        write_ctx = _ctx(
            tool_name="Write",
            tool_input={
                "file_path": "notes.md",
                "content": "The setup was slightly off but we've adjusted things now.",
            },
            cache_dir=cache,
        )
        result = gate.check(write_ctx)
        assert result is None

    def test_edit_also_checked(self, tmp_path):
        from concinno.honesty_gate import HonestyGate

        gate = HonestyGate()
        cache = str(tmp_path)

        # Record error
        post_ctx = _ctx(
            tool_name="Bash",
            cache_dir=cache,
            hook_event="PostToolUse",
            tool_result="FATAL: process crashed",
        )
        gate.on_post_tool(post_ctx)

        # Edit with euphemism
        edit_ctx = _ctx(
            tool_name="Edit",
            tool_input={
                "file_path": "status.md",
                "new_string": "The process had a minor issue but is almost there now.",
            },
            cache_dir=cache,
        )
        result = gate.check(edit_ctx)
        assert result is not None
        assert result.action == GuardAction.DENY

    def test_short_content_skipped(self, tmp_path):
        from concinno.honesty_gate import HonestyGate

        gate = HonestyGate()
        cache = str(tmp_path)

        # Record error
        post_ctx = _ctx(
            tool_name="Bash",
            cache_dir=cache,
            hook_event="PostToolUse",
            tool_result="ERROR: fail",
        )
        gate.on_post_tool(post_ctx)

        # Very short content — skip
        ctx = _ctx(
            tool_name="Write",
            tool_input={"file_path": "x.md", "content": "OK"},
            cache_dir=cache,
        )
        result = gate.check(ctx)
        assert result is None

    def test_guard_metadata(self):
        from concinno.honesty_gate import HonestyGate

        gate = HonestyGate()
        assert gate.name == "honesty_gate"
        assert gate.category.name == "QUALITY"


# ═══════════════════════════════════════════════════════════════════
# MultiPathGate
# ═══════════════════════════════════════════════════════════════════


class TestMultiPathGate:
    """B4/B5: Premature convergence — force ≥3 alternatives."""

    def test_non_write_passes(self):
        from concinno.multipath_gate import MultiPathGate

        gate = MultiPathGate()
        result = gate.check(_ctx(tool_name="Read"))
        assert result is None

    def test_non_planning_file_passes(self):
        from concinno.multipath_gate import MultiPathGate

        gate = MultiPathGate()
        ctx = _ctx(
            tool_name="Write",
            tool_input={
                "file_path": "src/main.py",
                "content": "The approach is to use a simple hash map. "
                "We should implement this directly." * 5,
            },
        )
        result = gate.check(ctx)
        assert result is None

    def test_planning_file_without_alternatives_denied(self):
        from concinno.multipath_gate import MultiPathGate

        gate = MultiPathGate()
        content = (
            "# Architecture Plan\n\n"
            "The approach is to use microservices. We should deploy each "
            "service independently. The implementation will use Docker and "
            "Kubernetes for orchestration. This is the recommended solution "
            "for our scaling needs."
        )
        ctx = _ctx(
            tool_name="Write",
            tool_input={
                "file_path": "05_Planning/new-architecture.md",
                "content": content,
            },
        )
        result = gate.check(ctx)
        assert result is not None
        assert result.action == GuardAction.DENY
        assert "alternative" in result.reason.lower()

    def test_planning_with_options_passes(self):
        from concinno.multipath_gate import MultiPathGate

        gate = MultiPathGate()
        content = (
            "# Architecture Decision\n\n"
            "## Options\n\n"
            "Option A: Microservices — scalable but complex\n"
            "Option B: Monolith — simple but harder to scale\n"
            "Option C: Modular monolith — middle ground\n\n"
            "Selected: Option C because it balances our current needs."
        )
        ctx = _ctx(
            tool_name="Write",
            tool_input={
                "file_path": "05_Planning/architecture-decision.md",
                "content": content,
            },
        )
        result = gate.check(ctx)
        assert result is None

    def test_comparison_table_passes(self):
        from concinno.multipath_gate import MultiPathGate

        gate = MultiPathGate()
        content = (
            "# Strategy Proposal\n\n"
            "The approach we recommend:\n\n"
            "| Option | Pros | Cons |\n"
            "|--------|------|------|\n"
            "| A | fast | costly |\n"
            "| B | cheap | slow |\n"
            "| C | balanced | mediocre |\n"
        )
        ctx = _ctx(
            tool_name="Write",
            tool_input={
                "file_path": "planning/strategy.md",
                "content": content,
            },
        )
        result = gate.check(ctx)
        assert result is None

    def test_pros_cons_passes(self):
        from concinno.multipath_gate import MultiPathGate

        gate = MultiPathGate()
        content = (
            "# Design Decision\n\n"
            "The solution we propose has clear pros and cons. "
            "We should implement the caching layer to improve performance. "
            "Advantages and disadvantages have been weighed carefully."
        )
        ctx = _ctx(
            tool_name="Write",
            tool_input={
                "file_path": "proposal/cache-design.md",
                "content": content,
            },
        )
        result = gate.check(ctx)
        assert result is None

    def test_numbered_list_passes(self):
        from concinno.multipath_gate import MultiPathGate

        gate = MultiPathGate()
        content = (
            "# Implementation Plan\n\n"
            "We should select the best approach:\n\n"
            "1. Use Redis for caching — fast, proven\n"
            "2. Use Memcached — simpler, less features\n"
            "3. Use local LRU cache — zero infra, limited\n\n"
            "Recommendation: Option 1"
        )
        ctx = _ctx(
            tool_name="Write",
            tool_input={
                "file_path": "architecture/cache-plan.md",
                "content": content,
            },
        )
        result = gate.check(ctx)
        assert result is None

    def test_short_content_exempt(self):
        from concinno.multipath_gate import MultiPathGate

        gate = MultiPathGate()
        ctx = _ctx(
            tool_name="Write",
            tool_input={
                "file_path": "05_Planning/quick-note.md",
                "content": "Status update: done.",
            },
        )
        result = gate.check(ctx)
        assert result is None

    def test_no_decision_language_passes(self):
        from concinno.multipath_gate import MultiPathGate

        gate = MultiPathGate()
        content = (
            "# Meeting Notes\n\n"
            "Today we discussed the timeline for the next sprint. "
            "Team agreed to focus on bug fixes this week. "
            "No major decisions were made. " * 5
        )
        ctx = _ctx(
            tool_name="Write",
            tool_input={
                "file_path": "05_Planning/meeting-notes.md",
                "content": content,
            },
        )
        result = gate.check(ctx)
        assert result is None

    def test_chinese_decision_file(self):
        from concinno.multipath_gate import MultiPathGate

        gate = MultiPathGate()
        content = (
            "# 架構提案\n\n"
            "建議採用微服務方案。實作方式是使用 Docker 容器化。"
            "這個策略可以解決目前的擴展需求。設計上使用事件驅動架構。" * 3
        )
        ctx = _ctx(
            tool_name="Write",
            tool_input={
                "file_path": "05_Planning/架構提案.md",
                "content": content,
            },
        )
        result = gate.check(ctx)
        assert result is not None
        assert result.action == GuardAction.DENY

    def test_three_options_chinese_passes(self):
        from concinno.multipath_gate import MultiPathGate

        gate = MultiPathGate()
        content = (
            "# 架構決策\n\n"
            "提出三個方案：\n"
            "方案 A：微服務 — 擴展性佳\n"
            "方案 B：單體 — 簡單直接\n"
            "方案 C：模組化單體 — 折衷\n\n"
            "建議採用方案 C"
        )
        ctx = _ctx(
            tool_name="Write",
            tool_input={
                "file_path": "決策/architecture.md",
                "content": content,
            },
        )
        result = gate.check(ctx)
        assert result is None

    def test_edit_also_checked(self):
        from concinno.multipath_gate import MultiPathGate

        gate = MultiPathGate()
        content = (
            "The approach is to rewrite everything from scratch. "
            "We should implement this solution immediately. "
            "The strategy involves a complete redesign of the system. " * 3
        )
        ctx = _ctx(
            tool_name="Edit",
            tool_input={
                "file_path": "spec/new-design.md",
                "new_string": content,
            },
        )
        result = gate.check(ctx)
        assert result is not None
        assert result.action == GuardAction.DENY

    def test_guard_metadata(self):
        from concinno.multipath_gate import MultiPathGate

        gate = MultiPathGate()
        assert gate.name == "multipath_gate"
        assert gate.category.name == "QUALITY"
        assert gate.step_back_reason != ""


# ═══════════════════════════════════════════════════════════════════
# Integration: Pipeline registration
# ═══════════════════════════════════════════════════════════════════


class TestRLHFGatesRegistration:
    """Verify all 4 RLHF gates are registered in default pipeline."""

    def test_all_four_registered(self):
        from concinno.guards.registry import create_default_pipeline

        pipe = create_default_pipeline()
        names = {g.name for g in pipe._guards}
        assert "overflow_gate" in names
        assert "orientation_gate" in names
        assert "honesty_gate" in names
        assert "multipath_gate" in names

    def test_guard_count_increased(self):
        from concinno.guards.registry import create_default_pipeline

        pipe = create_default_pipeline()
        # Was 39, +4 RLHF gates +1 MilestoneGate = 44, actual 42 (some merged)
        assert len(pipe._guards) >= 42
