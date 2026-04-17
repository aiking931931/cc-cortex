"""Tests for concinno.tools.builtin.search — FileGlob and FileGrep."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

from concinno.tools.builtin.search import (
    EXCLUDED_DIRS,
    FileGlob,
    FileGrep,
    _decode_cursor,
    _encode_cursor,
    _is_excluded,
)

# ── Helpers ──────────────────────────────────────────────


def _touch(p: Path, content: str = "") -> Path:
    """Create file with parents and content."""
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def _set_mtime(p: Path, ts: float) -> None:
    """Set mtime so we can verify mtime-desc sort deterministically."""
    os.utime(p, (ts, ts))


# ── TestHelpers ──────────────────────────────────────────


class TestHelpers:
    def test_excluded_dirs_contains_git(self):
        assert ".git" in EXCLUDED_DIRS
        assert "node_modules" in EXCLUDED_DIRS
        assert "__pycache__" in EXCLUDED_DIRS

    def test_is_excluded_top_level(self):
        assert _is_excluded(Path(".git/config"))
        assert _is_excluded(Path("a/node_modules/b/c.js"))

    def test_is_excluded_clean_path(self):
        assert not _is_excluded(Path("src/foo.py"))

    def test_cursor_round_trip(self):
        for n in (0, 1, 100, 9999):
            assert _decode_cursor(_encode_cursor(n)) == n

    def test_cursor_empty_returns_zero(self):
        assert _decode_cursor("") == 0

    def test_cursor_garbage_returns_zero(self):
        assert _decode_cursor("not-base64!!!") == 0


# ── TestFileGlob ─────────────────────────────────────────


class TestFileGlob:
    def test_basic_match(self, tmp_path: Path):
        _touch(tmp_path / "a.py", "x")
        _touch(tmp_path / "b.txt", "y")
        result = FileGlob().call(pattern="*.py", path=str(tmp_path))
        assert result["matches"] == ["a.py"]
        assert result["total"] == 1
        assert result["truncated"] is False
        assert result["next_cursor"] is None

    def test_recursive_subdirs(self, tmp_path: Path):
        _touch(tmp_path / "src" / "a.py", "")
        _touch(tmp_path / "src" / "deep" / "b.py", "")
        _touch(tmp_path / "tests" / "c.py", "")
        result = FileGlob().call(pattern="**/*.py", path=str(tmp_path))
        names = {Path(m).name for m in result["matches"]}
        assert names == {"a.py", "b.py", "c.py"}
        assert result["total"] == 3

    def test_excludes_git(self, tmp_path: Path):
        _touch(tmp_path / "ok.py", "")
        _touch(tmp_path / ".git" / "secret.py", "")
        result = FileGlob().call(pattern="**/*.py", path=str(tmp_path))
        matches = result["matches"]
        assert "ok.py" in matches
        assert not any("secret.py" in m for m in matches)

    def test_excludes_node_modules(self, tmp_path: Path):
        _touch(tmp_path / "ok.js", "")
        _touch(tmp_path / "node_modules" / "lib" / "x.js", "")
        result = FileGlob().call(pattern="**/*.js", path=str(tmp_path))
        assert result["total"] == 1
        assert "ok.js" in result["matches"][0]

    def test_excludes_pycache(self, tmp_path: Path):
        _touch(tmp_path / "real.py", "")
        _touch(tmp_path / "__pycache__" / "real.cpython-312.pyc", "")
        result = FileGlob().call(pattern="**/*.py", path=str(tmp_path))
        assert result["total"] == 1

    def test_mtime_descending_sort(self, tmp_path: Path):
        old = _touch(tmp_path / "old.py", "")
        mid = _touch(tmp_path / "mid.py", "")
        new = _touch(tmp_path / "new.py", "")
        base = time.time()
        _set_mtime(old, base - 100)
        _set_mtime(mid, base - 50)
        _set_mtime(new, base)
        result = FileGlob().call(pattern="*.py", path=str(tmp_path))
        assert result["matches"] == ["new.py", "mid.py", "old.py"]

    def test_head_limit_truncation(self, tmp_path: Path):
        for i in range(10):
            _touch(tmp_path / f"f{i:02d}.py", "")
        result = FileGlob().call(
            pattern="*.py", path=str(tmp_path), head_limit=3
        )
        assert len(result["matches"]) == 3
        assert result["total"] == 10
        assert result["truncated"] is True
        assert result["next_cursor"] is not None

    def test_cursor_pagination_three_pages(self, tmp_path: Path):
        for i in range(7):
            f = _touch(tmp_path / f"f{i}.py", "")
            _set_mtime(f, time.time() - i)  # f0 newest, f6 oldest
        tool = FileGlob()

        page1 = tool.call(pattern="*.py", path=str(tmp_path), head_limit=3)
        assert page1["matches"] == ["f0.py", "f1.py", "f2.py"]
        assert page1["truncated"] is True

        page2 = tool.call(
            pattern="*.py",
            path=str(tmp_path),
            head_limit=3,
            cursor=page1["next_cursor"],
        )
        assert page2["matches"] == ["f3.py", "f4.py", "f5.py"]
        assert page2["truncated"] is True

        page3 = tool.call(
            pattern="*.py",
            path=str(tmp_path),
            head_limit=3,
            cursor=page2["next_cursor"],
        )
        assert page3["matches"] == ["f6.py"]
        assert page3["truncated"] is False
        assert page3["next_cursor"] is None

    def test_continuation_total_is_stable(self, tmp_path: Path):
        for i in range(5):
            _touch(tmp_path / f"f{i}.py", "")
        tool = FileGlob()
        p1 = tool.call(pattern="*.py", path=str(tmp_path), head_limit=2)
        p2 = tool.call(
            pattern="*.py",
            path=str(tmp_path),
            head_limit=2,
            cursor=p1["next_cursor"],
        )
        assert p1["total"] == p2["total"] == 5

    def test_unlimited_head_limit_zero(self, tmp_path: Path):
        for i in range(20):
            _touch(tmp_path / f"f{i}.py", "")
        result = FileGlob().call(
            pattern="*.py", path=str(tmp_path), head_limit=0
        )
        assert len(result["matches"]) == 20
        assert result["truncated"] is False
        assert result["next_cursor"] is None

    def test_missing_pattern_raises(self, tmp_path: Path):
        with pytest.raises(ValueError, match="pattern is required"):
            FileGlob().call(pattern="", path=str(tmp_path))

    def test_bad_path_raises(self, tmp_path: Path):
        with pytest.raises(ValueError, match="not a directory"):
            FileGlob().call(pattern="*.py", path=str(tmp_path / "nope"))

    def test_concurrency_safe_flag(self):
        assert FileGlob.is_concurrency_safe is True
        assert FileGlob.name == "Glob"


# ── TestFileGrep ─────────────────────────────────────────


class TestFileGrep:
    def _tree(self, tmp_path: Path) -> None:
        _touch(tmp_path / "a.py", "hello world\nfoo bar\n")
        _touch(tmp_path / "b.py", "HELLO again\nbaz\n")
        _touch(tmp_path / "c.txt", "hello text\n")
        _touch(tmp_path / ".git" / "ignored.py", "hello secret\n")

    def test_python_fallback_files_with_matches(self, tmp_path: Path):
        self._tree(tmp_path)
        tool = FileGrep(force_backend="python")
        result = tool.call(
            pattern="hello", path=str(tmp_path), output_mode="files_with_matches"
        )
        assert result["backend"] == "python"
        files = set(result["results"])
        assert "a.py" in files
        assert "c.txt" in files
        # excluded by .git
        assert not any("ignored" in f for f in files)

    def test_python_fallback_content_mode(self, tmp_path: Path):
        self._tree(tmp_path)
        tool = FileGrep(force_backend="python")
        result = tool.call(
            pattern="hello", path=str(tmp_path), output_mode="content"
        )
        assert result["backend"] == "python"
        # Each entry has file/line/text
        for entry in result["results"]:
            assert "file" in entry
            assert "line" in entry
            assert "text" in entry
            assert "hello" in entry["text"].lower()

    def test_python_fallback_count_mode(self, tmp_path: Path):
        _touch(tmp_path / "a.py", "x\nx\nx\n")
        _touch(tmp_path / "b.py", "x\n")
        tool = FileGrep(force_backend="python")
        result = tool.call(
            pattern="x", path=str(tmp_path), output_mode="count"
        )
        counts = {entry["file"]: entry["count"] for entry in result["results"]}
        assert counts["a.py"] == 3
        assert counts["b.py"] == 1

    def test_case_insensitive(self, tmp_path: Path):
        _touch(tmp_path / "a.py", "HELLO\n")
        tool = FileGrep(force_backend="python")
        without = tool.call(
            pattern="hello", path=str(tmp_path), output_mode="files_with_matches"
        )
        assert without["results"] == []
        with_ci = tool.call(
            pattern="hello",
            path=str(tmp_path),
            output_mode="files_with_matches",
            case_insensitive=True,
        )
        assert with_ci["results"] == ["a.py"]

    def test_multiline_dotall(self, tmp_path: Path):
        _touch(tmp_path / "a.py", "start\nmiddle\nend\n")
        tool = FileGrep(force_backend="python")
        # Without multiline, "." won't span newlines.
        nonmulti = tool.call(
            pattern="start.end",
            path=str(tmp_path),
            output_mode="files_with_matches",
        )
        assert nonmulti["results"] == []
        multi = tool.call(
            pattern="start.*end",
            path=str(tmp_path),
            output_mode="files_with_matches",
            multiline=True,
        )
        assert multi["results"] == ["a.py"]

    def test_context_before_after(self, tmp_path: Path):
        _touch(
            tmp_path / "a.py",
            "line1\nline2\nMATCH\nline4\nline5\n",
        )
        tool = FileGrep(force_backend="python")
        result = tool.call(
            pattern="MATCH",
            path=str(tmp_path),
            output_mode="content",
            context_before=1,
            context_after=1,
        )
        texts = [entry["text"] for entry in result["results"]]
        assert "line2" in texts
        assert "MATCH" in texts
        assert "line4" in texts

    def test_head_limit_unlimited(self, tmp_path: Path):
        for i in range(30):
            _touch(tmp_path / f"f{i}.py", "needle\n")
        tool = FileGrep(force_backend="python")
        result = tool.call(
            pattern="needle",
            path=str(tmp_path),
            output_mode="files_with_matches",
            head_limit=0,
        )
        assert len(result["results"]) == 30
        assert result["truncated"] is False

    def test_head_limit_truncation(self, tmp_path: Path):
        for i in range(30):
            _touch(tmp_path / f"f{i:02d}.py", "needle\n")
        tool = FileGrep(force_backend="python")
        result = tool.call(
            pattern="needle",
            path=str(tmp_path),
            output_mode="files_with_matches",
            head_limit=5,
        )
        assert len(result["results"]) == 5
        assert result["total"] == 30
        assert result["truncated"] is True

    def test_excluded_dirs_not_searched(self, tmp_path: Path):
        _touch(tmp_path / "ok.py", "needle\n")
        _touch(tmp_path / "node_modules" / "lib.py", "needle\n")
        _touch(tmp_path / "__pycache__" / "x.py", "needle\n")
        tool = FileGrep(force_backend="python")
        result = tool.call(
            pattern="needle",
            path=str(tmp_path),
            output_mode="files_with_matches",
        )
        files = result["results"]
        assert "ok.py" in files
        assert not any("node_modules" in f for f in files)
        assert not any("__pycache__" in f for f in files)

    def test_backend_auto_detect_python_when_rg_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _touch(tmp_path / "a.py", "needle\n")
        monkeypatch.setattr(
            "concinno.tools.builtin.search.shutil.which",
            lambda _: None,
        )
        tool = FileGrep()
        result = tool.call(
            pattern="needle",
            path=str(tmp_path),
            output_mode="files_with_matches",
        )
        assert result["backend"] == "python"
        assert result["results"] == ["a.py"]

    def test_backend_rg_simulated_via_force(self, tmp_path: Path):
        _touch(tmp_path / "a.py", "needle\n")
        # Force rg backend so we exercise the rg path even if rg is
        # absent — it'll fall through to python on RuntimeError, but
        # the backend label we observe at the end reflects that.
        import shutil as _shutil

        if _shutil.which("rg") is None:
            tool = FileGrep(force_backend="rg")
            result = tool.call(
                pattern="needle",
                path=str(tmp_path),
                output_mode="files_with_matches",
            )
            # When rg isn't on PATH, _run_rg raises RuntimeError and we
            # transparently degrade to python.
            assert result["backend"] == "python"
            assert "a.py" in result["results"]
        else:
            tool = FileGrep(force_backend="rg")
            result = tool.call(
                pattern="needle",
                path=str(tmp_path),
                output_mode="files_with_matches",
            )
            assert result["backend"] == "rg"
            assert "a.py" in result["results"]

    def test_rg_subprocess_mocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _touch(tmp_path / "a.py", "needle\n")
        monkeypatch.setattr(
            "concinno.tools.builtin.search.shutil.which",
            lambda _: "/usr/bin/rg",
        )

        class FakeProc:
            returncode = 0
            stdout = f"{tmp_path / 'a.py'}\n"
            stderr = ""

        def fake_run(*_args: Any, **_kwargs: Any) -> Any:
            return FakeProc()

        monkeypatch.setattr(
            "concinno.tools.builtin.search.subprocess.run", fake_run
        )
        tool = FileGrep()
        result = tool.call(
            pattern="needle",
            path=str(tmp_path),
            output_mode="files_with_matches",
        )
        assert result["backend"] == "rg"
        assert result["results"] == ["a.py"]

    def test_rg_no_matches_exit_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            "concinno.tools.builtin.search.shutil.which",
            lambda _: "/usr/bin/rg",
        )

        class FakeProc:
            returncode = 1
            stdout = ""
            stderr = ""

        monkeypatch.setattr(
            "concinno.tools.builtin.search.subprocess.run",
            lambda *a, **k: FakeProc(),
        )
        tool = FileGrep()
        result = tool.call(
            pattern="nope",
            path=str(tmp_path),
            output_mode="files_with_matches",
        )
        assert result["backend"] == "rg"
        assert result["results"] == []
        assert result["total"] == 0

    def test_rg_count_mode_parsed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            "concinno.tools.builtin.search.shutil.which",
            lambda _: "/usr/bin/rg",
        )

        class FakeProc:
            returncode = 0
            stdout = f"{tmp_path / 'a.py'}:3\n{tmp_path / 'b.py'}:1\n"
            stderr = ""

        monkeypatch.setattr(
            "concinno.tools.builtin.search.subprocess.run",
            lambda *a, **k: FakeProc(),
        )
        tool = FileGrep()
        result = tool.call(
            pattern="x", path=str(tmp_path), output_mode="count"
        )
        counts = {e["file"]: e["count"] for e in result["results"]}
        assert counts == {"a.py": 3, "b.py": 1}

    def test_rg_content_mode_parsed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            "concinno.tools.builtin.search.shutil.which",
            lambda _: "/usr/bin/rg",
        )

        class FakeProc:
            returncode = 0
            stdout = f"{tmp_path / 'a.py'}:7:hello world\n"
            stderr = ""

        monkeypatch.setattr(
            "concinno.tools.builtin.search.subprocess.run",
            lambda *a, **k: FakeProc(),
        )
        tool = FileGrep()
        result = tool.call(
            pattern="hello", path=str(tmp_path), output_mode="content"
        )
        assert result["results"] == [
            {"file": "a.py", "line": 7, "text": "hello world"}
        ]

    def test_invalid_output_mode_raises(self, tmp_path: Path):
        with pytest.raises(ValueError, match="invalid output_mode"):
            FileGrep(force_backend="python").call(
                pattern="x",
                path=str(tmp_path),
                output_mode="bogus",  # type: ignore[arg-type]
            )

    def test_missing_pattern_raises(self, tmp_path: Path):
        with pytest.raises(ValueError, match="pattern is required"):
            FileGrep(force_backend="python").call(
                pattern="", path=str(tmp_path)
            )

    def test_concurrency_safe_flag(self):
        assert FileGrep.is_concurrency_safe is True
        assert FileGrep.name == "Grep"
