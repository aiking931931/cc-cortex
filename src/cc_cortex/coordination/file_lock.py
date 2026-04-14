"""File-based JSON lock coordination strategy.

Wraps the battle-tested logic from ``cc_cortex.multi_instance`` behind the
``CoordinationStrategy`` interface so it can be swapped for an SDK-level
implementation later.

The underlying data lives in a JSON file on disk (the same ``instance_lock.json``
used today).  All public helpers in ``multi_instance`` are re-used — this class
is a thin adapter, *not* a rewrite.
"""

import json
import os
from datetime import datetime, timezone
from typing import Optional

from cc_cortex.core.notify import make_session_title
from cc_cortex.core.session import save_session_marker
from cc_cortex.multi_instance import (
    check_conflict as _check_conflict,
)
from cc_cortex.multi_instance import (
    clean_zombies as _clean_zombies,
)
from cc_cortex.multi_instance import (
    register_session as _register_session,
)
from cc_cortex.multi_instance import (
    remove_session as _remove_session,
)
from cc_cortex.multi_instance import (
    track_file as _track_file,
)

from ._os_lock import OSFileLock
from .base import (
    CoordinationStrategy,
    LockResult,
    RenameResult,
    SessionInfo,
    normalize_project,
    project_abbr,
)

_DEFAULT_LOCK_PATH = os.path.join(
    os.path.expanduser("~"), ".claude", "token_state", "instance_lock.json"
)


