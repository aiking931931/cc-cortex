"""cc_cortex.process_guard — Cross-platform Claude Code process management.

@module process_guard
@responsibility Detect and kill orphan/stale Claude processes, register sessions,
               enforce RAM/quota limits.
@dependencies (none — stdlib only)
"""

from ._base import (
    CLAUDE_MAX_MB,
    IDLE_MINUTES,
    MEMORY_CRITICAL_PERCENT,
    STALE_MINUTES,
    ClaudeProcess,
    GuardResult,
    RegistrationResult,
    Tier,
    _get_system_memory_percent,
    _kill_process,
    _pid_alive,
)
from .classifier import (
    _classify_processes,
    _emergency_memory_relief,
    _find_ancestor,
    _find_orphan_children,
    _find_subagent_pids,
    _get_child_tree,
    _is_scheduled_task,
)
from .cli import main
from .discovery import _find_claude_processes, _get_all_processes
from .guard import run_guard
from .lock import _cleanup_instance_lock, _read_instance_lock
from .registration import register_session_startup

__all__ = [
    # Models
    "Tier",
    "ClaudeProcess",
    "GuardResult",
    "RegistrationResult",
    # Public API
    "run_guard",
    "register_session_startup",
    "main",
    # Constants
    "IDLE_MINUTES",
    "STALE_MINUTES",
    "CLAUDE_MAX_MB",
    "MEMORY_CRITICAL_PERCENT",
    # Internal (used by hooks/tests)
    "_get_all_processes",
    "_find_claude_processes",
    "_classify_processes",
    "_find_orphan_children",
    "_find_subagent_pids",
    "_get_child_tree",
    "_emergency_memory_relief",
    "_read_instance_lock",
    "_cleanup_instance_lock",
    "_find_ancestor",
    "_is_scheduled_task",
    "_get_system_memory_percent",
    "_kill_process",
    "_pid_alive",
]
