"""Main process guard — scan, classify, cleanup.

@module process_guard.guard
@responsibility Orchestrate process scanning and cleanup
@dependencies process_guard._base, discovery, classifier, lock
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Optional

from ._base import (
    CLAUDE_MAX_MB,
    IDLE_MINUTES,
    MEMORY_CRITICAL_PERCENT,
    STALE_MINUTES,
    GuardResult,
    Tier,
    _get_system_memory_percent,
    _kill_process,
)
from .classifier import (
    _classify_processes,
    _emergency_memory_relief,
    _find_orphan_children,
)
from .discovery import _find_claude_processes, _get_all_processes
from .lock import _cleanup_instance_lock, _read_instance_lock


def run_guard(
    *,
    lock_path: Optional[str] = None,
    idle_minutes: int = IDLE_MINUTES,
    stale_minutes: int = STALE_MINUTES,
    memory_critical_percent: float = MEMORY_CRITICAL_PERCENT,
    dry_run: bool = False,
) -> GuardResult:
    """Run the process guard — scan, classify, and cleanup.

    Args:
        lock_path: Path to instance_lock.json. Auto-detected if None.
        idle_minutes: Minutes before a session is considered idle.
        stale_minutes: Minutes before a stale process is killed.
        memory_critical_percent: System RAM % threshold for emergency relief.
        dry_run: If True, log actions but don't kill anything.

    Returns:
        GuardResult with scan/kill/warning details.
    """
    result = GuardResult()

    if not lock_path:
        workspace = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
        from concinno.core.config import get_config
        brain_dir = get_config().brain_dir
        lock_path = os.path.join(
            workspace, brain_dir, "cognition_shared", "instance_lock.json"
        )

    # Phase 1: Discover
    all_procs = _get_all_processes()
    claude_procs = _find_claude_processes(all_procs)
    result.scanned = len(claude_procs)

    if not claude_procs:
        return result

    total_mb = sum(cp.mem_mb for cp in claude_procs)
    result.actions.append(f"SCAN: {len(claude_procs)} Claude processes, {total_mb}MB total")

    # Phase 2: Classify
    claude_procs = _classify_processes(
        claude_procs, all_procs, lock_path,
        idle_minutes=idle_minutes, stale_minutes=stale_minutes,
    )

    # Phase 3: Kill orphans + stale
    _kill_targets(claude_procs, result, dry_run)

    # Phase 4: Kill orphan children (bash/node)
    _kill_orphan_children(all_procs, result, dry_run)

    # Phase 5: RAM check
    _check_memory(
        claude_procs, all_procs, lock_path, total_mb,
        result, memory_critical_percent, dry_run,
    )

    # Phase 6: Cleanup instance_lock
    _cleanup_lock(all_procs, lock_path, idle_minutes, result, dry_run)

    return result


def _kill_targets(
    claude_procs: list, result: GuardResult, dry_run: bool,
) -> None:
    """Kill ORPHAN and STALE processes."""
    kill_targets = [cp for cp in claude_procs if cp.tier in (Tier.ORPHAN, Tier.STALE)]
    for cp in kill_targets:
        mode = "DRY-RUN" if dry_run else "KILL"
        action = (
            f"{mode}: PID {cp.pid} ({cp.name}, {cp.mem_mb}MB) "
            f"— {cp.tier.value}: {cp.kill_reason}"
        )
        result.actions.append(action)
        if not dry_run and _kill_process(cp.pid):
            result.killed += 1
            result.freed_mb += cp.mem_mb


def _kill_orphan_children(
    all_procs: list[dict], result: GuardResult, dry_run: bool,
) -> None:
    """Kill orphan child processes (bash/node/MCP)."""
    orphan_children = _find_orphan_children(all_procs)
    for pid in orphan_children:
        action = f"{'DRY-RUN' if dry_run else 'KILL'}: orphan child PID {pid}"
        result.actions.append(action)
        if not dry_run:
            _kill_process(pid)


def _check_memory(
    claude_procs: list, all_procs: list[dict], lock_path: str,
    total_mb: int, result: GuardResult,
    memory_critical_percent: float, dry_run: bool,
) -> None:
    """Check RAM and trigger emergency relief if needed.

    Wave 1-3 (kill processes) runs first. If RAM is still ≥ threshold
    after the kill waves, wave 4 (memory_relief — Win32 standby /
    working-set trim) is invoked when ``memory_relief`` feature has
    ``auto_trigger_after_process_guard=True`` (the default). Wave 4
    never auto-escalates beyond ``safe`` mode; the operator must opt
    into standby/aggressive tiers via FEATURE_META ``auto_trigger_mode``.
    """
    mem_pct = _get_system_memory_percent()
    if mem_pct >= memory_critical_percent:
        relief_actions, relief_killed, relief_freed = _emergency_memory_relief(
            claude_procs, all_procs, lock_path,
            dry_run=dry_run, threshold=memory_critical_percent,
        )
        result.actions.extend(relief_actions)
        result.killed += relief_killed
        result.freed_mb += relief_freed
        if relief_killed > 0:
            result.user_notification = (
                f"EMERGENCY_RAM_RELIEF | ram={mem_pct:.0f}% "
                f"threshold={memory_critical_percent:.0f}% "
                f"killed={relief_killed} freed={relief_freed}MB "
                f"mother_preserved=true | "
                f"Tell the user in their language: "
                f"RAM hit {mem_pct:.0f}%, killed "
                f"{relief_killed} subprocess(es), "
                f"freed {relief_freed}MB. "
                f"Mother agent preserved for handoff."
            )

        # Wave 4: Win32 standby / working-set trim via memory_relief.
        # Only fires if wave 1-3 left RAM still pressured. Lazy-import
        # so an older Concinno install without memory_relief keeps the
        # guard chain operational.
        post_kill_pct = _get_system_memory_percent()
        if post_kill_pct >= memory_critical_percent:
            _try_memory_relief_wave4(result, post_kill_pct, dry_run)
    elif total_mb > CLAUDE_MAX_MB:
        result.warnings.append(
            f"Claude processes using {total_mb}MB > {CLAUDE_MAX_MB}MB limit"
        )


def _try_memory_relief_wave4(
    result: GuardResult, mem_pct: float, dry_run: bool,
) -> None:
    """Invoke ``memory_relief.run_cleanup`` as wave 4. Best-effort: any
    failure is recorded in ``result.actions`` but never aborts the guard.
    Reads tier + thresholds from FEATURE_META so operators can disable
    via ``concinno features set memory_relief.auto_trigger_after_process_guard false``."""
    try:
        from concinno.feature_config import get_feature
        from concinno.memory_relief import run_cleanup
    except ImportError as exc:
        result.actions.append(
            f"WAVE4-SKIP: memory_relief unavailable ({exc})"
        )
        return

    cfg = get_feature("memory_relief") or {}
    params = cfg.get("params") or {}

    def _param_default(key: str, fallback):
        entry = params.get(key)
        if isinstance(entry, dict) and "default" in entry:
            return entry["default"]
        return entry if entry is not None else fallback

    if not bool(_param_default("auto_trigger_after_process_guard", True)):
        return  # Opted out at FEATURE_META level.
    auto_mode = str(_param_default("auto_trigger_mode", "safe"))
    top_n = int(_param_default("top_n_per_process_trim", 8))
    min_mb = int(_param_default("min_trim_mb", 50))

    try:
        report = run_cleanup(
            mode=auto_mode,
            dry_run=dry_run,
            top_n=top_n,
            min_bytes=min_mb * 1024 * 1024,
        )
    except Exception as exc:  # noqa: BLE001 — wave 4 never aborts guard
        result.actions.append(
            f"WAVE4-ERROR: memory_relief raised {type(exc).__name__}: {exc}"
        )
        return

    mode_label = "DRY-RUN" if dry_run else "TRIM"
    result.actions.append(
        f"WAVE4 {mode_label}: memory_relief mode={auto_mode} "
        f"reclaimed={report.reclaimed_mb}MB "
        f"trimmed_processes={len(report.process_trims)} "
        f"ram_before_wave4={mem_pct:.0f}%"
    )
    result.freed_mb += report.reclaimed_mb


def _cleanup_lock(
    all_procs: list[dict], lock_path: str, idle_minutes: int,
    result: GuardResult, dry_run: bool,
) -> None:
    """Cleanup stale sessions from instance_lock.json."""
    if dry_run or not os.path.exists(lock_path):
        return

    now = time.time()
    lock = _read_instance_lock(lock_path)
    sessions = lock.get("sessions", {})
    idle_keys: set[str] = set()
    for key, sess in sessions.items():
        last_active = sess.get("last_active")
        if last_active:
            try:
                dt = datetime.fromisoformat(last_active.replace("Z", "+00:00"))
                if (now - dt.timestamp()) / 60 > idle_minutes:
                    idle_keys.add(key)
            except (ValueError, TypeError):
                pass

    ide_names = {"code", "code.exe", "cursor", "cursor.exe"}
    ide_pids = {p["pid"] for p in all_procs if p.get("name", "").lower() in ide_names}
    result.lock_cleaned = _cleanup_instance_lock(lock_path, idle_keys, ide_pids)
    if result.lock_cleaned:
        result.actions.append(f"LOCK-CLEAN: removed {result.lock_cleaned} stale session(s)")
