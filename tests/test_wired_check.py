"""Tests for WIREDO check (delivery.wiredo + helpers)."""

from __future__ import annotations

from cc_cortex.delivery import (
    _find_unwired_files,
    _get_session_code_files,
    _is_wired,
    wiredo_check,
)

# Backward compat alias used in existing tests
wired_check = wiredo_check


class TestIsWired:
    """Test _is_wired import detection."""

    def test_wired_file_found(self, tmp_path):
        """File that is imported by another file should be wired."""
        # Create a module
        mod = tmp_path / "my_module.py"
        mod.write_text("def hello(): pass\n")
        # Create a consumer that imports it
        consumer = tmp_path / "main.py"
        consumer.write_text("from my_module import hello\n")

        assert _is_wired("my_module", str(mod), str(tmp_path)) is True

    def test_unwired_file(self, tmp_path):
        """File that nobody imports should not be wired."""
        mod = tmp_path / "orphan.py"
        mod.write_text("def lonely(): pass\n")
        # No other file references it
        other = tmp_path / "unrelated.py"
        other.write_text("import os\n")

        assert _is_wired("orphan", str(mod), str(tmp_path)) is False

    def test_self_reference_not_counted(self, tmp_path):
        """A file importing itself doesn't count as wired."""
        mod = tmp_path / "self_ref.py"
        mod.write_text("# self_ref is used here\nimport self_ref\n")

        assert _is_wired("self_ref", str(mod), str(tmp_path)) is False


class TestFindUnwiredFiles:
    """Test _find_unwired_files filtering."""

    def test_skips_init_files(self, tmp_path):
        """__init__.py should be skipped (barrel file)."""
        init = tmp_path / "__init__.py"
        init.write_text("from .foo import bar\n")

        result = _find_unwired_files([str(init)], str(tmp_path))
        assert result == []

    def test_skips_underscore_prefix(self, tmp_path):
        """Files starting with _ should be skipped (internal)."""
        internal = tmp_path / "_internal.py"
        internal.write_text("SECRET = 42\n")

        result = _find_unwired_files([str(internal)], str(tmp_path))
        assert result == []

    def test_skips_index_files(self, tmp_path):
        """index.ts should be skipped (barrel file)."""
        idx = tmp_path / "index.ts"
        idx.write_text("export * from './foo'\n")

        result = _find_unwired_files([str(idx)], str(tmp_path))
        assert result == []

    def test_detects_orphan(self, tmp_path):
        """Unwired file should be detected."""
        orphan = tmp_path / "orphan.py"
        orphan.write_text("def lonely(): pass\n")

        result = _find_unwired_files([str(orphan)], str(tmp_path))
        assert len(result) == 1
        assert "orphan.py" in result[0]


class TestGetSessionCodeFiles:
    """Test _get_session_code_files sentinel state reading."""

    def test_empty_when_no_state(self, tmp_path):
        """No sentinel state = empty list."""
        result = _get_session_code_files(str(tmp_path), "nonexistent")
        assert result == []

    def test_filters_non_code_files(self, tmp_path):
        """Only code files (.py/.ts/.tsx/.js/.jsx) should be returned."""
        # Create sentinel state with mixed files
        from cc_cortex.core.state_store import StateStore
        store = StateStore(str(tmp_path))
        py_file = tmp_path / "test.py"
        py_file.write_text("x = 1\n")
        md_file = tmp_path / "readme.md"
        md_file.write_text("# Hello\n")

        store.write("sentinel", "test_session", {
            "edited_files": [str(py_file), str(md_file)],
        })

        result = _get_session_code_files(str(tmp_path), "test_session")
        assert len(result) == 1
        assert str(py_file) in result


class TestWiredCheck:
    """Test wired_check end-to-end."""

    def test_empty_when_no_files(self, tmp_path):
        """No edited files = empty report."""
        result = wired_check(str(tmp_path), "empty_session")
        assert result == ""

    def test_reports_orphans(self, tmp_path, monkeypatch):
        """Orphan files should appear in the report."""
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        from cc_cortex.core.state_store import StateStore
        store = StateStore(str(tmp_path))

        orphan = tmp_path / "orphan_module.py"
        orphan.write_text("def lost(): pass\n")

        store.write("sentinel", "test_session", {
            "edited_files": [str(orphan)],
        })

        result = wired_check(str(tmp_path), "test_session")
        assert "WIREDO-W" in result
        assert "orphan_module" in result

    def test_includes_wired_summary(self, tmp_path, monkeypatch):
        """WIRED summary line should always be present when code files edited."""
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        from cc_cortex.core.state_store import StateStore
        store = StateStore(str(tmp_path))

        # Create a wired file (imported by another)
        mod = tmp_path / "utils.py"
        mod.write_text("def helper(): pass\n")
        consumer = tmp_path / "main.py"
        consumer.write_text("from utils import helper\n")

        store.write("sentinel", "test_session", {
            "edited_files": [str(mod)],
        })

        result = wired_check(str(tmp_path), "test_session")
        assert "[WIREDO]" in result
        assert "1 code files edited" in result
