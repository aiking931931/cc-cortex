"""Tests for concinno.think_inject — ThinkInjectGuard."""

from __future__ import annotations

import pytest

from concinno.guards.base import GuardContext
from concinno.think_inject import (
    ThinkInjectGuard,
    _count_deleted_lines,
    _is_architecture_file,
    _is_new_module,
)


@pytest.fixture
def tmp_cache(tmp_path):
    return str(tmp_path)


def _ctx(tool_name, tool_input, cache_dir, hook_event="PostToolUse", result=""):
    return GuardContext(
        tool_name=tool_name,
        tool_input=tool_input,
        session_id="test-session",
        cache_dir=cache_dir,
        hook_event=hook_event,
        tool_result=result,
    )


# ── Unit tests for helpers ──────────────────────────────────


class TestCountDeletedLines:
    def test_pure_deletion(self):
        assert _count_deleted_lines({
            "old_string": "a\nb\nc\nd\ne",
            "new_string": "a",
        }) == 4

    def test_no_deletion(self):
        assert _count_deleted_lines({
            "old_string": "a",
            "new_string": "a\nb\nc",
        }) == 0

    def test_empty(self):
        assert _count_deleted_lines({}) == 0

    def test_equal_lines(self):
        assert _count_deleted_lines({
            "old_string": "a\nb",
            "new_string": "c\nd",
        }) == 0


class TestIsNewModule:
    def test_new_py_file(self, tmp_path):
        assert _is_new_module("Write", {
            "file_path": str(tmp_path / "new_mod.py"),
        }) is True

    def test_existing_py_file(self, tmp_path):
        existing = tmp_path / "existing.py"
        existing.write_text("pass")
        assert _is_new_module("Write", {
            "file_path": str(existing),
        }) is False

    def test_non_py_file(self, tmp_path):
        assert _is_new_module("Write", {
            "file_path": str(tmp_path / "readme.md"),
        }) is False

    def test_edit_not_write(self, tmp_path):
        assert _is_new_module("Edit", {
            "file_path": str(tmp_path / "new.py"),
        }) is False


class TestIsArchitectureFile:
    def test_guards_dir(self):
        assert _is_architecture_file(
            "src/concinno/guards/base.py",
            ["guards/", "core/"],
        ) is True

    def test_core_dir(self):
        assert _is_architecture_file(
            "src/concinno/core/state_store.py",
            ["guards/", "core/"],
        ) is True

    def test_init_file(self):
        assert _is_architecture_file(
            "src/pkg/__init__.py",
            ["__init__.py"],
        ) is True

    def test_regular_file(self):
        assert _is_architecture_file(
            "src/concinno/delivery.py",
            ["guards/", "core/"],
        ) is False

    def test_empty_path(self):
        assert _is_architecture_file("", ["guards/"]) is False

    def test_windows_path(self):
        assert _is_architecture_file(
            "src\\concinno\\guards\\pipeline.py",
            ["guards/"],
        ) is True


# ── Guard integration tests ─────────────────────────────────


