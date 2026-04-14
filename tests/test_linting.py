"""Tests for cc_cortex.linting — ESLint wrapper."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from cc_cortex.linting import SUPPORTED_EXTENSIONS, run_linter


class TestRunLinter:
    def test_none_path(self):
        assert run_linter(None) is None

    def test_empty_path(self):
        assert run_linter("") is None

    def test_nonexistent_file(self):
        assert run_linter("/nonexistent/file.js") is None

    def test_unsupported_extension(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("x=1")
        assert run_linter(str(f)) is None

    def test_supported_extensions(self):
        assert ".js" in SUPPORTED_EXTENSIONS
        assert ".jsx" in SUPPORTED_EXTENSIONS
        assert ".mjs" in SUPPORTED_EXTENSIONS
        assert ".cjs" in SUPPORTED_EXTENSIONS
        assert ".ts" not in SUPPORTED_EXTENSIONS

    def test_clean_file(self, tmp_path):
        f = tmp_path / "clean.js"
        f.write_text("const x = 1;")
        mock_result = MagicMock(returncode=0, stdout="", stderr="")
        with patch("cc_cortex.linting.subprocess.run", return_value=mock_result):
            assert run_linter(str(f)) is None

    def test_errors_reported(self, tmp_path):
        f = tmp_path / "bad.js"
        f.write_text("var x = 1")
        mock_result = MagicMock(
            returncode=1,
            stdout="bad.js: line 1, col 1, Error - no-var\nbad.js: line 2, col 1, Error - semi",
            stderr="",
        )
        with patch("cc_cortex.linting.subprocess.run", return_value=mock_result):
            result = run_linter(str(f))
        assert result is not None
        assert "2 issues" in result
        assert "bad.js" in result

    def test_more_than_5_errors_truncated(self, tmp_path):
        f = tmp_path / "many.js"
        f.write_text("x")
        errors = "\n".join(f"many.js: line {i}, col 1, Error - rule{i}" for i in range(8))
        mock_result = MagicMock(returncode=1, stdout=errors, stderr="")
        with patch("cc_cortex.linting.subprocess.run", return_value=mock_result):
            result = run_linter(str(f))
        assert "8 issues" in result
        assert "3 more" in result

    def test_stderr_fallback(self, tmp_path):
        f = tmp_path / "err.js"
        f.write_text("x")
        mock_result = MagicMock(
            returncode=1, stdout="", stderr="Some error happened\nAnother line"
        )
        with patch("cc_cortex.linting.subprocess.run", return_value=mock_result):
            result = run_linter(str(f))
        assert result is not None
        assert "2 issues" in result

    def test_timeout_returns_none(self, tmp_path):
        import subprocess

        f = tmp_path / "slow.js"
        f.write_text("x")
        with patch(
            "cc_cortex.linting.subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 15)
        ):
            assert run_linter(str(f)) is None

    def test_file_not_found_returns_none(self, tmp_path):
        f = tmp_path / "missing.js"
        f.write_text("x")
        with patch("cc_cortex.linting.subprocess.run", side_effect=FileNotFoundError):
            assert run_linter(str(f)) is None

    def test_no_error_lines_returns_none(self, tmp_path):
        f = tmp_path / "noerr.js"
        f.write_text("x")
        mock_result = MagicMock(returncode=1, stdout="some output without errors", stderr="")
        with patch("cc_cortex.linting.subprocess.run", return_value=mock_result):
            assert run_linter(str(f)) is None