class FileLockStrategy(CoordinationStrategy):
    """Coordinate multiple Claude Code sessions via a shared JSON file.

    Parameters
    ----------
    lock_path : str | None
        Path to the JSON lock file.  Defaults to
        ``~/.claude/token_state/instance_lock.json``.
    marker_dir : str | None
        Directory for per-session marker files.  Defaults to
        ``~/.claude/token_state/``.
    """

    def __init__(
        self,
        lock_path: Optional[str] = None,
        marker_dir: Optional[str] = None,
        os_lock_timeout: float = 5.0,
    ) -> None:
        self.lock_path = lock_path or _DEFAULT_LOCK_PATH
        self.marker_dir = marker_dir or os.path.dirname(self.lock_path)
        # Sentinel file for OS-level advisory lock — sits beside the JSON but
        # is never replaced, so an fd held on it stays valid across writes.
        self._os_lock_path = self.lock_path + ".lockfile"
        self._os_lock_timeout = os_lock_timeout

    def _os_lock(self) -> OSFileLock:
        """Return a fresh OS-level lock context manager."""
        return OSFileLock(self._os_lock_path, timeout=self._os_lock_timeout)

    # ── helpers ────────────────────────────────────────────────────

    def _read_lock(self) -> dict:
        """Read the lock file, returning empty structure on any error."""
        try:
            with open(self.lock_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"sessions": {}}

    def _write_lock(self, data: dict) -> None:
        """Atomically write the lock file."""
        os.makedirs(os.path.dirname(self.lock_path), exist_ok=True)
        tmp = self.lock_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.lock_path)

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _session_key(self, session_id: str) -> str:
        return f"cc_{session_id[:8]}" if len(session_id) > 8 else f"cc_{session_id}"

    # ── CoordinationStrategy implementation ────────────────────────

    def register_session(self, info: SessionInfo) -> bool:
        with self._os_lock():
            lock = self._read_lock()
            key = self._session_key(info.session_id)
            now = self._now_iso()
            _register_session(
                lock, key, info.session_id, vscode_pid=info.pid, now=now
            )
            # Track initially declared files
            for fp in info.active_files:
                _track_file(lock, key, fp, now=now)
            # Auto-claim project if supplied and free (or already ours)
            proj = normalize_project(info.project)
            if proj:
                projects = lock.setdefault("projects", {})
                existing = projects.get(proj, {})
                holder = existing.get("holder", "")
                holder_alive = holder in lock.get("sessions", {})
                if not holder or holder == key or not holder_alive:
                    projects[proj] = {
                        "holder": key,
                        "session_id": info.session_id,
                        "acquired_at": now,
                    }
                    lock["sessions"][key]["project"] = proj
            self._write_lock(lock)
            return True

    def unregister_session(self, session_id: str) -> bool:
        with self._os_lock():
            lock = self._read_lock()
            key = self._session_key(session_id)
            if key not in lock.get("sessions", {}):
                return False
            # Release any project claims held by this session
            projects = lock.get("projects", {})
            to_drop = [
                p for p, rec in projects.items() if rec.get("holder") == key
            ]
            for p in to_drop:
                del projects[p]
            _remove_session(lock, key)
            self._write_lock(lock)
            return True

    def acquire_file_lock(self, session_id: str, file_path: str) -> LockResult:
        with self._os_lock():
            lock = self._read_lock()
            key = self._session_key(session_id)
            sessions = lock.get("sessions", {})

            # Must be registered first
            if key not in sessions:
                return LockResult(success=False, message="Session not registered")

            # Check for conflicts
            conflict = _check_conflict(sessions, key, file_path)
            if conflict:
                return LockResult(
                    success=False,
                    holder=conflict.get("key"),
                    message=f"File held by {conflict.get('holder', conflict['key'])}",
                )

            # Claim the file
            now = self._now_iso()
            _track_file(lock, key, file_path, now=now)
            self._write_lock(lock)
            return LockResult(success=True)

    def release_file_lock(self, session_id: str, file_path: str) -> bool:
        with self._os_lock():
            lock = self._read_lock()
            key = self._session_key(session_id)
            session = lock.get("sessions", {}).get(key)
            if not session:
                return False
            files = session.get("files", [])
            if file_path in files:
                files.remove(file_path)
                session.get("file_timestamps", {}).pop(file_path, None)
                self._write_lock(lock)
                return True
            return False

    def get_active_sessions(self) -> list[SessionInfo]:
        with self._os_lock():
            lock = self._read_lock()
        result: list[SessionInfo] = []
        for key, data in lock.get("sessions", {}).items():
            result.append(
                SessionInfo(
                    session_id=data.get("session_id", key),
                    pid=data.get("vscode_pid", 0),
                    started_at=data.get("started", ""),
                    active_files=list(data.get("files", [])),
                    project=data.get("project", ""),
                )
            )
        return result

    def cleanup_zombies(self, timeout_seconds: int = 1800) -> list[str]:
        with self._os_lock():
            lock = self._read_lock()
            removed = _clean_zombies(
                lock,
                threshold_sec=timeout_seconds,
                marker_dir=self.marker_dir,
            )
            # Also release project claims held by removed sessions
            if removed:
                projects = lock.get("projects", {})
                dead = set(removed)
                to_drop = [
                    p for p, rec in projects.items() if rec.get("holder") in dead
                ]
                for p in to_drop:
                    del projects[p]
                self._write_lock(lock)
            return removed

    def check_conflict(self, session_id: str, file_path: str) -> Optional[str]:
        with self._os_lock():
            lock = self._read_lock()
        key = self._session_key(session_id)
        sessions = lock.get("sessions", {})
        conflict = _check_conflict(sessions, key, file_path)
        if conflict:
            return conflict["key"]
        return None

    # ── Project-level lock ────────────────────────────────────────

    def acquire_project(self, session_id: str, project: str) -> LockResult:
        proj = normalize_project(project)
        if not proj:
            return LockResult(success=False, message="Empty project name")

        with self._os_lock():
            lock = self._read_lock()
            key = self._session_key(session_id)
            if key not in lock.get("sessions", {}):
                return LockResult(success=False, message="Session not registered")

            projects = lock.setdefault("projects", {})
            existing = projects.get(proj)
            if existing:
                holder = existing.get("holder", "")
                # Already ours — idempotent success
                if holder == key:
                    return LockResult(success=True, message="Already held")
                # Held by a live session — deny
                if holder and holder in lock.get("sessions", {}):
                    return LockResult(
                        success=False,
                        holder=holder,
                        message=(
                            f"Project '{proj}' held by {holder}"
                        ),
                    )
                # Stale holder (session gone) — fall through and take over

            now = self._now_iso()
            projects[proj] = {
                "holder": key,
                "session_id": session_id,
                "acquired_at": now,
            }
            lock["sessions"][key]["project"] = proj
            self._write_lock(lock)
            return LockResult(success=True)

    def release_project(self, session_id: str, project: str) -> bool:
        proj = normalize_project(project)
        if not proj:
            return False

        with self._os_lock():
            lock = self._read_lock()
            key = self._session_key(session_id)
            projects = lock.get("projects", {})
            rec = projects.get(proj)
            if not rec or rec.get("holder") != key:
                return False
            del projects[proj]
            session = lock.get("sessions", {}).get(key)
            if session and session.get("project") == proj:
                session["project"] = ""
            self._write_lock(lock)
            return True

    def check_project_conflict(
        self, session_id: str, project: str
    ) -> Optional[str]:
        proj = normalize_project(project)
        if not proj:
            return None
        with self._os_lock():
            lock = self._read_lock()
        key = self._session_key(session_id)
        rec = lock.get("projects", {}).get(proj)
        if not rec:
            return None
        holder = rec.get("holder", "")
        if not holder or holder == key:
            return None
        # Stale holder check — if holder session no longer exists, treat as free
        if holder not in lock.get("sessions", {}):
            return None
        return holder

    # ── Session rename (project-scoped key + marker + optional toast) ──

    def rename_session(
        self,
        session_id: str,
        project: str,
        task: str = "",
        abbr_overrides: Optional[dict] = None,
        notify: bool = False,
    ) -> RenameResult:
        """Rename the current lock entry to ``<ABBR>_<4hex>_<HHMM>``.

        Single TOCTOU-safe operation: renames the sessions-dict key,
        updates task/project fields, writes the session marker, and
        rolls any matching handoff_locks references over to the new key.
        """
        proj = normalize_project(project)
        # Generate the new title outside the OS lock — it's pure
        title = make_session_title(prefix=project_abbr(project, abbr_overrides))

        with self._os_lock():
            lock = self._read_lock()
            sessions = lock.setdefault("sessions", {})

            # Locate the existing entry by session_id field, falling back
            # to the short-key mapping we use for everything else.
            old_key = self._session_key(session_id)
            if old_key not in sessions:
                match = next(
                    (
                        k
                        for k, v in sessions.items()
                        if v.get("session_id") == session_id
                    ),
                    None,
                )
                if match is None:
                    return RenameResult(
                        success=False,
                        title=title,
                        message="Session not registered",
                    )
                old_key = match

            if title == old_key:
                # Idempotent — already named this; still update fields
                entry = sessions[old_key]
            else:
                entry = sessions.pop(old_key)
                sessions[title] = entry

            if task:
                entry["task"] = task
            if proj:
                entry["project"] = proj
                # Project lock follows the session — drop any claim that
                # was held by the old key and create one for the new key.
                projects = lock.setdefault("projects", {})
                to_drop = [
                    p
                    for p, rec in projects.items()
                    if rec.get("holder") in (old_key, title)
                ]
                for p in to_drop:
                    del projects[p]
                projects[proj] = {
                    "holder": title,
                    "session_id": session_id,
                    "acquired_at": self._now_iso(),
                }

            # Any handoff_locks that referenced the old key must follow.
            handoff_locks = lock.get("handoff_locks", {})
            for hl_val in handoff_locks.values():
                if isinstance(hl_val, dict) and hl_val.get("session") == old_key:
                    hl_val["session"] = title

            self._write_lock(lock)

        # Side-effects outside the OS lock: marker file + toast
        save_session_marker(
            session_id=session_id,
            marker_dir=self.marker_dir,
            session_name=title,
        )
        if notify:
            try:
                from cc_cortex.core.notify import show_toast

                show_toast(
                    title=title,
                    message=task or proj or "Session renamed",
                    tag="cc-cortex-rename",
                    group="cc-cortex",
                )
            except Exception:
                pass

        return RenameResult(
            success=True,
            old_key=old_key,
            new_key=title,
            title=title,
        )
