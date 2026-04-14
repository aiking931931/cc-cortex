"""Tests for BackupManager — unified naming, retention, rollback."""

from __future__ import annotations

import time
from pathlib import Path

from cc_cortex.backup_manager import _BACKUP_RE, BackupEntry, BackupManager, _now_stamp

# ── Helpers ──────────────────────────────────────────────


def _make_files(base: Path, names: list[str]) -> None:
    """Create dummy files in base directory."""
    base.mkdir(parents=True, exist_ok=True)
    for name in names:
        (base / name).write_text(f"content of {name}")


def _make_backup_dir(base: Path, scope: str, timestamp: str, desc: str) -> Path:
    """Create a fake backup directory with marker file."""
    d = base / f"backup_{scope}_{timestamp}_{desc}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "marker.txt").write_text("backup content")
    return d


# ── TestBackupEntry ──────────────────────────────────────


class TestBackupEntry:
    def test_name_property(self, tmp_path: Path):
        entry = BackupEntry(
            path=tmp_path / "backup_rules_20260321-1830_test",
            scope="rules",
            timestamp="20260321-1830",
            description="test",
        )
        assert entry.name == "backup_rules_20260321-1830_test"

    def test_repr(self, tmp_path: Path):
        entry = BackupEntry(
            path=tmp_path / "backup_rules_20260321-1830_test",
            scope="rules",
            timestamp="20260321-1830",
            description="test",
        )
        assert "backup_rules_20260321-1830_test" in repr(entry)


# ── TestNamingConvention ─────────────────────────────────


class TestNamingConvention:
    def test_regex_matches_valid(self):
        m = _BACKUP_RE.match("backup_rules_20260321-1830_pre-refactor")
        assert m is not None
        assert m.group(1) == "rules"
        assert m.group(2) == "20260321-1830"
        assert m.group(3) == "pre-refactor"

    def test_regex_rejects_invalid(self):
        assert _BACKUP_RE.match("not_a_backup") is None
        assert _BACKUP_RE.match("backup_") is None
        assert _BACKUP_RE.match("backup_rules_badtime_desc") is None

    def test_now_stamp_format(self):
        stamp = _now_stamp()
        assert len(stamp) == 13  # YYYYMMDD-HHMM
        assert stamp[8] == "-"
        assert stamp[:8].isdigit()
        assert stamp[9:].isdigit()

    def test_description_sanitization(self, tmp_path: Path):
        _make_files(tmp_path, ["a.txt"])
        mgr = BackupManager(base_dir=tmp_path, scope="test", keep=5)
        entry = mgr.create("hello world!@#")
        # Special chars should be replaced with hyphens
        assert "hello-world---" in entry.description or "hello-world" in entry.description
        assert entry.path.is_dir()


# ── TestCreate ───────────────────────────────────────────


class TestCreate:
    def test_creates_backup_dir(self, tmp_path: Path):
        _make_files(tmp_path, ["file1.md", "file2.md"])
        mgr = BackupManager(base_dir=tmp_path, scope="rules", keep=5)
        entry = mgr.create("test-backup")
        assert entry.path.is_dir()
        assert entry.scope == "rules"
        assert "test-backup" in entry.description

    def test_copies_files(self, tmp_path: Path):
        _make_files(tmp_path, ["a.md", "b.md"])
        mgr = BackupManager(base_dir=tmp_path, scope="rules", keep=5)
        entry = mgr.create("copy-test")
        assert (entry.path / "a.md").exists()
        assert (entry.path / "b.md").exists()
        assert (entry.path / "a.md").read_text() == "content of a.md"

    def test_does_not_copy_backup_dirs(self, tmp_path: Path):
        _make_files(tmp_path, ["file.md"])
        _make_backup_dir(tmp_path, "rules", "20260101-0000", "old")
        mgr = BackupManager(base_dir=tmp_path, scope="rules", keep=5)
        entry = mgr.create("new")
        # Should only have file.md, not the old backup's marker
        backed_up = [f.name for f in entry.path.iterdir()]
        assert "file.md" in backed_up
        assert "marker.txt" not in backed_up

    def test_auto_prunes_on_create(self, tmp_path: Path):
        _make_files(tmp_path, ["x.txt"])
        mgr = BackupManager(base_dir=tmp_path, scope="test", keep=2)
        mgr.create("first")
        time.sleep(0.1)  # ensure different timestamps if within same minute
        mgr.create("second")
        mgr.create("third")
        # Should keep only 2 newest
        backups = mgr.list_backups()
        assert len(backups) <= 2

    def test_scope_auto_detect(self, tmp_path: Path):
        sub = tmp_path / "my-rules"
        _make_files(sub, ["r.md"])
        mgr = BackupManager(base_dir=sub)
        assert mgr.scope == "my-rules"

    def test_empty_dir(self, tmp_path: Path):
        tmp_path.mkdir(exist_ok=True)
        mgr = BackupManager(base_dir=tmp_path, scope="empty", keep=5)
        entry = mgr.create("empty-test")
        assert entry.path.is_dir()
        # No files to copy, but dir exists
        files = [f for f in entry.path.iterdir() if f.is_file()]
        assert len(files) == 0


