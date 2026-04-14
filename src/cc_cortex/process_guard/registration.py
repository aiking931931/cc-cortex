"""Session registration for SessionStart hook.

@module process_guard.registration
@responsibility Register CLI sessions, detect suspects, cleanup stale
@dependencies process_guard._base, discovery, lock
"""

from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from ._base import RegistrationResult
from .discovery import _find_claude_processes, _get_all_processes
from .lock import _clean_stale_from_lock, _register_cli_in_lock


def _build_proc_map(all_procs: list[dict]) -> dict[int, dict]:
    """Build {pid: {ppid, name}} map from process list."""
    return {
        p["pid"]: {"ppid": p.get("ppid", 0), "name": p.get("name", "").lower()}
        for p in all_procs
    }


def _find_cli_pid(proc_map: dict[int, dict]) -> int:
    """Walk up from current Python process to find Claude CLI parent."""
    cli_names = {"claude.exe", "claude", "code.exe", "code", "cursor.exe", "cursor"}
    pid = os.getpid()
    for _ in range(10):
        info = proc_map.get(pid)
        if not info:
            break
        ppid = info["ppid"]
        if ppid <= 0:
            break
        parent = proc_map.get(ppid)
        if not parent:
            break
        if parent["name"] in cli_names:
            return ppid
        pid = ppid
    return 0


def _find_vscode_main_pid(cli_pid: int, proc_map: dict[int, dict]) -> int:
    """Walk up from CLI to find topmost Code.exe/Cursor.exe ancestor."""
    ide_names = {"code.exe", "code", "cursor.exe", "cursor"}
    pid = cli_pid
    topmost = cli_pid
    for _ in range(8):
        info = proc_map.get(pid)
        if not info:
            break
        ppid = info["ppid"]
        if ppid <= 0:
            break
        parent = proc_map.get(ppid)
        if not parent:
            break
        if parent["name"] in ide_names:
            topmost = ppid
            pid = ppid
        else:
            break
    return topmost


def _get_or_create_session_key(
    session_id: str,
    marker_dir: str,
    tz_offset_hours: int = 8,
) -> str:
    """Read session_name from marker file, or generate + save one."""
    _VALID = re.compile(r"^[A-Za-z]{2,8}_[0-9a-f]{4,8}_\d{4}$")
    if not session_id:
        return ""
    marker = os.path.join(marker_dir, f"{session_id}.session_name")
    try:
        if os.path.isfile(marker):
            with open(marker, "r", encoding="utf-8") as f:
                name = f.read().strip()
                if name and _VALID.match(name):
                    return name
    except Exception:
        pass
    tz = timezone(timedelta(hours=tz_offset_hours))
    hhmm = datetime.now(tz).strftime("%H%M")
    hex4 = uuid.uuid4().hex[:4]
    key = f"cc_{hex4}_{hhmm}"
    try:
        os.makedirs(marker_dir, exist_ok=True)
        with open(marker, "w", encoding="utf-8") as f:
            f.write(key)
    except Exception:
        pass
    return key


def register_session_startup(
    session_id: str,
    *,
    lock_path: Optional[str] = None,
    marker_dir: Optional[str] = None,
    tz_offset_hours: int = 8,
) -> RegistrationResult:
    """High-level SessionStart registration.

    1. Snapshot process tree
    2. Find CLI PID by walking up
    3. Find VSCode main PID
    4. Generate/read session key
    5. Register in instance_lock.json
    6. Detect unregistered CLI suspects
    7. Clean stale sessions
    """
    result = RegistrationResult()

    if not session_id:
        result.actions.append("SKIP: no session_id")
        return result

    if not lock_path:
        workspace = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
        from cc_cortex.core.config import get_config
        brain_dir = get_config().brain_dir
        lock_path = os.path.join(
            workspace, brain_dir, "cognition_shared", "instance_lock.json",
        )
    if not marker_dir:
        home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
        marker_dir = os.path.join(home, ".claude", "session_markers")

    # Step 1: Snapshot
    all_procs = _get_all_processes()
    if not all_procs:
        result.actions.append("SKIP: empty process snapshot")
        return result

    proc_map = _build_proc_map(all_procs)

    # Step 2: Find CLI PID
    cli_pid = _find_cli_pid(proc_map)
    if not cli_pid:
        result.actions.append("SKIP: could not find CLI PID")
        return result
    result.cli_pid = cli_pid

    # Step 3: Find VSCode main PID
    vscode_pid = int(os.environ.get("VSCODE_PID", "0"))
    if not vscode_pid:
        vscode_pid = _find_vscode_main_pid(cli_pid, proc_map)
    result.vscode_pid = vscode_pid

    # Step 4: Get/create session key
    session_key = _get_or_create_session_key(session_id, marker_dir, tz_offset_hours)
    result.session_key = session_key

    # Step 5: Register in instance_lock
    sessions = _register_cli_in_lock(
        lock_path, session_id, session_key, cli_pid, vscode_pid, tz_offset_hours,
    )
    result.actions.append(
        f"REGISTER: {session_key} cli_pid={cli_pid} vscode_pid={vscode_pid}"
    )

    # Step 6: Detect unregistered CLI suspects
    registered = {s.get("cli_pid") for s in sessions.values() if s.get("cli_pid")}
    registered.add(cli_pid)
    claude_procs = _find_claude_processes(all_procs)
    suspects = [cp.pid for cp in claude_procs if cp.pid not in registered]
    result.suspects = suspects
    if suspects:
        result.actions.append(
            f"SUSPECT: {len(suspects)} unregistered CLI(s): {','.join(map(str, suspects))}"
        )

    # Step 7: Clean stale sessions
    result.stale_cleaned = _clean_stale_from_lock(lock_path, proc_map)
    if result.stale_cleaned:
        result.actions.append(
            f"LOCK-CLEAN: removed {result.stale_cleaned} stale session(s)"
        )

    return result
