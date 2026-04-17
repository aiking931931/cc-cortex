"""Tests for concinno.typescript — TypeScript tsc checker with SHA256 cache."""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

from concinno.typescript import (
    SUPPORTED_EXTENSIONS,
    _file_sha256,
    _find_ts_project,
    _match_configured_project,
    check_typescript,
)

# ── SUPPORTED_EXTENSIONS ─────────────────────────────────


class TestSupportedExtensions:
    def test_ts_supported(self):
        assert ".ts" in SUPPORTED_EXTENSIONS
        assert ".tsx" in SUPPORTED_EXTENSIONS

    def test_js_not_supported(self):
        assert ".js" not in SUPPORTED_EXTENSIONS


# ── _file_sha256 ─────────────────────────────────────────


class TestFileSha256:
    def test_valid_file(self, tmp_path):
        f = tmp_path / "test.ts"
        f.write_text("const x: number = 1;")
        sha = _file_sha256(str(f))
        assert len(sha) == 16  # truncated to 16 chars

    def test_same_content_same_sha(self, tmp_path):
        f1 = tmp_path / "a.ts"
        f2 = tmp_path / "b.ts"
        f1.write_text("hello")
        f2.write_text("hello")
        assert _file_sha256(str(f1)) == _file_sha256(str(f2))

    def test_different_content_different_sha(self, tmp_path):
        f1 = tmp_path / "a.ts"
        f2 = tmp_path / "b.ts"
        f1.write_text("hello")
        f2.write_text("world")
        assert _file_sha256(str(f1)) != _file_sha256(str(f2))

    def test_nonexistent_file(self):
        assert _file_sha256("/nonexistent") == ""


# ── _find_ts_project ─────────────────────────────────────


class TestFindTsProject:
    def test_finds_tsconfig(self, tmp_path):
        proj = tmp_path / "myapp"
        proj.mkdir()
        (proj / "tsconfig.json").write_text("{}")
        src = proj / "src"
        src.mkdir()
        f = src / "index.ts"
        f.write_text("export {}")
        result = _find_ts_project(str(f))
        assert result is not None
        assert os.path.normpath(result) == os.path.normpath(str(proj))

    def test_no_tsconfig(self, tmp_path):
        f = tmp_path / "orphan.ts"
        f.write_text("x")
        assert _find_ts_project(str(f)) is None


# ── _match_configured_project ────────────────────────────


class TestMatchConfiguredProject:
    def test_matches_configured(self, tmp_path):
        proj = str(tmp_path / "myapp")
        projects = [(proj, "myapp")]
        file_path = os.path.join(proj, "src", "index.ts")
        p, n = _match_configured_project(file_path, projects)
        assert p == proj
        assert n == "myapp"

    def test_no_match(self, tmp_path):
        projects = [("/other/proj", "other")]
        p, n = _match_configured_project("/somewhere/file.ts", projects)
        assert p is None
        assert n is None

    def test_none_projects(self):
        p, n = _match_configured_project("/some/file.ts", None)
        assert p is None


# ── check_typescript ─────────────────────────────────────


class TestCheckTypescript:
    def test_non_ts_file_passes(self):
        assert check_typescript("Write", {"file_path": "src/main.py"}) is None

    def test_empty_path_passes(self):
        assert check_typescript("Write", {"file_path": ""}) is None
        assert check_typescript("Write", {}) is None

    def test_no_project_found(self, tmp_path):
        f = tmp_path / "orphan.ts"
        f.write_text("const x = 1;")
        assert check_typescript("Write", {"file_path": str(f)}, use_cache=False) is None

    def test_tsc_success(self, tmp_path):
        proj = tmp_path / "app"
        proj.mkdir()
        (proj / "tsconfig.json").write_text("{}")
        f = proj / "index.ts"
        f.write_text("const x: number = 1;")

        mock_result = MagicMock(returncode=0, stdout="", stderr="")
        with patch("concinno.typescript.subprocess.run", return_value=mock_result):
            result = check_typescript("Write", {"file_path": str(f)}, use_cache=False)
        assert result is None

    def test_tsc_errors(self, tmp_path):
        proj = tmp_path / "app"
        proj.mkdir()
        (proj / "tsconfig.json").write_text("{}")
        f = proj / "index.ts"
        f.write_text("const x: number = 'bad';")

        errors = (
            "index.ts(1,7): error TS2322: Type 'string' not assignable.\n"
            "index.ts(2,3): error TS1005: ';' expected.\n"
        )
        mock_result = MagicMock(returncode=1, stdout=errors, stderr="")
        with patch("concinno.typescript.subprocess.run", return_value=mock_result):
            result = check_typescript("Write", {"file_path": str(f)}, use_cache=False)
        assert result is not None
        assert "2 errors" in result
        assert "error TS2322" in result

    def test_tsc_many_errors_truncated(self, tmp_path):
        proj = tmp_path / "app"
        proj.mkdir()
        (proj / "tsconfig.json").write_text("{}")
        f = proj / "index.ts"
        f.write_text("x")

        errors = "\n".join(f"file.ts({i},1): error TS{i}: err" for i in range(6))
        mock_result = MagicMock(returncode=1, stdout=errors, stderr="")
        with patch("concinno.typescript.subprocess.run", return_value=mock_result):
            result = check_typescript("Write", {"file_path": str(f)}, use_cache=False)
        assert "6 errors" in result
        assert "3 more" in result

    def test_timeout_returns_none(self, tmp_path):
        import subprocess

        proj = tmp_path / "app"
        proj.mkdir()
        (proj / "tsconfig.json").write_text("{}")
        f = proj / "index.ts"
        f.write_text("x")

        with patch(
            "concinno.typescript.subprocess.run",
            side_effect=subprocess.TimeoutExpired("cmd", 15),
        ):
            assert check_typescript("Write", {"file_path": str(f)}, use_cache=False) is None

    def test_configured_project(self, tmp_path):
        proj = tmp_path / "app"
        proj.mkdir()
        (proj / "tsconfig.json").write_text("{}")
        f = proj / "index.ts"
        f.write_text("x")

        mock_result = MagicMock(returncode=0, stdout="", stderr="")
        with patch("concinno.typescript.subprocess.run", return_value=mock_result):
            result = check_typescript(
                "Write",
                {"file_path": str(f)},
                ts_projects=[(str(proj), "MyApp")],
                use_cache=False,
            )
        assert result is None

    def test_cache_hit_skips_tsc(self, tmp_path):
        proj = tmp_path / "app"
        proj.mkdir()
        (proj / "tsconfig.json").write_text("{}")
        f = proj / "index.ts"
        f.write_text("const x: number = 1;")
        sha = _file_sha256(str(f))

        cache_file = tmp_path / ".concinno_cache" / "tsc_sha.json"
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({str(f): sha}))

        with patch("concinno.typescript._CACHE_FILE", str(cache_file)):
            # Should skip tsc entirely due to cache hit
            with patch("concinno.typescript.subprocess.run") as mock_run:
                result = check_typescript("Write", {"file_path": str(f)}, use_cache=True)
                mock_run.assert_not_called()
        assert result is None

    def test_no_error_ts_lines_returns_none(self, tmp_path):
        proj = tmp_path / "app"
        proj.mkdir()
        (proj / "tsconfig.json").write_text("{}")
        f = proj / "index.ts"
        f.write_text("x")

        mock_result = MagicMock(returncode=1, stdout="some output", stderr="")
        with patch("concinno.typescript.subprocess.run", return_value=mock_result):
            assert check_typescript("Write", {"file_path": str(f)}, use_cache=False) is None
