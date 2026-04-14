"""cc_cortex.multi_instance — Multi-session coordination and conflict detection.

@module multi_instance
@responsibility Track per-session file edits, detect write conflicts, manage zombie
               session cleanup, enforce role boundaries (ABCD mode).
@dependencies cc_cortex.constants, cc_cortex.core.path_utils
@exports check_conflict, clean_zombies, register_session,
         track_file, remove_session, check_role_boundary
"""

import os
import time
from datetime import datetime
from typing import Optional

from cc_cortex.constants import WRITE_TOOLS
from cc_cortex.core.path_utils import extract_file_path as _extract_fp


def extract_file_path(tool_name: str, tool_input: dict) -> Optional[str]:
    """Extract file path from tool input (delegates to path_utils)."""
    return _extract_fp(tool_input) or None


def normalize_path(file_path: str, workspace: str) -> Optional[str]:
    """Normalize absolute path to relative path from workspace root.

    E.g. 'C:\\Projects\\myapp\\src\\index.ts' → 'src/index.ts'
    """
    if not file_path:
        return None
    try:
        normalized = os.path.normpath(file_path)
        if normalized.lower().startswith(workspace.lower()):
            rel = normalized[len(workspace) :].lstrip(os.sep)
            return rel.replace("\\", "/")
    except Exception:
        pass
    return None


def is_shared(normalized_path: str, shared_basenames: frozenset, shared_paths: frozenset) -> bool:
    """Check if file is in the shared (no-conflict) list."""
    if not normalized_path:
        return False
    basename = os.path.basename(normalized_path)
    return basename in shared_basenames or normalized_path in shared_paths


def is_handoff(normalized_path: str, handoff_prefixes: tuple) -> bool:
    """Check if file is a handoff file (sequential-shared with short-lived lock)."""
    if not normalized_path:
        return False
    basename = os.path.basename(normalized_path)
    return any(basename.startswith(p) for p in handoff_prefixes)


def paths_conflict(declared: str, target: str) -> bool:
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


def check_conflict(
    sessions: dict,
    my_key: str,
    target_path: str,
    file_stale_sec: int = 300,
) -> Optional[dict]:
    """Check if target file conflicts with another active session's files.

    Files not edited for *file_stale_sec* (default 5 min) are auto-released
    so another session can take over without waiting.
    Returns conflict info dict or None.
    """
    now_ts = time.time()
    for key, session in sessions.items():
        if key == my_key:
            continue
        file_ts = session.get("file_timestamps", {})
        for f in list(session.get("files", [])):
            if paths_conflict(f, target_path):
                # Staleness check: file not edited for N seconds -> auto-release
                ts_str = file_ts.get(f)
                if ts_str:
                    try:
                        last_edit = datetime.fromisoformat(ts_str)
                        age = now_ts - last_edit.timestamp()
                        if age > file_stale_sec:
                            session["files"].remove(f)
                            file_ts.pop(f, None)
                            continue  # no conflict
                    except Exception:
                        pass
                return {
                    "key": key,
                    "holder": session.get("holder", ""),
                    "task": session.get("task", ""),
                }
    return None


def _is_abcd_mode(lock_data: dict) -> bool:
    """Check if ABCD parallel mode is active (3+ sessions with files)."""
    sessions = lock_data.get("sessions", {})
    active = sum(1 for s in sessions.values() if s.get("files"))
    return active >= 3


def cleanup_marker_by_prefix(marker_dir: str, prefix: str) -> None:
    """Clean up marker files matching prefix."""
    try:
        if not os.path.isdir(marker_dir):
            return
        for f in os.listdir(marker_dir):
            if f.startswith(prefix):
                os.remove(os.path.join(marker_dir, f))
    except Exception:
        pass


def _has_recent_activity(
    session: dict,
    recency_sec: int = 1800,
    now_ts: float = 0.0,
) -> bool:
    """Check if session had file activity within recency window.

    Looks at per-file timestamps (file_timestamps) — if ANY file was
    written within the window, the session is still active even if
    last_active wasn't updated.
    """
    if not now_ts:
        now_ts = time.time()
    file_ts = session.get("file_timestamps", {})
    for ts_str in file_ts.values():
        try:
            ts = datetime.fromisoformat(ts_str)
            if now_ts - ts.timestamp() < recency_sec:
                return True
        except Exception:
            continue
    return False


