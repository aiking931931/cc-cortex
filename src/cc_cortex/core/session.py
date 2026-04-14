"""
cc_cortex.core.session — Session ID generation and marker file management.

Generates SESSION_<HHmm>_<uuid12> format IDs and manages marker files
for cross-session identification.
"""

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional


def generate_session_id(tz_offset_hours: int = 8) -> str:
    """Generate a unique session ID in unified format cc_<4hex>_<HHmm>.

    Initial sessions use 'cc_' prefix. update_session_task.py renames
    to project abbreviation (e.g. EVO_b500_1423) after task registration.

    Args:
        tz_offset_hours: Timezone offset from UTC (default 8 for Asia/Taipei).

    Returns:
        Session ID string like "cc_a3f7_1432".
    """
    tz = timezone(timedelta(hours=tz_offset_hours))
    now = datetime.now(tz)
    hhmm = now.strftime("%H%M")
    hex4 = uuid.uuid4().hex[:4]
    return f"cc_{hex4}_{hhmm}"


def save_session_marker(
    session_id: str,
    marker_dir: Optional[str] = None,
    session_name: Optional[str] = None,
) -> bool:
    """Save session name to a marker file for PreToolUse hook validation.

    Args:
        session_id: The Claude Code session ID.
        marker_dir: Directory for marker files (default ~/.claude/session_markers).
        session_name: The generated session name to save.

    Returns:
        True on success.
    """
    if not marker_dir:
        marker_dir = os.path.expanduser("~/.claude/session_markers")
    os.makedirs(marker_dir, exist_ok=True)
    try:
        marker_path = os.path.join(marker_dir, f"{session_id}.session_name")
        with open(marker_path, "w", encoding="utf-8") as f:
            f.write(session_name or "")
        return True
    except Exception:
        return False


def cleanup_session_marker(session_id: str, marker_dir: Optional[str] = None) -> None:
    """Remove session marker files when session ends.

    Args:
        session_id: The Claude Code session ID.
        marker_dir: Directory for marker files.
    """
    if not marker_dir:
        marker_dir = os.path.expanduser("~/.claude/session_markers")
    for suffix in (".session_name", ".active"):
        try:
            path = os.path.join(marker_dir, f"{session_id}{suffix}")
            if os.path.isfile(path):
                os.remove(path)
        except Exception:
            pass


def write_active_marker(
    session_id: str,
    lock_key: str,
    vscode_pid: int = 0,
    tz_offset_hours: int = 8,
    marker_dir: Optional[str] = None,
) -> None:
    """Write an .active marker file so Claude can find its own session."""
    if not marker_dir:
        marker_dir = os.path.expanduser("~/.claude/session_markers")
    try:
        os.makedirs(marker_dir, exist_ok=True)
        tz = timezone(timedelta(hours=tz_offset_hours))
        marker_path = os.path.join(marker_dir, f"{session_id}.active")
        with open(marker_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "session_id": session_id,
                    "lock_key": lock_key,
                    "vscode_pid": vscode_pid,
                    "updated": datetime.now(tz).isoformat(),
                },
                f,
            )
    except Exception:
        pass


def cleanup_markers_by_prefix(prefix: str, marker_dir: Optional[str] = None) -> None:
    """Clean up all marker files matching a prefix."""
    if not marker_dir:
        marker_dir = os.path.expanduser("~/.claude/session_markers")
    try:
        if not os.path.isdir(marker_dir):
            return
        for f in os.listdir(marker_dir):
            if f.startswith(prefix):
                os.remove(os.path.join(marker_dir, f))
    except Exception:
        pass