# ── TestListBackups ──────────────────────────────────────


class TestListBackups:
    def test_list_empty(self, tmp_path: Path):
        mgr = BackupManager(base_dir=tmp_path, scope="rules", keep=5)
        assert mgr.list_backups() == []

    def test_list_sorted_newest_first(self, tmp_path: Path):
        _make_backup_dir(tmp_path, "rules", "20260101-0000", "old")
        _make_backup_dir(tmp_path, "rules", "20260301-1200", "mid")
        _make_backup_dir(tmp_path, "rules", "20260321-1830", "new")
        mgr = BackupManager(base_dir=tmp_path, scope="rules", keep=5)
        backups = mgr.list_backups()
        assert len(backups) == 3
        assert backups[0].timestamp == "20260321-1830"
        assert backups[2].timestamp == "20260101-0000"

    def test_filters_by_scope(self, tmp_path: Path):
        _make_backup_dir(tmp_path, "rules", "20260101-0000", "a")
        _make_backup_dir(tmp_path, "skills", "20260101-0000", "b")
        mgr = BackupManager(base_dir=tmp_path, scope="rules", keep=5)
        backups = mgr.list_backups()
        assert len(backups) == 1
        assert backups[0].scope == "rules"

    def test_ignores_non_backup_dirs(self, tmp_path: Path):
        (tmp_path / "random_dir").mkdir()
        (tmp_path / "some_file.md").write_text("hi")
        _make_backup_dir(tmp_path, "rules", "20260101-0000", "real")
        mgr = BackupManager(base_dir=tmp_path, scope="rules", keep=5)
        backups = mgr.list_backups()
        assert len(backups) == 1

    def test_nonexistent_base_dir(self, tmp_path: Path):
        mgr = BackupManager(base_dir=tmp_path / "nope", scope="rules", keep=5)
        assert mgr.list_backups() == []


# ── TestRollback ─────────────────────────────────────────


class TestRollback:
    def test_rollback_latest(self, tmp_path: Path):
        _make_files(tmp_path, ["original.md"])
        mgr = BackupManager(base_dir=tmp_path, scope="test", keep=10)
        mgr.create("v1")

        # Modify current state
        (tmp_path / "original.md").write_text("modified")
        (tmp_path / "new_file.md").write_text("new")

        result = mgr.rollback()
        assert "error" not in result
        assert "original.md" in result["files_restored"]
        assert (tmp_path / "original.md").read_text() == "content of original.md"
        # new_file.md should be gone (only backup files restored)
        assert not (tmp_path / "new_file.md").exists()

    def test_rollback_creates_safety_backup(self, tmp_path: Path):
        _make_files(tmp_path, ["f.md"])
        mgr = BackupManager(base_dir=tmp_path, scope="test", keep=10)
        mgr.create("v1")
        result = mgr.rollback()
        assert "pre-rollback" in result["backup_before_rollback"]

    def test_rollback_by_target_name(self, tmp_path: Path):
        _make_files(tmp_path, ["f.md"])
        mgr = BackupManager(base_dir=tmp_path, scope="test", keep=10)
        entry1 = mgr.create("version-one")
        (tmp_path / "f.md").write_text("changed")
        mgr.create("version-two")

        result = mgr.rollback(target=entry1.timestamp)
        assert result["restored_from"] == entry1.name

    def test_rollback_target_not_found(self, tmp_path: Path):
        _make_files(tmp_path, ["f.md"])
        mgr = BackupManager(base_dir=tmp_path, scope="test", keep=10)
        mgr.create("v1")
        result = mgr.rollback(target="nonexistent")
        assert "error" in result

    def test_rollback_no_backups(self, tmp_path: Path):
        mgr = BackupManager(base_dir=tmp_path, scope="test", keep=5)
        result = mgr.rollback()
        assert "error" in result


