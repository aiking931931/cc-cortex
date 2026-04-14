"""Instance lock file operations.

@module process_guard.lock
@responsibility Read/write/cleanup instance_lock.json
@dependencies process_guard._base
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from ._base import _pid_alive


def _read_instance_lock(lock_path: str) -> dict:
    """Read instance_lock.json, return empty dict on failure."""
    try:
        with open(lock_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _cleanup_instance_lock(
    lock_path: str,
    idle_session_keys: set[str],
    ide_pids: set[int],
) -> int:
    """Remove dead/superseded sessions from instance_lock.json."""
    try:
        with open(lock_path, encoding="utf-8") as f:
            lock = json.load(f)
    except Exception:
        return 0

    sessions = lock.get("sessions", {})
    if not sessions:
        return 0

    # Find superseded sessions (same IDE PID, older session)
    by_ide: dict[int, list[tuple[str, str]]] = {}
    for key, sess in sessions.items():
        vp = int(sess.get("vscode_pid", 0))
        started = sess.get("started", "")
        if vp > 0:
            by_ide.setdefault(vp, []).append((key, started))

    superseded: set[str] = set()
    for _vp, group in by_ide.items():
        if len(group) <= 1:
            continue
        group.sort(key=lambda x: x[1], reverse=True)
        for key, _ in group[1:]:
            if key in idle_session_keys:
                superseded.add(key)

    # Remove dead IDE + superseded
    keys_to_remove = []
    for key in idle_session_keys:
        vp = int(sessions.get(key, {}).get("vscode_pid", 0))
        ide_alive = vp > 0 and vp in ide_pids and _pid_alive(vp)
        is_superseded = key in superseded

        if not ide_alive or is_superseded:
            keys_to_remove.append(key)

    removed = 0
    for key in keys_to_remove:
        del sessions[key]
        removed += 1

    if removed > 0:
        lock["last_updated"] = datetime.now(timezone.utc).isoformat()
        try:
            with open(lock_path, "w", encoding="utf-8") as f:
                json.dump(lock, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    return removed


def _register_cli_in_lock(
    lock_path: str,
    session_id: str,
    session_key: str,
    cli_pid: int,
    vscode_pid: int,
    tz_offset_hours: int = 8,
) -> dict:
    """Register cli_pid in instance_lock.json. Returns sessions dict."""
    tz = timezone(timedelta(hours=tz_offset_hours))
    now = datetime.now(tz).isoformat()

    lock: dict = {}
    try:
        with open(lock_path, "r", encoding="utf-8") as f:
            lock = json.load(f)
    except Exception:
        pass

    sessions = lock.setdefault("sessions", {})
    if session_key in sessions:
        sessions[session_key]["cli_pid"] = cli_pid
        sessions[session_key]["last_active"] = now
        # Back-fill holder if still empty
        if not sessions[session_key].get("holder"):
            sessions[session_key]["holder"] = session_key
    else:
        sessions[session_key] = {
            "session_id": session_id,
            "vscode_pid": vscode_pid,
            "cli_pid": cli_pid,
            "holder": session_key,
            "project": "",
            "task": "",
            "files": [],
            "started": now,
            "last_active": now,
        }

    lock["last_updated"] = now
    tmp = lock_path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(lock, f, indent=2, ensure_ascii=False)
        os.replace(tmp, lock_path)
    except Exception:
        pass

    return sessions


def _clean_stale_from_lock(lock_path: str, proc_map: dict[int, dict]) -> int:
    """Remove sessions whose VSCode/CLI is dead. Returns count removed."""
    try:
        with open(lock_path, "r", encoding="utf-8") as f:
            lock = json.load(f)
    except Exception:
        return 0

    sessions = lock.get("sessions", {})
    if not sessions:
        return 0

    ide_names = {"code.exe", "code", "cursor.exe", "cursor"}
    alive_ides = {pid for pid, info in proc_map.items() if info["name"] in ide_names}

    to_remove = []
    for key, sess in sessions.items():
        vp = int(sess.get("vscode_pid", 0))
        cp = int(sess.get("cli_pid", 0))
        if vp > 0 and vp not in alive_ides:
            to_remove.append(key)
        elif cp > 0 and cp not in proc_map:
            to_remove.append(key)

    if to_remove:
        for key in to_remove:
            del sessions[key]
        lock["last_updated"] = datetime.now(timezone.utc).isoformat()
        try:
            tmp = lock_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(lock, f, indent=2, ensure_ascii=False)
            os.replace(tmp, lock_path)
        except Exception:
            pass

    return len(to_remove)