class TestThinkInjectGuard:
    def test_check_is_noop(self, tmp_cache):
        guard = ThinkInjectGuard()
        ctx = _ctx("Write", {"file_path": "x.py"}, tmp_cache,
                    hook_event="PreToolUse")
        assert guard.check(ctx) is None

    def test_no_cache_dir(self):
        guard = ThinkInjectGuard()
        ctx = _ctx("Write", {"file_path": "x.py"}, "",
                    hook_event="PostToolUse")
        assert guard.on_post_tool(ctx) is None

    def test_read_tool_ignored(self, tmp_cache):
        guard = ThinkInjectGuard()
        ctx = _ctx("Read", {"file_path": "x.py"}, tmp_cache)
        assert guard.on_post_tool(ctx) is None

    def test_multi_file_trigger(self, tmp_cache):
        guard = ThinkInjectGuard({"files_edited_trigger": 3})
        # Edit 3 files
        for i in range(3):
            ctx = _ctx("Edit", {
                "file_path": f"file_{i}.py",
                "old_string": "a",
                "new_string": "b",
            }, tmp_cache)
            result = guard.on_post_tool(ctx)

        # Third file should trigger
        assert result is not None
        assert "3 files" in result.context

    def test_multi_file_no_trigger_below_threshold(self, tmp_cache):
        guard = ThinkInjectGuard({
            "files_edited_trigger": 5,
            "blind_edit_trigger": 99,  # disable cognitive trigger
        })
        results = []
        for i in range(3):
            ctx = _ctx("Edit", {
                "file_path": f"file_{i}.py",
                "old_string": "a",
                "new_string": "b",
            }, tmp_cache)
            results.append(guard.on_post_tool(ctx))

        # None should trigger (threshold=5)
        for r in results:
            assert r is None

    def test_large_deletion_trigger(self, tmp_cache):
        guard = ThinkInjectGuard({"lines_deleted_trigger": 5})
        old = "\n".join(f"line {i}" for i in range(10))
        ctx = _ctx("Edit", {
            "file_path": "big.py",
            "old_string": old,
            "new_string": "# replaced",
        }, tmp_cache)
        result = guard.on_post_tool(ctx)
        assert result is not None
        assert "connected" in result.context.lower()

    def test_small_deletion_no_trigger(self, tmp_cache):
        guard = ThinkInjectGuard({"lines_deleted_trigger": 50})
        ctx = _ctx("Edit", {
            "file_path": "small.py",
            "old_string": "a\nb",
            "new_string": "a",
        }, tmp_cache)
        result = guard.on_post_tool(ctx)
        assert result is None

    def test_new_module_trigger(self, tmp_cache, tmp_path):
        guard = ThinkInjectGuard({"new_module_trigger": True})
        new_file = str(tmp_path / "brand_new.py")
        ctx = _ctx("Write", {"file_path": new_file}, tmp_cache)
        result = guard.on_post_tool(ctx)
        assert result is not None
        assert "new file" in result.context.lower() or "promise" in result.context.lower()

    def test_architecture_file_trigger(self, tmp_cache):
        guard = ThinkInjectGuard({
            "architecture_patterns": ["guards/", "core/"],
        })
        ctx = _ctx("Edit", {
            "file_path": "src/concinno/guards/base.py",
            "old_string": "a",
            "new_string": "b",
        }, tmp_cache)
        result = guard.on_post_tool(ctx)
        assert result is not None
        assert "foundation" in result.context.lower()

    def test_dedup_same_trigger(self, tmp_cache):
        """Same trigger type should not fire twice."""
        guard = ThinkInjectGuard({
            "architecture_patterns": ["guards/"],
        })
        ctx1 = _ctx("Edit", {
            "file_path": "guards/a.py",
            "old_string": "x",
            "new_string": "y",
        }, tmp_cache)
        r1 = guard.on_post_tool(ctx1)

        ctx2 = _ctx("Edit", {
            "file_path": "guards/b.py",
            "old_string": "x",
            "new_string": "y",
        }, tmp_cache)
        r2 = guard.on_post_tool(ctx2)

        assert r1 is not None
        assert r2 is None  # deduped

    def test_guard_metadata(self):
        guard = ThinkInjectGuard()
        assert guard.name == "think_inject"
        assert guard.category.value == 3  # COGNITIVE
        assert guard.step_back_reason == ""

    def test_custom_thresholds(self):
        guard = ThinkInjectGuard({
            "files_edited_trigger": 10,
            "lines_deleted_trigger": 100,
        })
        assert guard._thresholds["files_edited_trigger"] == 10
        assert guard._thresholds["lines_deleted_trigger"] == 100
        # defaults still present
        assert guard._thresholds["new_module_trigger"] is True

    def test_duplicate_file_not_counted(self, tmp_cache):
        guard = ThinkInjectGuard({
            "files_edited_trigger": 2,
            "blind_edit_trigger": 99,
        })
        # Edit same file twice
        for _ in range(3):
            ctx = _ctx("Edit", {
                "file_path": "same.py",
                "old_string": "a",
                "new_string": "b",
            }, tmp_cache)
            result = guard.on_post_tool(ctx)

        # Should not trigger — only 1 unique file
        assert result is None


