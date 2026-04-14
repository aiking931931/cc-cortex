"""Tests for cc_cortex.cleanup — workspace hygiene utilities."""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cc_cortex.cleanup import (
    CleanupResult,
    archive_dead_handoffs,
    cleanup_stale_files,
    count_auto_commits,
    detect_dead_handoffs,
    detect_large_git_objects,
    git_gc,
    rotate_log_files,
    run_cleanup,
    squash_auto_commits,
)

_TZ = timezone(timedelta(hours=8))


# ── Helpers ──────────────────────────────────────────────


def _make_handoff(
    base: Path,
    name: str,
    *,
    has_pending: bool = False,
    has_star: bool = False,
    age_days: int = 0,
) -> Path:
    """Create a fake handoff file."""
    base.mkdir(parents=True, exist_ok=True)
    fp = base / name
    updated = datetime.now(_TZ) - timedelta(days=age_days)
    lines = [
        "---",
        "status: active",
        f"last_updated: {updated.strftime('%Y-%m-%d')}",
        "---",
        "",
        "# Handoff",
    ]
    if has_pending:
        lines.append("- ⬜ some pending task")
    if has_star:
        lines.append("- ★ permanent milestone")
    lines.append("- ✅ completed task")
    fp.write_text("\n".join(lines), encoding="utf-8")
    return fp


