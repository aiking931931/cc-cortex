"""Tests for concinno.git_size_monitor — .git pack-size warning gate."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from concinno.git_size_monitor import (
    DEFAULT_WARN_GB,
    check_git_size,
    git_size_monitor_hook,
)


# ── Helpers ────────────────────────────────────────────────


def _make_git(
    project_dir: Path,
    *,
    pack_sizes_bytes: list[int],
) -> Path:
    """Create a synthetic .git/objects/pack/ with fake pack files.

    Writes sparse files (seek + truncate) so we can simulate multi-GB
    repos without actually allocating the blocks on disk.
    """
    git_dir = project_dir / ".git"
    pack_dir = git_dir / "objects" / "pack"
    pack_dir.mkdir(parents=True, exist_ok=True)
    for i, size in enumerate(pack_sizes_bytes):
        pack = pack_dir / f"pack-deadbeef{i:02d}.pack"
        with open(pack, "wb") as f:
            if size > 0:
                f.seek(size - 1)
                f.write(b"\0")
        # Index sibling — should be ignored by the monitor
        idx = pack_dir / f"pack-deadbeef{i:02d}.idx"
        idx.write_bytes(b"idx placeholder")
    return git_dir


# ── Unit tests ─────────────────────────────────────────────


class TestCheckGitSize:
    """Pack-size threshold behaviour."""

    def test_no_git_dir(self, tmp_path: Path):
        # No .git/ — monitor stays silent.
        assert check_git_size(str(tmp_path)) is None

    def test_empty_project_dir_arg(self):
        assert check_git_size("") is None

    def test_below_threshold(self, tmp_path: Path):
        # 10 MB pack, 5 GB threshold — silent.
        _make_git(tmp_path, pack_sizes_bytes=[10 * 1024 * 1024])
        assert check_git_size(str(tmp_path), warn_gb=5.0) is None

    def test_exactly_threshold_fires(self, tmp_path: Path):
        # Threshold uses `<` (size < threshold => silent), so size == threshold
        # must still fire. 2 MB pack with 2 MB threshold (= 2/1024 GB).
        size = 2 * 1024 * 1024  # 2 MB
        _make_git(tmp_path, pack_sizes_bytes=[size])
        threshold_gb = 2 / 1024  # exactly 2 MB in GB
        result = check_git_size(str(tmp_path), warn_gb=threshold_gb)
        assert result is not None

    def test_just_above_threshold(self, tmp_path: Path):
        # 3 MB pack vs 1 MB threshold (= 1/1024 GB) — fires.
        size = 3 * 1024 * 1024
        _make_git(tmp_path, pack_sizes_bytes=[size])
        threshold_gb = 1 / 1024
        result = check_git_size(str(tmp_path), warn_gb=threshold_gb)
        assert result is not None
        assert "exceeds" in result
        assert "git_size_monitor" in result

    def test_multiple_packs_summed(self, tmp_path: Path):
        # Three 1 MB packs = 3 MB > 2 MB threshold.
        sizes = [1024 * 1024] * 3
        _make_git(tmp_path, pack_sizes_bytes=sizes)
        threshold_gb = 2 / 1024  # 2 MB
        result = check_git_size(str(tmp_path), warn_gb=threshold_gb)
        assert result is not None

    def test_idx_siblings_ignored(self, tmp_path: Path):
        # Only `.pack` files count — `.idx` and other files must not inflate.
        _make_git(tmp_path, pack_sizes_bytes=[1024])
        other = tmp_path / ".git" / "objects" / "pack" / "pack-zz.bogus"
        # 5 MB bogus non-.pack — must be ignored.
        with open(other, "wb") as f:
            f.seek(5 * 1024 * 1024 - 1)
            f.write(b"\0")
        # Threshold 1 MB — if bogus counted, would fire. Real pack = 1 KB.
        threshold_gb = 1 / 1024
        assert check_git_size(str(tmp_path), warn_gb=threshold_gb) is None

    def test_env_override(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        size = 3 * 1024 * 1024  # 3 MB
        _make_git(tmp_path, pack_sizes_bytes=[size])
        # Default threshold (5 GB) — no warn.
        monkeypatch.delenv("CONCINNO_GIT_SIZE_WARN_GB", raising=False)
        assert check_git_size(str(tmp_path)) is None
        # Env lowers to 0.001 GB (~1 MB) -> warn.
        monkeypatch.setenv("CONCINNO_GIT_SIZE_WARN_GB", "0.001")
        result = check_git_size(str(tmp_path))
        assert result is not None
        assert "0.0 GB" in result  # formatted to one decimal

    def test_bad_env_falls_back_to_default(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        # Bogus env must not raise; default (5 GB) stands.
        monkeypatch.setenv("CONCINNO_GIT_SIZE_WARN_GB", "not-a-number")
        size = 10 * 1024 * 1024  # 10 MB — far below 5 GB
        _make_git(tmp_path, pack_sizes_bytes=[size])
        assert check_git_size(str(tmp_path)) is None

    def test_zero_or_negative_threshold_disables(self, tmp_path: Path):
        # 0 or negative threshold = disabled (no warnings).
        _make_git(tmp_path, pack_sizes_bytes=[10 * 1024 * 1024])
        assert check_git_size(str(tmp_path), warn_gb=0.0) is None
        assert check_git_size(str(tmp_path), warn_gb=-1.0) is None

    def test_worktree_gitdir_file(self, tmp_path: Path):
        # Worktree: .git is a file containing `gitdir: <path>`.
        real_git = tmp_path / "real_git"
        pack_dir = real_git / "objects" / "pack"
        pack_dir.mkdir(parents=True)
        pack = pack_dir / "pack-cafe.pack"
        with open(pack, "wb") as f:
            f.seek(3 * 1024 * 1024 - 1)  # 3 MB
            f.write(b"\0")

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / ".git").write_text(
            f"gitdir: {real_git}\n",
            encoding="utf-8",
        )
        threshold_gb = 1 / 1024  # 1 MB
        result = check_git_size(str(worktree), warn_gb=threshold_gb)
        assert result is not None


class TestHookEntry:
    """``git_size_monitor_hook`` is what on_stop actually calls."""

    def test_hook_uses_env_project_dir(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        _make_git(tmp_path, pack_sizes_bytes=[3 * 1024 * 1024])
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        monkeypatch.setenv("CONCINNO_GIT_SIZE_WARN_GB", "0.001")
        assert git_size_monitor_hook() is not None

    def test_hook_missing_project_dir(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        assert git_size_monitor_hook() is None

    def test_hook_never_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ):
        # Force a crash inside check_git_size (by swapping it for a
        # function that always raises). Hook must catch + return None.
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))

        def _boom(*_a, **_kw):
            raise RuntimeError("simulated probe failure")

        import concinno.git_size_monitor as mod

        monkeypatch.setattr(mod, "check_git_size", _boom)
        assert git_size_monitor_hook() is None

    def test_default_threshold_constant(self):
        # Regression — changing this silently would re-tune prod noise.
        assert DEFAULT_WARN_GB == pytest.approx(5.0)


# ── on_stop wiring smoke test ──────────────────────────────


class TestStopHookWiring:
    """Verify the stop hook registers git_size_monitor module."""

    def test_module_registered_in_pipeline(self):
        # Static source check — avoids needing a live stop hook to prove
        # the pipeline actually runs us.
        from concinno.hooks import on_stop as stop_mod

        src = Path(stop_mod.__file__).read_text(encoding="utf-8")
        # Registered as _StopModule in main().
        assert '_StopModule("git_size_monitor"' in src
        # Whitelisted for stderr emission inside _emit_stderr_outputs.
        # Find the whitelist tuple literal and assert our name is in it.
        emit_marker = "def _emit_stderr_outputs"
        emit_idx = src.find(emit_marker)
        assert emit_idx != -1, "expected _emit_stderr_outputs in on_stop"
        emit_body = src[emit_idx:]
        assert '"git_size_monitor"' in emit_body

    def test_builder_returns_callable(self):
        from concinno.hooks.on_stop import _build_git_size_monitor

        fn = _build_git_size_monitor()
        assert callable(fn)
        # Safe to invoke in empty env — fail-open returns None.
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        assert fn() is None