# ── TestPrune ────────────────────────────────────────────


class TestPrune:
    def test_prune_keeps_newest(self, tmp_path: Path):
        _make_backup_dir(tmp_path, "rules", "20260101-0000", "oldest")
        _make_backup_dir(tmp_path, "rules", "20260201-0000", "mid")
        _make_backup_dir(tmp_path, "rules", "20260301-0000", "newest")
        mgr = BackupManager(base_dir=tmp_path, scope="rules", keep=2)
        deleted = mgr.prune()
        assert len(deleted) == 1
        assert "oldest" in deleted[0]
        # Verify only 2 remain
        assert len(mgr.list_backups()) == 2

    def test_prune_with_custom_keep(self, tmp_path: Path):
        for i in range(5):
            _make_backup_dir(tmp_path, "rules", f"2026010{i}-0000", f"v{i}")
        mgr = BackupManager(base_dir=tmp_path, scope="rules", keep=10)
        deleted = mgr.prune(keep=1)
        assert len(deleted) == 4
        assert len(mgr.list_backups()) == 1

    def test_prune_nothing_to_delete(self, tmp_path: Path):
        _make_backup_dir(tmp_path, "rules", "20260101-0000", "only")
        mgr = BackupManager(base_dir=tmp_path, scope="rules", keep=5)
        deleted = mgr.prune()
        assert deleted == []

    def test_prune_empty(self, tmp_path: Path):
        mgr = BackupManager(base_dir=tmp_path, scope="rules", keep=2)
        deleted = mgr.prune()
        assert deleted == []


# ── TestStatus ───────────────────────────────────────────


class TestStatus:
    def test_status_summary(self, tmp_path: Path):
        _make_backup_dir(tmp_path, "rules", "20260101-0000", "a")
        _make_backup_dir(tmp_path, "rules", "20260201-0000", "b")
        mgr = BackupManager(base_dir=tmp_path, scope="rules", keep=2)
        status = mgr.status()
        assert status["scope"] == "rules"
        assert status["total_backups"] == 2
        assert status["keep_policy"] == 2
        assert len(status["backups"]) == 2

    def test_status_empty(self, tmp_path: Path):
        mgr = BackupManager(base_dir=tmp_path, scope="empty", keep=2)
        status = mgr.status()
        assert status["total_backups"] == 0
        assert status["backups"] == []


# ── TestEndToEnd ─────────────────────────────────────────


class TestEndToEnd:
    def test_full_lifecycle(self, tmp_path: Path):
        """create → list → modify → rollback → verify → prune."""
        # Setup
        _make_files(tmp_path, ["config.yaml", "rules.md"])
        mgr = BackupManager(base_dir=tmp_path, scope="config", keep=3)

        # Create backup
        entry = mgr.create("initial")
        assert entry.path.is_dir()
        assert (entry.path / "config.yaml").exists()

        # Verify list
        backups = mgr.list_backups()
        assert len(backups) == 1

        # Modify files
        (tmp_path / "config.yaml").write_text("modified!")
        (tmp_path / "rules.md").unlink()

        # Rollback
        mgr.rollback()
        assert (tmp_path / "config.yaml").read_text() == "content of config.yaml"
        assert (tmp_path / "rules.md").read_text() == "content of rules.md"

        # Status should show backups (initial + pre-rollback)
        status = mgr.status()
        assert status["total_backups"] >= 2

        # Prune to 1
        mgr.prune(keep=1)
        assert len(mgr.list_backups()) == 1


# ── TestImport ───────────────────────────────────────────


class TestImport:
    def test_public_api_import(self):
        from cc_cortex import BackupManager as BM

        assert BM is BackupManager