def _init_git_repo(path: Path, n_commits: int = 5) -> None:
    """Create a temp git repo with n auto-commits."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init"], cwd=str(path), capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=str(path), capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test"],
        cwd=str(path), capture_output=True,
    )
    for i in range(n_commits):
        (path / f"file{i}.txt").write_text(f"content {i}")
        subprocess.run(
            ["git", "add", "-A"], cwd=str(path), capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", f"auto: update {i}"],
            cwd=str(path), capture_output=True,
        )


# ── Dead handoff detection ───────────────────────────────


class TestDeadHandoffs:
    def test_detects_dead_handoff(self, tmp_path: Path):
        _make_handoff(tmp_path, "交接_old.md", age_days=45)
        result = detect_dead_handoffs(tmp_path, max_age_days=30)
        assert len(result) == 1
        assert result[0].name == "交接_old.md"

    def test_keeps_handoff_with_pending(self, tmp_path: Path):
        _make_handoff(
            tmp_path, "交接_active.md",
            has_pending=True, age_days=45,
        )
        result = detect_dead_handoffs(tmp_path, max_age_days=30)
        assert len(result) == 0

    def test_keeps_handoff_with_star(self, tmp_path: Path):
        _make_handoff(
            tmp_path, "交接_star.md",
            has_star=True, age_days=45,
        )
        result = detect_dead_handoffs(tmp_path, max_age_days=30)
        assert len(result) == 0

    def test_keeps_recent_handoff(self, tmp_path: Path):
        _make_handoff(tmp_path, "交接_new.md", age_days=5)
        result = detect_dead_handoffs(tmp_path, max_age_days=30)
        assert len(result) == 0

    def test_skips_archive_dir(self, tmp_path: Path):
        archive = tmp_path / "_archive"
        _make_handoff(archive, "交接_archived.md", age_days=45)
        result = detect_dead_handoffs(tmp_path, max_age_days=30)
        assert len(result) == 0

    def test_archive_moves_to_subdir(self, tmp_path: Path):
        _make_handoff(tmp_path, "交接_dead.md", age_days=45)
        result = archive_dead_handoffs(tmp_path, max_age_days=30)
        assert result.items_cleaned == 1
        assert (tmp_path / "_archive" / "交接_dead.md").exists()
        assert not (tmp_path / "交接_dead.md").exists()

    def test_archive_dry_run(self, tmp_path: Path):
        _make_handoff(tmp_path, "交接_dead.md", age_days=45)
        result = archive_dead_handoffs(
            tmp_path, max_age_days=30, dry_run=True,
        )
        assert result.items_found == 1
        assert result.items_cleaned == 0
        assert (tmp_path / "交接_dead.md").exists()

    def test_empty_dir(self, tmp_path: Path):
        result = detect_dead_handoffs(tmp_path / "nonexistent")
        assert result == []


# ── Stale file cleanup ───────────────────────────────────


class TestStaleFiles:
    def test_removes_old_temp(self, tmp_path: Path):
        old = tmp_path / "_temp_old"
        old.mkdir()
        (old / "f.txt").write_text("x")
        # Set mtime to 10 days ago
        import os
        old_time = (datetime.now() - timedelta(days=10)).timestamp()
        os.utime(old, (old_time, old_time))

        result = cleanup_stale_files(
            tmp_path, patterns=["_temp_*"], max_age_days=7,
        )
        assert result.items_cleaned == 1
        assert not old.exists()

    def test_keeps_recent_temp(self, tmp_path: Path):
        recent = tmp_path / "_temp_new"
        recent.mkdir()
        result = cleanup_stale_files(
            tmp_path, patterns=["_temp_*"], max_age_days=7,
        )
        assert result.items_cleaned == 0
        assert recent.exists()

    def test_dry_run(self, tmp_path: Path):
        old = tmp_path / "test.bak"
        old.write_text("x")
        import os
        old_time = (datetime.now() - timedelta(days=10)).timestamp()
        os.utime(old, (old_time, old_time))

        result = cleanup_stale_files(
            tmp_path, patterns=["*.bak"], max_age_days=7, dry_run=True,
        )
        assert result.items_found == 1
        assert result.items_cleaned == 0
        assert old.exists()


# ── Log rotation ─────────────────────────────────────────


class TestLogRotation:
    def test_rotates_large_log(self, tmp_path: Path):
        log = tmp_path / "test.log"
        lines = [f"line {i}" for i in range(600)]
        log.write_text("\n".join(lines), encoding="utf-8")

        result = rotate_log_files(
            tmp_path, max_lines=500, keep_lines=200,
        )
        assert result.items_cleaned == 1
        content = log.read_text(encoding="utf-8")
        # Should have header + 200 lines
        assert "rotated" in content
        assert len(content.splitlines()) <= 202

    def test_keeps_small_log(self, tmp_path: Path):
        log = tmp_path / "small.log"
        log.write_text("just a few lines\n" * 10, encoding="utf-8")

        result = rotate_log_files(tmp_path, max_lines=500)
        assert result.items_found == 0


# ── Git operations ───────────────────────────────────────


class TestGitOperations:
    def test_count_auto_commits(self, tmp_path: Path):
        _init_git_repo(tmp_path, n_commits=5)
        count = count_auto_commits(tmp_path, pattern="auto:")
        assert count == 5

    def test_squash_keeps_n(self, tmp_path: Path):
        _init_git_repo(tmp_path, n_commits=8)
        result = squash_auto_commits(tmp_path, keep=3)
        assert result.items_cleaned == 5
        # Verify only keep+1 commits remain (3 kept + 1 archive)
        out = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=str(tmp_path), capture_output=True,
            encoding="utf-8",
        )
        assert int(out.stdout.strip()) == 4  # 3 kept + 1 archive

    def test_squash_noop_when_few(self, tmp_path: Path):
        _init_git_repo(tmp_path, n_commits=2)
        result = squash_auto_commits(tmp_path, keep=3)
        assert result.items_found == 0

    def test_squash_dry_run(self, tmp_path: Path):
        _init_git_repo(tmp_path, n_commits=8)
        result = squash_auto_commits(tmp_path, keep=3, dry_run=True)
        assert result.items_found == 5
        assert result.items_cleaned == 0
        # Commits still intact
        out = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=str(tmp_path), capture_output=True,
            encoding="utf-8",
        )
        assert int(out.stdout.strip()) == 8

    def test_git_gc(self, tmp_path: Path):
        _init_git_repo(tmp_path, n_commits=3)
        result = git_gc(tmp_path)
        assert result.action == "git_gc"
        assert "before:" in result.details[0]

    def test_detect_large_objects_empty(self, tmp_path: Path):
        _init_git_repo(tmp_path, n_commits=1)
        large = detect_large_git_objects(tmp_path, min_size_mb=10)
        assert large == []


# ── Orchestrator ─────────────────────────────────────────


class TestRunCleanup:
    def test_runs_safe_ops(self, tmp_path: Path):
        _init_git_repo(tmp_path, n_commits=3)
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "test.log").write_text("x\n" * 10)

        results = run_cleanup(
            repo_dir=tmp_path,
            log_dir=log_dir,
            squash_git=False,
        )
        assert len(results) >= 2  # stale files + git gc at minimum
        assert all(isinstance(r, CleanupResult) for r in results)

    def test_dry_run_no_changes(self, tmp_path: Path):
        _init_git_repo(tmp_path, n_commits=3)
        results = run_cleanup(repo_dir=tmp_path, dry_run=True)
        for r in results:
            if r.action != "git_gc":  # gc always runs
                assert r.items_cleaned == 0


# ── CleanupResult ────────────────────────────────────────


class TestCleanupResult:
    def test_defaults(self):
        r = CleanupResult(action="test")
        assert r.items_found == 0
        assert r.items_cleaned == 0
        assert r.bytes_freed == 0
        assert r.details == []
        assert r.error == ""
