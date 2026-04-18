"""Backup Manager — unified naming, retention policy, and rollback.

Naming convention: ``backup_<scope>_<YYYYMMDD-HHMM>_<description>``
Retention: keep latest N versions (default 2), prune older ones on each backup.

Usage::

    from concinno.backup_manager import BackupManager

    mgr = BackupManager(base_dir=".claude/rules")
    mgr.create("pre-refactor")       # backup_rules_20260321-1830_pre-refactor/
    mgr.list_backups()                # sorted newest first
    mgr.rollback()                    # restore from latest backup
    mgr.prune(keep=2)                # delete all but newest 2
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from concinno.destruction_guard import destruction_gate

# UTC+8 for user's timezone
_TZ = timezone(timedelta(hours=8))

# Pattern: backup_<scope>_<YYYYMMDD-HHMM>_<description>
_BACKUP_RE = re.compile(r"^backup_(\w+)_(\d{8}-\d{4})_(.+)$")


def _now_stamp() -> str:
    """Current timestamp in YYYYMMDD-HHMM format (UTC+8)."""
    return datetime.now(_TZ).strftime("%Y%m%d-%H%M")


class BackupEntry:
    """Represents a single backup."""

    def __init__(self, path: Path, scope: str, timestamp: str, description: str):
        self.path = path
        self.scope = scope
        self.timestamp = timestamp
        self.description = description

    @property
    def name(self) -> str:
        return self.path.name

    def __repr__(self) -> str:
        return f"Backup({self.name})"


class BackupManager:
    """Manage backups with unified naming and retention policy.

    Parameters
    ----------
    base_dir : str | Path
        Directory containing the files to back up.
        Backups are stored as subdirectories within this directory.
    scope : str
        Scope label for backup names (e.g. "rules", "skills", "config").
        Auto-detected from directory name if not provided.
    keep : int
        Number of recent backups to retain. Older ones are pruned on each backup.
    """

    def __init__(
        self,
        base_dir: str | Path,
        scope: str = "",
        keep: int = 2,
    ):
        self.base_dir = Path(base_dir)
        self.scope = scope or self.base_dir.name
        self.keep = keep

    def create(self, description: str) -> BackupEntry:
        """Create a backup of all non-backup files in base_dir.

        Copies all files (not subdirectories that are themselves backups)
        into a new backup directory. Automatically prunes old backups.

        Returns the created BackupEntry.
        """
        import os as _os

        stamp = _now_stamp()
        safe_desc = re.sub(r"[^\w\-]", "-", description)
        backup_name = f"backup_{self.scope}_{stamp}_{safe_desc}"
        backup_path = self.base_dir / backup_name
        # 反熵優先: prune BEFORE creating new backup. The in-process
        # call path raises the destruction_gate escape flag so the
        # retention policy doesn't demand a reason kwarg on the normal
        # maintenance path.
        prev_flag = _os.environ.get("CONCINNO_BACKUP_PRUNE")
        prev_proj = _os.environ.get("CLAUDE_PROJECT_DIR")
        if not prev_proj:
            _os.environ["CLAUDE_PROJECT_DIR"] = _os.getcwd()
        _os.environ["CONCINNO_BACKUP_PRUNE"] = "1"
        try:
            self.prune(max(self.keep - 1, 1))
        finally:
            if prev_flag is None:
                _os.environ.pop("CONCINNO_BACKUP_PRUNE", None)
            else:
                _os.environ["CONCINNO_BACKUP_PRUNE"] = prev_flag
            if prev_proj is None:
                _os.environ.pop("CLAUDE_PROJECT_DIR", None)

        backup_path.mkdir(parents=True, exist_ok=True)

        # Copy non-backup files
        for item in sorted(self.base_dir.iterdir()):
            if item.name.startswith("backup_"):
                continue
            if item.is_file():
                shutil.copy2(item, backup_path / item.name)

        return BackupEntry(
            path=backup_path,
            scope=self.scope,
            timestamp=stamp,
            description=safe_desc,
        )

    def list_backups(self) -> list[BackupEntry]:
        """List all backups, newest first."""
        entries: list[BackupEntry] = []
        if not self.base_dir.is_dir():
            return entries
        for d in self.base_dir.iterdir():
            if not d.is_dir():
                continue
            m = _BACKUP_RE.match(d.name)
            if m and m.group(1) == self.scope:
                entries.append(BackupEntry(
                    path=d,
                    scope=m.group(1),
                    timestamp=m.group(2),
                    description=m.group(3),
                ))
        entries.sort(key=lambda e: e.timestamp, reverse=True)
        return entries

    def rollback(self, target: Optional[str] = None) -> dict:
        """Restore files from a backup.

        Parameters
        ----------
        target : str | None
            Backup name or timestamp to restore. None = latest.

        Returns
        -------
        dict with keys: restored_from, files_restored, backup_before_rollback
        """
        backups = self.list_backups()
        if not backups:
            return {"error": "No backups found"}

        # Find target
        entry: Optional[BackupEntry] = None
        if target:
            for b in backups:
                if target in b.name or target == b.timestamp:
                    entry = b
                    break
            if not entry:
                return {"error": f"Backup not found: {target}"}
        else:
            entry = backups[0]

        # Safety: backup current state before rollback
        safety = self.create("pre-rollback")

        # Restore: remove current non-backup files, copy from backup
        for item in sorted(self.base_dir.iterdir()):
            if item.name.startswith("backup_"):
                continue
            if item.is_file():
                item.unlink()

        files_restored = []
        for item in sorted(entry.path.iterdir()):
            if item.is_file():
                shutil.copy2(item, self.base_dir / item.name)
                files_restored.append(item.name)

        return {
            "restored_from": entry.name,
            "files_restored": files_restored,
            "backup_before_rollback": safety.name,
        }

    @destruction_gate(risk="R2", op_name="prune")
    def prune(self, keep: Optional[int] = None) -> list[str]:
        """Delete old backups, keeping only the newest `keep` versions.

        Returns list of deleted backup names.

        Gate: direct calls require ``reason=<keyword>``. In-process calls
        from :meth:`create` set ``CONCINNO_BACKUP_PRUNE=1`` so the
        back-entropy maintenance path passes through.
        """
        keep = keep if keep is not None else self.keep
        backups = self.list_backups()
        to_delete = backups[keep:]
        deleted = []
        for entry in to_delete:
            shutil.rmtree(entry.path, ignore_errors=True)
            deleted.append(entry.name)
        return deleted

    def status(self) -> dict:
        """Return backup status summary."""
        backups = self.list_backups()
        return {
            "scope": self.scope,
            "base_dir": str(self.base_dir),
            "total_backups": len(backups),
            "keep_policy": self.keep,
            "backups": [
                {"name": b.name, "timestamp": b.timestamp, "description": b.description}
                for b in backups
            ],
        }
