"""Tests for concinno.cleanup — workspace hygiene utilities."""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from concinno.cleanup import (
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

    def test_squash_ignores_dirty_submodule(self, tmp_path: Path):
        """Regression: nested repos kept squash aborting → .git bloat."""
        _init_git_repo(tmp_path, n_commits=8)
        # Simulate a nested repo (like .claude/skills/last30days) with
        # its own commit, registered as gitlink at top level, then
        # dirtied again. Top-level `git status` without
        # --ignore-submodules reports ` m nested_repo`, aborting squash.
        nested = tmp_path / "nested_repo"
        nested.mkdir()
        for cmd in (
            ["git", "init"],
            ["git", "config", "user.name", "test"],
            ["git", "config", "user.email", "test@test"],
        ):
            subprocess.run(cmd, cwd=str(nested), capture_output=True)
        (nested / "inner.txt").write_text("v1")
        subprocess.run(
            ["git", "add", "-A"], cwd=str(nested), capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "inner v1"],
            cwd=str(nested), capture_output=True,
        )
        # Register nested repo as gitlink at top level.
        subprocess.run(
            ["git", "add", "nested_repo"],
            cwd=str(tmp_path), capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "auto: add nested"],
            cwd=str(tmp_path), capture_output=True,
        )
        # Dirty the nested repo's working tree → top-level sees ` m nested_repo`.
        (nested / "inner.txt").write_text("v2-dirty")

        # Sanity: without --ignore-submodules status IS dirty at top level.
        out_no = subprocess.run(
            ["git", "status", "--short"],
            cwd=str(tmp_path), capture_output=True, encoding="utf-8",
        )
        out_yes = subprocess.run(
            ["git", "status", "--short", "--ignore-submodules=all"],
            cwd=str(tmp_path), capture_output=True, encoding="utf-8",
        )
        assert "nested_repo" in out_no.stdout
        assert out_yes.stdout.strip() == "", (
            f"flag did not suppress: {out_yes.stdout!r}"
        )

        result = squash_auto_commits(tmp_path, keep=3)
        assert result.error == "", f"squash aborted: {result.error}"
        assert result.items_cleaned > 0

    def test_squash_still_aborts_on_tracked_dirt(self, tmp_path: Path):
        """Real dirty tracked files must still block squash (rebase safety)."""
        _init_git_repo(tmp_path, n_commits=8)
        (tmp_path / "file0.txt").write_text("modified")
        result = squash_auto_commits(tmp_path, keep=3)
        assert "uncommitted changes" in result.error

    def test_squash_skips_when_outer_embeds_inner(self, tmp_path: Path):
        """2.9.0 治本 regression: outer repo that tracks paths inside an
        inner repo's working tree must refuse to squash.

        Scenario reproduction (ai-king embedding projects/concinno):
          outer .gitignore carve-out tracks projects/concinno/src/**
          inner projects/concinno/.git is a full repo
          outer squash without this guard = replays old inner snapshots =
          blows away inner WIP. This test verifies the guard trips.
        """
        # Scenario: outer tracks files that later get covered by an
        # inner .git. This reproduces ai-king embedding projects/concinno
        # where .gitignore has `!projects/concinno/` carve-out.
        path = tmp_path
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init"], cwd=str(path), capture_output=True)
        subprocess.run(
            ["git", "config", "user.name", "outer"],
            cwd=str(path), capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "outer@test"],
            cwd=str(path), capture_output=True,
        )
        # Seed the inner path as tracked BEFORE inner .git exists.
        inner_rel = Path("projects") / "concinno"
        (path / inner_rel).mkdir(parents=True)
        (path / inner_rel / "important.py").write_text("outer-seed")
        for i in range(8):
            (path / f"f{i}.txt").write_text(f"{i}")
            subprocess.run(
                ["git", "add", "-A"], cwd=str(path), capture_output=True
            )
            subprocess.run(
                ["git", "commit", "-m", f"auto: {i}"],
                cwd=str(path), capture_output=True,
            )

        # Now initialize inner repo — outer still tracks the file; inner
        # owns the working tree going forward.
        inner_dir = path / inner_rel
        for cmd in (
            ["git", "init"],
            ["git", "config", "user.name", "inner"],
            ["git", "config", "user.email", "inner@test"],
        ):
            subprocess.run(cmd, cwd=str(inner_dir), capture_output=True)
        (inner_dir / "important.py").write_text("inner-wip")
        subprocess.run(
            ["git", "add", "-A"], cwd=str(inner_dir), capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "inner v1"],
            cwd=str(inner_dir), capture_output=True,
        )
        # Discard the outer's dirty view so the dirty-tree guard doesn't
        # short-circuit ahead of the nested-repo guard. In the real
        # ai-king case, `auto_commit` commits the diff first, then calls
        # squash on a clean tree.
        subprocess.run(
            ["git", "checkout", "--", str(inner_rel / "important.py")],
            cwd=str(path), capture_output=True,
        )

        result = squash_auto_commits(path, keep=3)
        assert "nested repo" in result.error, (
            f"expected nested-repo guard to trip, got: {result.error!r}"
        )
        assert result.items_cleaned == 0

    def test_squash_nested_skip_bypass_env(
        self, tmp_path: Path, monkeypatch
    ):
        """CONCINNO_SKIP_NESTED_REPOS=0 bypasses the nested-repo guard.

        Only for operators who explicitly know the outer does not track
        inner paths (rare). Default behavior stays safe.
        """
        path = tmp_path
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init"], cwd=str(path), capture_output=True)
        subprocess.run(
            ["git", "config", "user.name", "o"],
            cwd=str(path), capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "o@t"],
            cwd=str(path), capture_output=True,
        )
        inner_rel = Path("projects") / "concinno"
        (path / inner_rel).mkdir(parents=True)
        (path / inner_rel / "x.py").write_text("seed")
        for i in range(8):
            (path / f"f{i}.txt").write_text(f"{i}")
            subprocess.run(
                ["git", "add", "-A"], cwd=str(path), capture_output=True
            )
            subprocess.run(
                ["git", "commit", "-m", f"auto: {i}"],
                cwd=str(path), capture_output=True,
            )
        inner_dir = path / inner_rel
        for cmd in (
            ["git", "init"],
            ["git", "config", "user.name", "i"],
            ["git", "config", "user.email", "i@t"],
        ):
            subprocess.run(cmd, cwd=str(inner_dir), capture_output=True)
        (inner_dir / "x.py").write_text("wip")
        subprocess.run(
            ["git", "add", "-A"], cwd=str(inner_dir), capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "v1"],
            cwd=str(inner_dir), capture_output=True,
        )
        subprocess.run(
            ["git", "checkout", "--", str(inner_rel / "x.py")],
            cwd=str(path), capture_output=True,
        )

        monkeypatch.setenv("CONCINNO_SKIP_NESTED_REPOS", "0")
        result = squash_auto_commits(path, keep=3)
        # With bypass, guard is off. Whether the squash itself
        # succeeds depends on downstream git state — we only assert
        # the nested-repo guard did NOT trip.
        assert "nested repo" not in result.error

    def test_detect_embedded_nested_repos_clean_tree(
        self, tmp_path: Path
    ):
        """detect helper returns [] for a repo with no nested .git."""
        from concinno.cleanup import _detect_embedded_nested_repos
        _init_git_repo(tmp_path, n_commits=3)
        assert _detect_embedded_nested_repos(str(tmp_path)) == []

    def test_detect_embedded_nested_repos_submodule_gitlink_ok(
        self, tmp_path: Path
    ):
        """Submodule gitlinks (where .git is a *file* not dir) are safe —
        outer only stores commit SHA, rebase can't overwrite inner tree."""
        from concinno.cleanup import _detect_embedded_nested_repos
        _init_git_repo(tmp_path, n_commits=3)
        # Simulate a submodule's .git *file* (not directory)
        (tmp_path / "submod").mkdir()
        (tmp_path / "submod" / ".git").write_text(
            "gitdir: ../.git/modules/submod\n"
        )
        # Outer does not track anything inside submod/
        assert _detect_embedded_nested_repos(str(tmp_path)) == []

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
