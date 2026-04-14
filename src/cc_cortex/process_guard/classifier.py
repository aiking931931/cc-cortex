"""Process classification and tree operations.

@module process_guard.classifier
@responsibility Classify processes, tree walk, orphan detection, emergency relief
@dependencies process_guard._base
"""

from __future__ import annotations

import re
import time
from datetime import datetime

from ._base import (
    IDLE_MINUTES,
    MEMORY_CRITICAL_PERCENT,
    STALE_MINUTES,
    ClaudeProcess,
    Tier,
    _get_system_memory_percent,
    _kill_process,
    _pid_alive,
    logger,
)
from .lock import _read_instance_lock

# ── Tree operations ────────────────────────────────────────


def _find_ancestor(
    pid: int,
    target_pids: set[int],
    proc_map: dict[int, dict],
    max_depth: int = 6,
) -> int:
    """Walk up process tree to find an ancestor PID in target_pids."""
    current = pid
    for _ in range(max_depth):
        parent = proc_map.get(current, {}).get("ppid", 0)
        if parent <= 0:
            break
        if parent in target_pids:
            return parent
        current = parent
    return 0


def _is_scheduled_task(pid: int, proc_map: dict[int, dict], max_depth: int = 6) -> bool:
    """Check if process is a child of scheduled_launcher or auto-agent."""
    current = pid
    for _ in range(max_depth):
        parent_info = proc_map.get(current)
        if not parent_info:
            break
        cmdline = parent_info.get("cmdline", "")
        if "auto-agent" in cmdline or "scheduled_launcher" in cmdline:
            return True
        current = parent_info.get("ppid", 0)
        if current <= 0:
            break
    return False


def _find_subagent_pids(parent_pid: int, all_procs: list[dict]) -> list[int]:
    """Find subagent processes (claude.exe children) spawned by a parent."""
    proc_map = {p["pid"]: p for p in all_procs}
    subagents = []
    for p in all_procs:
        if p["pid"] == parent_pid:
            continue
        name = p.get("name", "").lower()
        if name not in ("claude", "claude.exe"):
            continue
        current = p["pid"]
        for _ in range(8):
            info = proc_map.get(current)
            if not info:
                break
            ppid = info.get("ppid", 0)
            if ppid == parent_pid:
                subagents.append(p)
                break
            if ppid <= 0:
                break
            current = ppid
    subagents.sort(key=lambda p: p.get("start_time") or 0)
    return [p["pid"] for p in subagents]


def _get_child_tree(pid: int, all_procs: list[dict]) -> list[int]:
    """Get all descendant PIDs of a process."""
    children_map: dict[int, list[int]] = {}
    for p in all_procs:
        ppid = p.get("ppid", 0)
        if ppid > 0:
            children_map.setdefault(ppid, []).append(p["pid"])

    result = []
    queue = children_map.get(pid, [])
    while queue:
        child = queue.pop(0)
        result.append(child)
        queue.extend(children_map.get(child, []))
    return result


def _find_orphan_children(all_procs: list[dict]) -> list[int]:
    """Find orphaned bash/node/MCP-server processes from dead Claude sessions."""
    proc_map = {p["pid"]: p for p in all_procs}
    orphans = []

    for p in all_procs:
        name = p.get("name", "").lower()
        cmdline = p.get("cmdline", "")

        is_target = False
        if name in ("bash", "bash.exe") and "claude" in cmdline.lower():
            is_target = True
        elif name in ("node", "node.exe") and re.search(
            r"(phase\d+\.test|npm\s+(test|run\s+dev))", cmdline
        ):
            is_target = True
        elif name in ("python", "python.exe", "python3", "python3.exe") and re.search(
            r"mcp[_\-]server", cmdline, re.IGNORECASE
        ):
            is_target = True

        if is_target:
            ppid = p.get("ppid", 0)
            if ppid > 0 and ppid not in proc_map:
                orphans.append(p["pid"])
            elif ppid > 0 and not _pid_alive(ppid):
                orphans.append(p["pid"])

    return orphans


# ── Classification ─────────────────────────────────────────


def _classify_processes(
    claude_procs: list[ClaudeProcess],
    all_procs: list[dict],
    lock_path: str,
    idle_minutes: int = IDLE_MINUTES,
    stale_minutes: int = STALE_MINUTES,
) -> list[ClaudeProcess]:
    """Classify each Claude process into ALIVE/STALE/ORPHAN/SCHEDULED."""
    now = time.time()
    proc_map = {p["pid"]: p for p in all_procs}

    ide_names = {"code", "code.exe", "cursor", "cursor.exe"}
    ide_pids = {p["pid"] for p in all_procs if p.get("name", "").lower() in ide_names}

    lock = _read_instance_lock(lock_path)
    sessions = lock.get("sessions", {})

    active_ide_pids: set[int] = set()
    registered_cli_pids: set[int] = set()
    for _key, sess in sessions.items():
        # Collect registered cli_pids (strongest protection for thinking sessions)
        cli_pid = sess.get("cli_pid", 0)
        if isinstance(cli_pid, (int, float)) and int(cli_pid) > 0:
            registered_cli_pids.add(int(cli_pid))

        last_active = sess.get("last_active")
        if last_active:
            try:
                dt = datetime.fromisoformat(last_active.replace("Z", "+00:00"))
                idle_min = (now - dt.timestamp()) / 60
                if idle_min > idle_minutes:
                    continue
            except (ValueError, TypeError):
                pass
        vscode_pid = sess.get("vscode_pid", 0)
        if isinstance(vscode_pid, (int, float)) and int(vscode_pid) > 0:
            active_ide_pids.add(int(vscode_pid))

    for cp in claude_procs:
        ancestor_ide = _find_ancestor(cp.pid, ide_pids, proc_map)

        if ancestor_ide == 0:
            if _is_scheduled_task(cp.pid, proc_map):
                cp.tier = Tier.SCHEDULED
            else:
                cp.tier = Tier.ORPHAN
                cp.kill_reason = "no IDE parent"
            continue

        # ★ IDE parent alive = ALWAYS ALIVE (never STALE).
        # /clear zombies are caught downstream by zombie-CLI detection.
        # STALE logic was killing thinking sessions — false positive cost >> cleanup value.
        cp.tier = Tier.ALIVE
        if ancestor_ide in active_ide_pids:
            logger.debug("ALIVE | PID %d — active session under IDE %d", cp.pid, ancestor_ide)
        else:
            logger.info("ALIVE | PID %d — IDE-parented (no-stale rule)", cp.pid)

    return claude_procs


