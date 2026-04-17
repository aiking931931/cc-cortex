"""Tests for concinno.code_guard module."""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

from concinno.code_guard import (
    SUPPORTED_EXTENSIONS,
    CodeGuard,
    _check_python,
    _file_sha256,
    _is_cached,
    _update_cache,
    check_code_guard,
)
from concinno.guards.base import GuardContext

# ── _file_sha256 ──────────────────────────────────────────


def test_file_sha256_valid_file(tmp_path):
    f = tmp_path / "hello.py"
    f.write_text("print('hi')", encoding="utf-8")
    sha = _file_sha256(str(f))
    assert len(sha) == 16
    assert all(c in "0123456789abcdef" for c in sha)


def test_file_sha256_missing_file():
    sha = _file_sha256("/nonexistent/path/file.py")
    assert sha == ""


def test_file_sha256_deterministic(tmp_path):
    f = tmp_path / "det.py"
    f.write_text("x = 1", encoding="utf-8")
    assert _file_sha256(str(f)) == _file_sha256(str(f))


# ── Cache round-trip ─────────────────────────────────────


def test_cache_roundtrip(tmp_path, monkeypatch):
    cache_file = str(tmp_path / ".concinno_cache" / "code_guard_sha.json")
    monkeypatch.setattr("concinno.code_guard._CACHE_FILE", cache_file)

    _update_cache("/some/file.py", "abc123")
    assert _is_cached("/some/file.py", "abc123") is True
    assert _is_cached("/some/file.py", "different") is False
    assert _is_cached("/other/file.py", "abc123") is False


def test_cache_bounded_to_500(tmp_path, monkeypatch):
    cache_file = str(tmp_path / ".concinno_cache" / "code_guard_sha.json")
    monkeypatch.setattr("concinno.code_guard._CACHE_FILE", cache_file)

    # Fill cache with 501 entries
    for i in range(501):
        _update_cache(f"/file_{i}.py", f"sha_{i}")

    with open(cache_file, "r", encoding="utf-8") as f:
        cache = json.load(f)

    # After evicting oldest 100, should have 401
    assert len(cache) <= 401


# ── check_code_guard ─────────────────────────────────────


def test_returns_none_for_nonexistent_file():
    result = check_code_guard("Write", {"file_path": "/no/such/file.py"})
    assert result is None


def test_returns_none_for_unsupported_txt(tmp_path):
    f = tmp_path / "readme.txt"
    f.write_text("hello", encoding="utf-8")
    result = check_code_guard("Write", {"file_path": str(f)})
    assert result is None


def test_returns_none_for_unsupported_md(tmp_path):
    f = tmp_path / "notes.md"
    f.write_text("# Title", encoding="utf-8")
    result = check_code_guard("Edit", {"file_path": str(f)})
    assert result is None


def test_extracts_file_path_key(tmp_path, monkeypatch):
    """check_code_guard extracts path from file_path, notebook_path, and path keys."""
    f = tmp_path / "test.py"
    f.write_text("x = 1\n", encoding="utf-8")
    fp = str(f)

    mock_checker = MagicMock(return_value=None)
    import concinno.code_guard as cg_mod

    monkeypatch.setitem(cg_mod._EXT_CHECKER, ".py", mock_checker)

    check_code_guard("Write", {"file_path": fp}, use_cache=False)
    assert mock_checker.called

    mock_checker.reset_mock()
    check_code_guard("Write", {"notebook_path": fp}, use_cache=False)
    assert mock_checker.called

    mock_checker.reset_mock()
    check_code_guard("Write", {"path": fp}, use_cache=False)
    assert mock_checker.called


def test_use_cache_false_always_runs_checker(tmp_path, monkeypatch):
    cache_file = str(tmp_path / ".concinno_cache" / "code_guard_sha.json")
    monkeypatch.setattr("concinno.code_guard._CACHE_FILE", cache_file)

    f = tmp_path / "cached.py"
    f.write_text("x = 1\n", encoding="utf-8")
    fp = str(f)

    # Prime cache
    sha = _file_sha256(fp)
    _update_cache(fp, sha)

    import concinno.code_guard as cg_mod

    mock_checker = MagicMock(return_value=None)
    monkeypatch.setitem(cg_mod._EXT_CHECKER, ".py", mock_checker)
    check_code_guard("Write", {"file_path": fp}, use_cache=False)
    assert mock_checker.called, "Checker must run when use_cache=False"


def test_cache_hit_skips_checker(tmp_path, monkeypatch):
    cache_file = str(tmp_path / ".concinno_cache" / "code_guard_sha.json")
    monkeypatch.setattr("concinno.code_guard._CACHE_FILE", cache_file)

    f = tmp_path / "cached2.py"
    f.write_text("x = 1\n", encoding="utf-8")
    fp = str(f)

    sha = _file_sha256(fp)
    _update_cache(fp, sha)

    with patch("concinno.code_guard._check_python", return_value=None) as mock_check:
        check_code_guard("Write", {"file_path": fp}, use_cache=True)
        assert not mock_check.called, "Checker should be skipped on cache hit"


# ── _check_python with mocked subprocess ─────────────────


def test_check_python_clean(tmp_path):
    f = tmp_path / "clean.py"
    f.write_text("x = 1\n", encoding="utf-8")

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = ""

    with patch("concinno.code_guard._run_cmd", return_value=mock_result):
        assert _check_python(str(f)) is None


def test_check_python_errors(tmp_path):
    f = tmp_path / "bad.py"
    f.write_text("import os\n", encoding="utf-8")

    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = "bad.py:1:1: F401 `os` imported but unused\n"

    with patch("concinno.code_guard._run_cmd", return_value=mock_result):
        result = _check_python(str(f))
        assert result is not None
        assert "ruff" in result
        assert "1 issues" in result