def clean_zombies(
    lock_data: dict,
    threshold_sec: int = 1800,
    fast_threshold_sec: int = 600,
    marker_dir: str = "",
    recency_check_sec: int = 1800,
) -> list[str]:
    """Remove sessions that are truly dead — not just idle.

    When a session exceeds the threshold, check if it had ANY file activity
    in the last *recency_check_sec* (default 30 min). If yes, it's still
    alive (just didn't update last_active) — skip it. Only remove sessions
    with ZERO recent activity.

    Automatically uses fast threshold in ABCD mode (3+ active sessions).
    Returns list of removed session keys.
    """
    threshold = fast_threshold_sec if _is_abcd_mode(lock_data) else threshold_sec
    sessions = lock_data.get("sessions", {})
    now_ts = time.time()
    to_remove = []

    for key, session in sessions.items():
        try:
            last = datetime.fromisoformat(session["last_active"])
            age_sec = now_ts - last.timestamp()
            if age_sec < threshold:
                continue  # Not yet expired

            # Threshold exceeded — but was there recent file activity?
            if _has_recent_activity(session, recency_check_sec, now_ts):
                # Still active — update last_active to extend lifetime
                session["last_active"] = datetime.now().isoformat()
                continue

            # Truly dead — no last_active AND no file activity
            to_remove.append(key)
        except Exception:
            # Can't parse last_active — check file activity as last resort
            if not _has_recent_activity(session, recency_check_sec, now_ts):
                to_remove.append(key)

    for key in to_remove:
        del sessions[key]
        if marker_dir:
            sid = key.replace("cc_", "")
            cleanup_marker_by_prefix(marker_dir, sid)

    return to_remove


def register_session(
    lock_data: dict,
    key: str,
    session_id: str,
    vscode_pid: int = 0,
    now: str = "",
) -> None:
    """Register or update a session in the lock data."""
    sessions = lock_data.setdefault("sessions", {})
    if key in sessions:
        sessions[key]["last_active"] = now
    else:
        sessions[key] = {
            "session_id": session_id,
            "vscode_pid": vscode_pid,
            "holder": "",
            "project": "",
            "task": "",
            "files": [],
            "started": now,
            "last_active": now,
        }


def track_file(
    lock_data: dict,
    key: str,
    normalized_path: str,
    max_files: int = 30,
    now: str = "",
) -> None:
    """Add a file to session's tracked files list (rolling window).

    Also updates per-file timestamp for staleness checking when *now* is provided.
    """
    sessions = lock_data.get("sessions", {})
    if key not in sessions:
        return
    files_list = sessions[key]["files"]
    file_ts = sessions[key].setdefault("file_timestamps", {})
    if normalized_path not in files_list:
        files_list.append(normalized_path)
        if len(files_list) > max_files:
            removed = files_list[:-max_files]
            sessions[key]["files"] = files_list[-max_files:]
            for r in removed:
                file_ts.pop(r, None)
    # Always update per-file timestamp on write
    if now:
        file_ts[normalized_path] = now


def remove_session(lock_data: dict, key: str) -> None:
    """Remove a session from the lock data."""
    sessions = lock_data.get("sessions", {})
    if key in sessions:
        del sessions[key]


def clean_same_pid_stale(
    lock_data: dict,
    current_key: str,
    vscode_pid: int,
    stale_sec: int = 120,
) -> list[str]:
    """Remove sessions sharing the same vscode_pid that are stale (>stale_sec).

    This handles /clear or restart scenarios where the old session marker
    lingers with the same PID but is no longer active.
    Returns list of removed session keys.
    """
    if not vscode_pid:
        return []
    sessions = lock_data.get("sessions", {})
    now_ts = time.time()
    to_remove = []

    for key, session in sessions.items():
        if key == current_key:
            continue
        if session.get("vscode_pid") != vscode_pid:
            continue
        try:
            last = datetime.fromisoformat(session["last_active"])
            age_sec = now_ts - last.timestamp()
            if age_sec > stale_sec:
                to_remove.append(key)
        except Exception:
            to_remove.append(key)

    for key in to_remove:
        del sessions[key]

    return to_remove