def _has_active_children(pid: int, all_procs: list[dict]) -> bool:
    """Check if a process has active child processes (bash/node/claude)."""
    active_names = {"bash", "bash.exe", "node", "node.exe", "claude", "claude.exe"}
    child_pids = [
        p["pid"]
        for p in all_procs
        if p.get("ppid") == pid and p.get("name", "").lower() in active_names
    ]
    if child_pids:
        return True
    # Check grandchildren
    direct_children = [p["pid"] for p in all_procs if p.get("ppid") == pid]
    for dc_pid in direct_children:
        gc = [
            p
            for p in all_procs
            if p.get("ppid") == dc_pid and p.get("name", "").lower() in active_names
        ]
        if gc:
            return True
    return False


# ── Emergency memory relief ────────────────────────────────


def _emergency_memory_relief(
    claude_procs: list[ClaudeProcess],
    all_procs: list[dict],
    lock_path: str,
    dry_run: bool = False,
    threshold: float = MEMORY_CRITICAL_PERCENT,
) -> tuple[list[str], int, int]:
    """Emergency memory relief when system RAM >= threshold.

    Kill order (least damage first):
    1. Orphan MCP servers (dead parent)
    2. Subagents of each session (innermost first)
    3. Child trees of idle sessions (preserve mother claude.exe)
    """
    actions: list[str] = []
    killed = 0
    freed_mb = 0

    def _try_kill(pid: int, reason: str) -> bool:
        nonlocal killed, freed_mb
        mem = 0
        for p in all_procs:
            if p["pid"] == pid:
                mem = int(p.get("mem", 0)) // (1024 * 1024)
                break
        mode = "DRY-RUN" if dry_run else "KILL"
        actions.append(f"EMERGENCY {mode}: PID {pid} ({mem}MB) — {reason}")
        if not dry_run and _kill_process(pid):
            killed += 1
            freed_mb += mem
            return True
        return False

    mem_pct = _get_system_memory_percent()
    if mem_pct < threshold:
        return actions, killed, freed_mb

    actions.append(f"EMERGENCY: system RAM at {mem_pct:.0f}% >= {threshold:.0f}%")

    # Wave 1: Orphan MCP servers
    orphans = _find_orphan_children(all_procs)
    for pid in orphans:
        _try_kill(pid, "orphan child (MCP/bash/node)")
        if _get_system_memory_percent() < threshold:
            actions.append("EMERGENCY: RAM recovered after wave 1 (orphans)")
            return actions, killed, freed_mb

    # Wave 2: Subagents (per session, idle sessions first)
    lock = _read_instance_lock(lock_path)
    sessions = lock.get("sessions", {})
    session_list = sorted(
        sessions.items(),
        key=lambda kv: kv[1].get("last_active", ""),
    )

    for _key, sess in session_list:
        cli_pid = sess.get("cli_pid", 0)
        if not cli_pid or not _pid_alive(cli_pid):
            continue
        subs = _find_subagent_pids(cli_pid, all_procs)
        for sub_pid in subs:
            _try_kill(sub_pid, f"subagent of cli_pid={cli_pid}")
            if _get_system_memory_percent() < threshold:
                actions.append("EMERGENCY: RAM recovered after wave 2 (subagents)")
                return actions, killed, freed_mb

    # Wave 3: Kill child trees of idle sessions, preserve mother claude.exe
    for _key, sess in session_list:
        cli_pid = sess.get("cli_pid", 0)
        if not cli_pid or not _pid_alive(cli_pid):
            continue
        children = _get_child_tree(cli_pid, all_procs)
        for child_pid in children:
            child_info = next((p for p in all_procs if p["pid"] == child_pid), {})
            child_name = child_info.get("name", "").lower()
            if child_name in ("claude", "claude.exe"):
                continue  # preserve mother
            _try_kill(child_pid, f"child of idle session cli_pid={cli_pid}")
        if _get_system_memory_percent() < threshold:
            actions.append("EMERGENCY: RAM recovered after wave 3 (idle children)")
            return actions, killed, freed_mb

    final_pct = _get_system_memory_percent()
    actions.append(
        f"EMERGENCY: all waves done, RAM at {final_pct:.0f}% "
        f"(freed {freed_mb}MB, killed {killed} processes)"
    )
    return actions, killed, freed_mb