# ── SUPPORTED_EXTENSIONS ─────────────────────────────────


def test_supported_extensions():
    assert SUPPORTED_EXTENSIONS == frozenset({".py", ".rs", ".go"})


# ── Lint Debt (PreToolUse hardening) ─────────────────────


class TestLintDebt:
    """Tests for CodeGuard lint debt enforcement (Pre+PostToolUse)."""

    def _make_ctx(self, tool_name, file_path, hook_event="PreToolUse"):
        return GuardContext(
            tool_name=tool_name,
            tool_input={"file_path": file_path},
            session_id="test-session",
            cache_dir="",
            hook_event=hook_event,
        )

    def test_no_debt_allows_write(self, tmp_path, monkeypatch):
        """PreToolUse: no lint debt → allow."""
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        guard = CodeGuard()
        f = tmp_path / "new.py"
        f.write_text("x = 1\n", encoding="utf-8")
        result = guard.check(self._make_ctx("Write", str(f)))
        assert result is None

    def test_debt_on_other_file_denies(self, tmp_path, monkeypatch):
        """PreToolUse: debt on foo.py → deny Write to bar.py."""
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        foo = tmp_path / "foo.py"
        foo.write_text("bad", encoding="utf-8")
        bar = tmp_path / "bar.py"
        bar.write_text("ok", encoding="utf-8")

        # Manually write debt
        from concinno.code_guard import _write_lint_debt
        _write_lint_debt({os.path.normpath(str(foo)): "ruff error in foo.py"})

        guard = CodeGuard()
        result = guard.check(self._make_ctx("Write", str(bar)))
        assert result is not None
        assert result.action.value == "deny"
        assert "foo.py" in result.reason

    def test_debt_on_same_file_allows(self, tmp_path, monkeypatch):
        """PreToolUse: debt on foo.py → allow Edit to foo.py (user fixing it)."""
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        foo = tmp_path / "foo.py"
        foo.write_text("bad", encoding="utf-8")

        from concinno.code_guard import _write_lint_debt
        _write_lint_debt({os.path.normpath(str(foo)): "ruff error"})

        guard = CodeGuard()
        result = guard.check(self._make_ctx("Edit", str(foo)))
        assert result is None

    def test_stale_debt_cleaned(self, tmp_path, monkeypatch):
        """PreToolUse: debt for deleted file → auto-cleaned, allow."""
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        bar = tmp_path / "bar.py"
        bar.write_text("ok", encoding="utf-8")

        from concinno.code_guard import _write_lint_debt
        _write_lint_debt({"/nonexistent/deleted.py": "stale error"})

        guard = CodeGuard()
        result = guard.check(self._make_ctx("Write", str(bar)))
        assert result is None

    def test_post_tool_persists_debt(self, tmp_path, monkeypatch):
        """PostToolUse: ruff errors → debt persisted."""
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        f = tmp_path / "bad.py"
        f.write_text("import os\n", encoding="utf-8")

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = "bad.py:1:1: F401 `os` imported but unused\n"

        guard = CodeGuard()
        with patch("concinno.code_guard._run_cmd", return_value=mock_result):
            result = guard.on_post_tool(self._make_ctx("Write", str(f), "PostToolUse"))

        assert result is not None
        assert result.action.value == "allow"  # PostToolUse returns allow+context

        from concinno.code_guard import _read_lint_debt
        debt = _read_lint_debt()
        assert os.path.normpath(str(f)) in debt

    def test_post_tool_clears_debt_on_fix(self, tmp_path, monkeypatch):
        """PostToolUse: ruff clean → debt cleared."""
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        f = tmp_path / "fixed.py"
        f.write_text("x = 1\n", encoding="utf-8")

        from concinno.code_guard import _write_lint_debt
        norm = os.path.normpath(str(f))
        _write_lint_debt({norm: "old error"})

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""

        guard = CodeGuard()
        with patch("concinno.code_guard._run_cmd", return_value=mock_result):
            result = guard.on_post_tool(self._make_ctx("Edit", str(f), "PostToolUse"))

        assert result is None
        from concinno.code_guard import _read_lint_debt
        assert norm not in _read_lint_debt()

    def test_non_write_tools_ignored(self, tmp_path, monkeypatch):
        """PreToolUse: Read/Grep etc. never blocked by lint debt."""
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        foo = tmp_path / "foo.py"
        foo.write_text("x", encoding="utf-8")

        from concinno.code_guard import _write_lint_debt
        _write_lint_debt({os.path.normpath(str(foo)): "error"})

        guard = CodeGuard()
        for tool in ("Read", "Grep", "Glob", "Bash"):
            result = guard.check(self._make_ctx(tool, str(foo)))
            assert result is None

    def test_multiple_debt_files_shown(self, tmp_path, monkeypatch):
        """PreToolUse: multiple debt files → all listed in deny reason."""
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        a = tmp_path / "a.py"
        b = tmp_path / "b.py"
        c = tmp_path / "c.py"
        for f in [a, b, c]:
            f.write_text("x", encoding="utf-8")

        from concinno.code_guard import _write_lint_debt
        _write_lint_debt({
            os.path.normpath(str(a)): "error a",
            os.path.normpath(str(b)): "error b",
        })

        guard = CodeGuard()
        result = guard.check(self._make_ctx("Write", str(c)))
        assert result is not None
        assert result.action.value == "deny"
        assert "a.py" in result.reason
        assert "b.py" in result.reason
