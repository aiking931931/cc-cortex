"""Tests for cc_cortex.sibling_scan — Sibling Pattern Scan guard."""

from __future__ import annotations

import os
from unittest.mock import patch

from cc_cortex.guards.base import GuardContext
from cc_cortex.sibling_scan import (
    SiblingScanGuard,
    _build_result,
    _extract_core_pattern,
    _filter_siblings,
)


class TestExtractCorePattern:
    """Test pattern extraction from old_string."""

    def test_picks_longest_line(self):
        old = "import os\nshort\nthis_is_the_longest_line_in_the_block = True\nx = 1"
        result = _extract_core_pattern(old)
        assert "this_is_the_longest_line_in_the_block" in result

    def test_skips_imports_and_comments(self):
        old = (
            "import very_long_module_name_here\n"
            "# this is a long comment line\n"
            "actual_code_pattern_here = value"
        )
        result = _extract_core_pattern(old)
        assert "actual_code_pattern" in result

    def test_empty_string(self):
        assert _extract_core_pattern("") == ""

    def test_only_short_lines(self):
        assert _extract_core_pattern("x\ny\nz") == ""

    def test_only_comments(self):
        assert _extract_core_pattern("# long comment here enough chars\n// another comment") == ""


class TestFilterSiblings:
    """Test sibling file filtering."""

    def test_filters_edited_file(self):
        matches = ["src/a.py", "src/b.py", "src/c.py"]
        edited = os.path.join("/workspace", "src", "a.py")
        result = _filter_siblings(matches, edited, "/workspace")
        assert len(result) == 2
        assert all("a.py" not in r for r in result)

    def test_filters_skip_dirs(self):
        matches = ["node_modules/dep/file.py", "src/real.py"]
        result = _filter_siblings(matches, "/workspace/other.py", "/workspace")
        assert len(result) == 1
        assert "real.py" in result[0]

    def test_limits_to_max(self):
        matches = [f"src/file{i}.py" for i in range(20)]
        result = _filter_siblings(matches, "/workspace/other.py", "/workspace")
        assert len(result) <= 5

    def test_empty_matches(self):
        assert _filter_siblings([], "/workspace/a.py", "/workspace") == []


class TestBuildResult:
    """Test result construction."""

    def test_single_sibling(self):
        result = _build_result(["src/b.py"], 2)
        assert result.context
        assert "1 other file" in result.context
        assert "src/b.py" in result.context

    def test_multiple_siblings(self):
        siblings = ["src/a.py", "src/b.py", "src/c.py"]
        result = _build_result(siblings, 4)
        assert "3 other file" in result.context

    def test_more_indicator(self):
        siblings = ["src/a.py"]
        result = _build_result(siblings, 20)
        assert "and more" in result.context


class TestSiblingScanGuard:
    """Integration tests for the guard."""

    def _make_ctx(self, **kwargs) -> GuardContext:
        defaults = {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "/workspace/src/component_a.tsx",
                "old_string": "className={styles.overlapping_element_container}",
            },
            "session_id": "test-123",
            "cache_dir": "/tmp/cache",
            "hook_event": "PostToolUse",
            "workspace": "/workspace",
        }
        defaults.update(kwargs)
        return GuardContext(**defaults)

    def test_check_noop(self):
        guard = SiblingScanGuard()
        assert guard.check(self._make_ctx()) is None

    def test_skip_non_edit(self):
        guard = SiblingScanGuard()
        ctx = self._make_ctx(tool_name="Read")
        assert guard.on_post_tool(ctx) is None

    def test_skip_short_old_string(self):
        guard = SiblingScanGuard()
        ctx = self._make_ctx(tool_input={"file_path": "/a.py", "old_string": "x=1"})
        assert guard.on_post_tool(ctx) is None

    def test_skip_unscannable_extension(self):
        guard = SiblingScanGuard()
        ctx = self._make_ctx(
            tool_input={
                "file_path": "/workspace/data.bin",
                "old_string": "a very long pattern that should be long enough",
            }
        )
        assert guard.on_post_tool(ctx) is None

    def test_skip_no_workspace(self):
        guard = SiblingScanGuard()
        ctx = self._make_ctx(workspace="")
        assert guard.on_post_tool(ctx) is None

    @patch("cc_cortex.sibling_scan._run_grep")
    def test_no_siblings_found(self, mock_grep):
        mock_grep.return_value = ["/workspace/src/component_a.tsx"]
        guard = SiblingScanGuard()
        ctx = self._make_ctx()
        result = guard.on_post_tool(ctx)
        assert result is None

    @patch("cc_cortex.sibling_scan._run_grep")
    def test_siblings_found(self, mock_grep):
        mock_grep.return_value = [
            "src/component_a.tsx",
            "src/component_b.tsx",
            "src/component_c.tsx",
        ]
        guard = SiblingScanGuard()
        ctx = self._make_ctx()
        result = guard.on_post_tool(ctx)
        assert result is not None
        assert "Sibling pattern" in result.context
        assert "component_b" in result.context
        assert "component_c" in result.context

    def test_guard_registration(self):
        """Verify guard can be registered in pipeline."""
        from cc_cortex.guards.pipeline import GuardPipeline
        pipe = GuardPipeline()
        guard = SiblingScanGuard()
        pipe.register(guard)
        assert guard.name in str(pipe.list_guards())
