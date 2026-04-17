"""concinno.file_tracker — File tracking, conflict detection, locking, and zombie cleanup.

@module file_tracker
@responsibility Multi-session file coordination via instance_lock.json: atomic locking,
    per-session tracking with auto-release, TTL-based handoff write locks,
    zombie session cleanup, and session marker files.
@dependencies concinno.core.path_utils, concinno.guards.base
@exports FileTracker, FileTrackerGuard
"""

import json
import os
import time
from datetime import datetime
from typing import Any, Optional

from concinno.core.path_utils import extract_file_path
from concinno.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult


class FileTracker:
    """Stateless file tracking engine. All state lives in instance_lock.json + marker files."""

    def __init__(
        self,
        workspace: str,
        lock_path: str,
        marker_dir: str,
        *,
        zombie_threshold_sec: int = 1800,
        zombie_fast_sec: int = 600,
        file_stale_sec: int = 300,
        max_files: int = 30,
        handoff_lock_ttl: int = 15,
        shared_basenames: frozenset = frozenset(),
        shared_paths: frozenset = frozenset(),
        handoff_prefixes: tuple = (),
        write_tools: frozenset = frozenset(["Write", "Edit", "NotebookEdit"]),
        tz: Any = None,
    ):
        self.workspace = os.path.normpath(workspace)
        self.lock_path = lock_path
        self.lock_file = lock_path + ".lock"
        self.marker_dir = marker_dir
        self.zombie_threshold_sec = zombie_threshold_sec
        self.zombie_fast_sec = zombie_fast_sec
        self.file_stale_sec = file_stale_sec
        self.max_files = max_files
        self.handoff_lock_ttl = handoff_lock_ttl
        self.shared_basenames = shared_basenames
        self.shared_paths = shared_paths
        self.handoff_prefixes = handoff_prefixes
        self.write_tools = write_tools
        self.handoff_written_dir = os.path.expanduser("~/.claude/handoff_markers")
        if tz is None:
            from datetime import timedelta
            from datetime import timezone as _tz
            self.tz = _tz(timedelta(hours=8))
        else:
            self.tz = tz

    # ── Public API ────────────────────────────────────────

    def process(
        self,
        session_id: str,
        tool_name: str,
        tool_input: dict,
        vscode_pid: int = 0,
        find_cli_pid_fn: Any = None,
    ) -> dict:
        """Main entry point. Returns a result dict with keys:
        - "deny": present if operation should be denied (contains "reason" + "additional")
        - "session_key": the resolved session key
        - "lock_data": the current lock data (for downstream use)
        - "is_new_session": bool
        - "normalized": normalized file path or None
        """
        now = datetime.now(self.tz).isoformat()
        key = self._resolve_session_key(session_id)
        file_path = extract_file_path(tool_input) or None
        normalized = self.normalize_path(file_path) if file_path else None

        got_lock = self._acquire_file_lock()
        if not got_lock:
            # Can't get lock — still do read-only conflict check
            deny = self._readonly_conflict_check(key, normalized, tool_name)
            if deny:
                return {"deny": deny, "session_key": key, "lock_data": None,
                        "is_new_session": False, "normalized": normalized}
            return {"session_key": key, "lock_data": None,
                    "is_new_session": False, "normalized": normalized}

        conflict = None
        lock_data = None
        is_new = False
        try:
            lock_data = self._read_lock()
            self._clean_zombies(lock_data)
            sessions = lock_data.setdefault("sessions", {})

            is_new = key not in sessions
            if key in sessions:
                sessions[key]["last_active"] = now
                if not sessions[key].get("cli_pid") and find_cli_pid_fn:
                    try:
                        cli_pid = find_cli_pid_fn()
                        if cli_pid:
                            sessions[key]["cli_pid"] = cli_pid
                    except Exception:
                        pass
            else:
                cli_pid = 0
                if find_cli_pid_fn:
                    try:
                        cli_pid = find_cli_pid_fn() or 0
                    except Exception:
                        pass
                sessions[key] = {
                    "session_id": session_id,
                    "vscode_pid": vscode_pid,
                    "cli_pid": cli_pid,
                    "holder": "",
                    "project": "",
                    "task": "",
                    "files": [],
                    "started": now,
                    "last_active": now,
                }

            self._clean_handoff_locks(lock_data)

            # Conflict check (3 categories: handoff / shared / normal)
            if normalized and tool_name in self.write_tools:
                if self.is_handoff(normalized):
                    conflict = self._check_handoff_write_lock(lock_data, key, normalized)
                    if not conflict:
                        self._set_handoff_write_lock(lock_data, key, normalized, now)
                        self._mark_handoff_written(session_id)
                elif self.is_shared(normalized):
                    pass  # shared files: no conflict check
                else:
                    conflict = self._check_conflict(sessions, key, normalized)

            # Auto-track files for write operations (normal files only, no conflict)
            if normalized and tool_name in self.write_tools and not conflict:
                if not self.is_shared(normalized) and not self.is_handoff(normalized):
                    files_list = sessions[key]["files"]
                    file_ts = sessions[key].setdefault("file_timestamps", {})
                    if normalized not in files_list:
                        files_list.append(normalized)
                        if len(files_list) > self.max_files:
                            removed = files_list[:-self.max_files]
                            sessions[key]["files"] = files_list[-self.max_files:]
                            for r in removed:
                                file_ts.pop(r, None)
                    file_ts[normalized] = now

            lock_data["protocol_version"] = "4.1"
            lock_data["last_updated"] = now
            self._write_lock(lock_data)
        except Exception:
            pass
        finally:
            self._release_file_lock()

        # Write marker file
        self._write_marker(session_id, key, vscode_pid)

        if conflict:
            deny = self._format_conflict_deny(conflict, normalized)
            return {"deny": deny, "session_key": key, "lock_data": lock_data,
                    "is_new_session": is_new, "normalized": normalized}

        return {"session_key": key, "lock_data": lock_data,
                "is_new_session": is_new, "normalized": normalized}

    # ── File Path Utilities ───────────────────────────────

    def normalize_path(self, file_path: str) -> Optional[str]:
        """Normalize absolute path to relative path from workspace root."""
        if not file_path:
            return None
        try:
            normalized = os.path.normpath(file_path)
            if normalized.lower().startswith(self.workspace.lower()):
                rel = normalized[len(self.workspace):].lstrip(os.sep)
                return rel.replace("\\", "/")
        except Exception:
            pass
        return None

    def is_shared(self, normalized_path: str) -> bool:
        """Check if file is in the shared (no-conflict) list."""
        if not normalized_path:
            return False
        basename = os.path.basename(normalized_path)
        return basename in self.shared_basenames or normalized_path in self.shared_paths

    def is_handoff(self, normalized_path: str) -> bool:
        """Check if file is a handoff file (sequential-shared with short-lived lock)."""
        if not normalized_path:
            return False
        basename = os.path.basename(normalized_path)
        return any(basename.startswith(p) for p in self.handoff_prefixes)

    # ── Session Key Resolution ────────────────────────────

    def _resolve_session_key(self, session_id: str) -> str:
        """Resolve session key from marker file or fallback to cc_ prefix."""
        s_name = ""
        try:
            marker = os.path.join(self.marker_dir, f"{session_id}.session_name")
            if os.path.isfile(marker):
                with open(marker, "r", encoding="utf-8") as f:
                    s_name = f.read().strip()
        except Exception:
            pass
        return s_name if s_name else f"cc_{session_id[:8]}"

    # ── File Locking (atomic lock file) ───────────────────

    def _acquire_file_lock(self, timeout: float = 3.0) -> bool:
        """Acquire exclusive lock using atomic file creation (O_CREAT|O_EXCL)."""
        os.makedirs(os.path.dirname(self.lock_file), exist_ok=True)
        deadline = time.time() + timeout
        while True:
            try:
                fd = os.open(self.lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return True
            except FileExistsError:
                try:
                    age = time.time() - os.path.getmtime(self.lock_file)
                    if age > 10:
                        os.remove(self.lock_file)
                        continue
                except Exception:
                    pass
                if time.time() >= deadline:
                    try:
                        os.remove(self.lock_file)
                    except Exception:
                        pass
                    return False
                time.sleep(0.02)
            except Exception:
                return False

    def _release_file_lock(self):
        """Release the file lock by removing the lock file."""
        try:
            os.remove(self.lock_file)
        except Exception:
            pass

    # ── Lock File I/O ─────────────────────────────────────

    def _read_lock(self) -> dict:
        """Read instance_lock.json. Returns default dict if missing/corrupted."""
        try:
            with open(self.lock_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"protocol_version": "4.0", "sessions": {}}

    def _write_lock(self, lock: dict):
        """Write instance_lock.json atomically (tmp + replace)."""
        try:
            os.makedirs(os.path.dirname(self.lock_path), exist_ok=True)
            tmp_path = self.lock_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(lock, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, self.lock_path)
        except Exception:
            try:
                with open(self.lock_path, "w", encoding="utf-8") as f:
                    json.dump(lock, f, indent=2, ensure_ascii=False)
            except Exception:
                pass

    # ── Zombie Cleanup ────────────────────────────────────

    def _is_abcd_mode(self, lock: dict) -> bool:
        """Check if ABCD parallel mode is active (3+ active sessions)."""
        sessions = lock.get("sessions", {})
        active = sum(1 for s in sessions.values() if s.get("files"))
        return active >= 3

    def _is_session_alive(self, session_id: str) -> bool:
        """Check if session marker file exists."""
        marker_path = os.path.join(self.marker_dir, f"{session_id}.active")
        return os.path.isfile(marker_path)

    def _clean_zombies(self, lock: dict):
        """Remove sessions with last_active > threshold OR whose marker file is gone."""
        threshold = self.zombie_fast_sec if self._is_abcd_mode(lock) else self.zombie_threshold_sec
        sessions = lock.get("sessions", {})
        now_ts = time.time()
        to_remove = []
        for key, session in sessions.items():
            try:
                last = datetime.fromisoformat(session["last_active"])
                age_sec = now_ts - last.timestamp()
                sid = session.get("session_id", "")
                marker_alive = self._is_session_alive(sid)
                if marker_alive:
                    if age_sec > threshold * 2:
                        to_remove.append(key)
                else:
                    if age_sec > 10:
                        to_remove.append(key)
            except Exception:
                to_remove.append(key)
        for key in to_remove:
            del sessions[key]

    # ── Conflict Detection ────────────────────────────────

    def _check_conflict(self, sessions: dict, my_key: str, target_path: str) -> Optional[dict]:
        """Check if target file conflicts with another active session's files."""
        now_ts = time.time()
        dead_keys = []
        for key, session in sessions.items():
            if key == my_key:
                continue
            file_ts = session.get("file_timestamps", {})
            sid = session.get("session_id", "")
            if not self._is_session_alive(sid):
                dead_keys.append(key)
                continue
            for f in session.get("files", []):
                if self._paths_conflict(f, target_path):
                    ts_str = file_ts.get(f)
                    if ts_str:
                        try:
                            last_edit = datetime.fromisoformat(ts_str)
                            age = now_ts - last_edit.timestamp()
                            if age > self.file_stale_sec:
                                session["files"].remove(f)
                                file_ts.pop(f, None)
                                continue
                        except Exception:
                            pass
                    return {
                        "key": key,
                        "holder": session.get("holder", ""),
                        "task": session.get("task", ""),
                    }
        for key in dead_keys:
            del sessions[key]
        return None

    @staticmethod
    def _paths_conflict(declared: str, target: str) -> bool:
        """Check if two file paths conflict (exact match or glob patterns)."""
        if declared == target:
            return True
        if declared.endswith("/*"):
            if target.startswith(declared[:-1]):
                return True
        if target.endswith("/*"):
            if declared.startswith(target[:-1]):
                return True
        return False

    # ── Handoff File Locks ────────────────────────────────

    def _check_handoff_write_lock(
        self, lock_data: dict, my_key: str, target_path: str,
    ) -> Optional[dict]:
        """Check if a handoff file has an active write lock from another session."""
        handoff_locks = lock_data.get("handoff_locks", {})
        if target_path not in handoff_locks:
            return None
        entry = handoff_locks[target_path]
        if entry.get("session") == my_key:
            return None
        try:
            last_write = datetime.fromisoformat(entry["last_write"])
            age = time.time() - last_write.timestamp()
            if age > self.handoff_lock_ttl:
                return None
        except Exception:
            return None
        return {
            "key": entry.get("session", "unknown"),
            "holder": "",
            "task": f"Writing {os.path.basename(target_path)}",
            "is_handoff": True,
        }

    def _set_handoff_write_lock(self, lock_data: dict, my_key: str, target_path: str, now: str):
        """Set or renew a short-lived write lock for a handoff file."""
        handoff_locks = lock_data.setdefault("handoff_locks", {})
        handoff_locks[target_path] = {"session": my_key, "last_write": now}

    def _mark_handoff_written(self, session_id: str):
        """Mark that this session wrote a handoff file (for on-stop.py detection)."""
        try:
            os.makedirs(self.handoff_written_dir, exist_ok=True)
            marker = os.path.join(self.handoff_written_dir, f"{session_id[:8]}.wrote")
            with open(marker, "w") as f:
                f.write("1")
        except Exception:
            pass

    def _clean_handoff_locks(self, lock_data: dict):
        """Remove expired handoff write locks (4x TTL)."""
        handoff_locks = lock_data.get("handoff_locks", {})
        if not handoff_locks:
            return
        to_remove = []
        for path, entry in handoff_locks.items():
            try:
                last_write = datetime.fromisoformat(entry["last_write"])
                age = time.time() - last_write.timestamp()
                if age > self.handoff_lock_ttl * 4:
                    to_remove.append(path)
            except Exception:
                to_remove.append(path)
        for path in to_remove:
            del handoff_locks[path]

    # ── Marker Files ──────────────────────────────────────

    def _write_marker(self, session_id: str, lock_key: str, pid: int):
        """Write marker file so Claude can find its own session."""
        try:
            os.makedirs(self.marker_dir, exist_ok=True)
            marker_path = os.path.join(self.marker_dir, f"{session_id}.active")
            with open(marker_path, "w", encoding="utf-8") as f:
                json.dump({
                    "session_id": session_id,
                    "lock_key": lock_key,
                    "vscode_pid": pid,
                    "updated": datetime.now(self.tz).isoformat(),
                }, f)
        except Exception:
            pass

    # ── Read-only Conflict Check (when lock not acquired) ─

    def _readonly_conflict_check(
        self, key: str, normalized: Optional[str], tool_name: str,
    ) -> Optional[dict]:
        """Read-only conflict check when file lock can't be acquired."""
        if not normalized or tool_name not in self.write_tools:
            return None
        if self.is_shared(normalized) or self.is_handoff(normalized):
            return None
        try:
            lock_data = self._read_lock()
            sessions = lock_data.get("sessions", {})
            conflict = self._check_conflict(sessions, key, normalized)
            if conflict:
                return {
                    "reason": (
                        f"\u26a0 File conflict (read-only): "
                        f"{normalized} in use by {conflict['key']}."
                    ),
                    "additional": (
                        "Lock contention prevented tracking update, "
                        "but conflict check still works. "
                        "Please work on other files."
                    ),
                }
        except Exception:
            pass
        return None

    # ── Query API ─────────────────────────────────────────

    def get_session_file_info(
        self,
        session_id: str,
    ) -> dict:
        """Read current session's file tracking info from instance_lock.json.

        Returns dict with:
          - files: list[str] — tracked file paths
          - count: int — number of tracked files
          - handoff_written: bool — whether a handoff marker exists
        """
        key = self._resolve_session_key(session_id)
        try:
            lock_data = self._read_lock()
            sessions = lock_data.get("sessions", {})
            session = sessions.get(key, {})
            files = session.get("files", [])
        except Exception:
            files = []

        handoff_written = False
        try:
            marker = os.path.join(
                self.handoff_written_dir, f"{session_id[:8]}.wrote",
            )
            handoff_written = os.path.isfile(marker)
        except Exception:
            pass

        return {
            "files": files,
            "count": len(files),
            "handoff_written": handoff_written,
        }

    # ── Deny Formatting ───────────────────────────────────

    def _format_conflict_deny(self, conflict: dict, normalized: Optional[str]) -> dict:
        """Format a conflict into a deny response dict."""
        is_handoff_conflict = conflict.get("is_handoff", False)
        if is_handoff_conflict:
            return {
                "reason": (
                    f"\u26a0 Handoff queued: {normalized} being written by "
                    f"{conflict['key']} ({self.handoff_lock_ttl}s short lock). Retry shortly."
                ),
                "additional": (
                    f"Another session is updating this handoff file. "
                    f"Handoff files use a {self.handoff_lock_ttl}s short write lock "
                    "\u2014 no need to wait for the other session to end. "
                    "Retry after a few seconds. Work on something else in the meantime."
                ),
            }
        return {
            "reason": (
                f"\u26a0 File conflict: {normalized} in use by session "
                f"{conflict['key']} "
                f"({conflict.get('task') or conflict.get('holder') or 'unknown'})."
                " Please work on other files or wait."
            ),
            "additional": (
                "Another Claude Code session is editing this file. "
                "Check instance_lock.json for all sessions' file lists. "
                "Work on non-conflicting files or wait for the other session to finish."
            ),
        }


# ── BaseGuard adapter ───────────────────────────────────────────


class FileTrackerGuard(BaseGuard):
    """Multi-session file conflict detection via instance_lock.json."""

    name = "file_tracker"
    category = GuardCategory.QUALITY

    def check(self, ctx: GuardContext) -> GuardResult | None:
        """Deny writes to files locked by another active session.

        Args:
            ctx: Guard context with workspace, session_id, tool_name, tool_input.

        Returns:
            GuardResult.deny with conflict details, or None if no conflict.
        """
        if not ctx.workspace or not ctx.session_id:
            return None
        try:
            from concinno.core.config import get_config
            brain_dir = get_config().brain_dir
            lock_path = os.path.join(
                ctx.workspace, brain_dir, "cognition_shared", "instance_lock.json",
            )
            marker_dir = os.path.join(
                ctx.workspace, brain_dir, "cognition_shared", "markers",
            )
            tracker = FileTracker(
                ctx.workspace, lock_path, marker_dir,
            )
            result = tracker.process(ctx.session_id, ctx.tool_name, ctx.tool_input)
            deny = result.get("deny") if result else None
            if deny:
                return GuardResult.deny(
                    deny.get("reason", self.name),
                    context=deny.get("additional", ""),
                )
        except Exception:
            pass  # fail-open
        return None