class TestCognitiveTriggers:
    """Tests for cognitive anti-pattern detection."""

    def test_blind_edit_triggers(self, tmp_cache):
        """3+ writes without read → inject."""
        guard = ThinkInjectGuard({"blind_edit_trigger": 3})
        results = []
        for i in range(4):
            ctx = _ctx("Edit", {
                "file_path": f"f{i}.py",
                "old_string": "a",
                "new_string": "b",
            }, tmp_cache)
            results.append(guard.on_post_tool(ctx))

        # 3rd edit should trigger blind_edit
        assert any(
            r is not None and "without reading" in r.context
            for r in results
        )

    def test_read_resets_blind_counter(self, tmp_cache):
        """Read between edits resets the blind counter."""
        guard = ThinkInjectGuard({"blind_edit_trigger": 3})
        for i in range(2):
            ctx = _ctx("Edit", {
                "file_path": f"f{i}.py",
                "old_string": "a",
                "new_string": "b",
            }, tmp_cache)
            guard.on_post_tool(ctx)

        # Read resets counter
        read_ctx = _ctx("Read", {
            "file_path": "f0.py",
        }, tmp_cache)
        guard.on_post_tool(read_ctx)

        # 2 more edits — still below threshold
        results = []
        for i in range(2):
            ctx = _ctx("Edit", {
                "file_path": f"g{i}.py",
                "old_string": "a",
                "new_string": "b",
            }, tmp_cache)
            results.append(guard.on_post_tool(ctx))

        assert all(
            r is None or "without reading" not in r.context
            for r in results
        )

    def test_consecutive_failure_triggers(self, tmp_cache):
        """2+ consecutive tool failures → inject hypothesis."""
        guard = ThinkInjectGuard({"failure_trigger": 2})
        results = []
        for i in range(3):
            ctx = _ctx("Bash", {
                "command": f"test {i}",
            }, tmp_cache, result="Error: command failed")
            results.append(guard.on_post_tool(ctx))

        # 2nd call should trigger (deduped on 3rd)
        assert any(
            r is not None and "hypothes" in r.context.lower()
            for r in results
        )

    def test_success_resets_failure_counter(self, tmp_cache):
        """Successful edit resets failure counter."""
        guard = ThinkInjectGuard({
            "failure_trigger": 2,
            "blind_edit_trigger": 99,
        })
        # One failure
        ctx = _ctx("Bash", {
            "command": "test",
        }, tmp_cache, result="Error: failed")
        guard.on_post_tool(ctx)

        # Successful edit resets
        ctx = _ctx("Edit", {
            "file_path": "ok.py",
            "old_string": "a",
            "new_string": "b",
        }, tmp_cache, result="ok")
        guard.on_post_tool(ctx)

        # One more failure — should NOT trigger (counter reset)
        ctx = _ctx("Bash", {
            "command": "test2",
        }, tmp_cache, result="Error: failed again")
        result = guard.on_post_tool(ctx)
        assert result is None

    def test_grep_resets_blind_counter(self, tmp_cache):
        """Grep also counts as reading."""
        guard = ThinkInjectGuard({"blind_edit_trigger": 3})
        for i in range(2):
            ctx = _ctx("Edit", {
                "file_path": f"f{i}.py",
                "old_string": "a",
                "new_string": "b",
            }, tmp_cache)
            guard.on_post_tool(ctx)

        grep_ctx = _ctx("Grep", {
            "pattern": "foo",
        }, tmp_cache)
        guard.on_post_tool(grep_ctx)

        # Counter reset, 2 more edits OK
        for i in range(2):
            ctx = _ctx("Edit", {
                "file_path": f"h{i}.py",
                "old_string": "a",
                "new_string": "b",
            }, tmp_cache)
            result = guard.on_post_tool(ctx)

        assert result is None or "without reading" not in (
            result.context if result else ""
        )
