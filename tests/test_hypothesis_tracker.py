"""Tests for cc_cortex.hypothesis_tracker — HypothesisTrackerGuard."""

from __future__ import annotations

from cc_cortex.guards.base import GuardContext
from cc_cortex.hypothesis_tracker import (
    HypothesisTrackerGuard,
    _approach_signature,
    _is_failure,
    get_failed_context,
    record_attempt,
)


def _ctx(
    tool_name, tool_input, cache_dir,
    hook_event="PreToolUse", result="",
):
    return GuardContext(
        tool_name=tool_name,
        tool_input=tool_input,
        session_id="test-session",
        cache_dir=cache_dir,
        hook_event=hook_event,
        tool_result=result,
    )


# ── Helpers ──────────────────────────────────────────────────


class TestApproachSignature:
    def test_file_path(self):
        sig = _approach_signature("Edit", {"file_path": "src/foo/bar.py"})
        assert "Edit" in sig
        assert "bar.py" in sig

    def test_bash_command(self):
        sig = _approach_signature("Bash", {"command": "npm run build"})
        assert "Bash" in sig
        assert "npm run build" in sig

    def test_truncation(self):
        sig = _approach_signature("Write", {"file_path": "a" * 200})
        assert len(sig) <= 80


class TestIsFailure:
    def test_error(self):
        assert _is_failure("Traceback (most recent call last):") is True

    def test_not_found(self):
        assert _is_failure("No such file or directory") is True

    def test_exit_code(self):
        assert _is_failure("Exit code 1") is True

    def test_success(self):
        assert _is_failure("File written successfully") is False

    def test_empty(self):
        assert _is_failure("") is False

    def test_permission(self):
        assert _is_failure("Permission denied") is True


# ── record_attempt + get_failed_context ──────────────────────


class TestRecordAndGet:
    def test_records_failure(self, tmp_path):
        record_attempt(
            str(tmp_path), "Bash",
            {"command": "npm test"},
            "Error: test failed with exit code 1",
        )
        ctx = get_failed_context(str(tmp_path))
        assert "Bash" in ctx
        assert "npm test" in ctx

    def test_ignores_success(self, tmp_path):
        record_attempt(
            str(tmp_path), "Edit",
            {"file_path": "x.py"},
            "File edited successfully",
        )
        ctx = get_failed_context(str(tmp_path))
        assert ctx == ""

    def test_dedup(self, tmp_path):
        for _ in range(3):
            record_attempt(
                str(tmp_path), "Bash",
                {"command": "npm test"},
                "Error: failed",
            )
        ctx = get_failed_context(str(tmp_path))
        # Should appear only once
        assert ctx.count("npm test") == 1

    def test_no_cache_dir(self):
        record_attempt("", "Bash", {"command": "x"}, "Error")
        assert get_failed_context("") == ""

    def test_max_history(self, tmp_path):
        for i in range(15):
            record_attempt(
                str(tmp_path), "Bash",
                {"command": f"cmd_{i}"},
                "Error: failed",
            )
        ctx = get_failed_context(str(tmp_path))
        # Shows last 5 of 10 kept
        assert "cmd_14" in ctx
        assert "cmd_0" not in ctx


# ── Guard integration ────────────────────────────────────────


class TestHypothesisTrackerGuard:
    def test_no_cache(self):
        guard = HypothesisTrackerGuard()
        ctx = _ctx("Edit", {"file_path": "x.py"}, "")
        assert guard.check(ctx) is None

    def test_read_tool_skipped(self, tmp_path):
        guard = HypothesisTrackerGuard()
        ctx = _ctx("Read", {"file_path": "x.py"}, str(tmp_path))
        assert guard.check(ctx) is None

    def test_inject_after_failure(self, tmp_path):
        guard = HypothesisTrackerGuard()

        # Record a failure via on_post_tool
        post_ctx = _ctx(
            "Bash", {"command": "npm test"}, str(tmp_path),
            hook_event="PostToolUse",
            result="Error: test failed with exit code 1",
        )
        guard.on_post_tool(post_ctx)

        # Next write tool should get context
        pre_ctx = _ctx(
            "Edit",
            {"file_path": "fix.py", "old_string": "a", "new_string": "b"},
            str(tmp_path),
        )
        result = guard.check(pre_ctx)
        assert result is not None
        assert "attempted" in result.context.lower() or "已嘗試" in result.context

    def test_no_inject_without_failure(self, tmp_path):
        guard = HypothesisTrackerGuard()
        ctx = _ctx(
            "Edit",
            {"file_path": "x.py", "old_string": "a", "new_string": "b"},
            str(tmp_path),
        )
        assert guard.check(ctx) is None

    def test_guard_metadata(self):
        guard = HypothesisTrackerGuard()
        assert guard.name == "hypothesis_tracker"
        assert guard.category.value == 3  # COGNITIVE
        assert guard.step_back_reason == ""

    def test_post_tool_records(self, tmp_path):
        guard = HypothesisTrackerGuard()
        ctx = _ctx(
            "Bash", {"command": "make build"}, str(tmp_path),
            hook_event="PostToolUse",
            result="command not found: make",
        )
        guard.on_post_tool(ctx)
        context = get_failed_context(str(tmp_path))
        assert "make build" in context