def check_role_boundary(
    lock_data: dict,
    my_key: str,
    marker_dir: str,
    zombie_threshold_sec: int = 1800,
) -> Optional[str]:
    """Warn once per session when other active sessions exist.

    Returns a warning string or None. Uses a marker file so the warning
    is emitted at most once per session.
    """
    short_id = my_key.replace("cc_", "")
    marker = os.path.join(marker_dir, f"{short_id}_role_boundary")
    try:
        os.makedirs(marker_dir, exist_ok=True)
        if os.path.isfile(marker):
            return None
    except Exception:
        return None

    sessions = lock_data.get("sessions", {})
    now_ts = time.time()
    active_others = 0
    for k, v in sessions.items():
        if k == my_key:
            continue
        try:
            last = datetime.fromisoformat(v.get("last_active", ""))
            if now_ts - last.timestamp() < zombie_threshold_sec:
                active_others += 1
        except Exception:
            pass

    if active_others == 0:
        return None

    # Write marker so we only warn once
    try:
        with open(marker, "w") as f:
            f.write("1")
    except Exception:
        pass

    return (
        f"👥 MultiInstance: {active_others} active session(s) "
        "— complete your role, don't claim others' tasks."
    )


def cleanup_role_boundary_marker(
    session_id: str,
    marker_dir: Optional[str] = None,
) -> None:
    """Remove the role_boundary marker file when session ends."""
    if not marker_dir:
        marker_dir = os.path.expanduser("~/.claude/token_state")
    short_id = session_id[:8] if len(session_id) > 8 else session_id
    marker = os.path.join(marker_dir, f"{short_id}_role_boundary")
    try:
        if os.path.isfile(marker):
            os.remove(marker)
    except Exception:
        pass


def release_lock() -> None:
    """Release current session's lock entry. Called by on_stop pipeline.

    Reads instance_lock.json, removes current session by CC_SESSION_ID,
    cleans up role boundary marker, writes back.
    """
    import json

    session_id = os.environ.get("CC_SESSION_ID", "")
    if not session_id:
        return

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if not project_dir:
        return

    from cc_cortex.core.config import get_config
    brain_dir = get_config().brain_dir
    lock_path = os.path.join(
        project_dir, brain_dir, "cognition_shared", "instance_lock.json",
    )
    if not os.path.isfile(lock_path):
        return

    try:
        with open(lock_path, "r", encoding="utf-8") as f:
            lock_data = json.load(f)
    except Exception:
        return

    key = f"cc_{session_id[:8]}"
    sessions = lock_data.get("sessions", {})
    if key not in sessions:
        # Try matching by session_id field
        key = next(
            (k for k, v in sessions.items() if v.get("session_id") == session_id),
            "",
        )
    if not key or key not in sessions:
        return

    remove_session(lock_data, key)

    try:
        tmp = lock_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(lock_data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, lock_path)
    except Exception:
        try:
            with open(lock_path, "w", encoding="utf-8") as f:
                json.dump(lock_data, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    # Clean up role boundary marker
    marker_dir = os.path.join(project_dir, brain_dir, "cognition_shared", "markers")
    cleanup_marker_by_prefix(marker_dir, session_id[:8])
    cleanup_role_boundary_marker(session_id)


__all__ = [
    "WRITE_TOOLS",
    "extract_file_path",
    "normalize_path",
    "is_shared",
    "is_handoff",
    "paths_conflict",
    "check_conflict",
    "_is_abcd_mode",
    "cleanup_marker_by_prefix",
    "clean_zombies",
    "register_session",
    "track_file",
    "remove_session",
    "release_lock",
    "clean_same_pid_stale",
    "check_role_boundary",
    "cleanup_role_boundary_marker",
]
