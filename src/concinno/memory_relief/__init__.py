"""concinno.memory_relief — Windows RAM cleanup with anti-snake-oil defaults.

@module memory_relief
@responsibility Programmatic + tray + agent-tool surfaces for releasing
    Windows standby/modified/working-set memory **only when measurement
    proves it helps**. Documented Win32 APIs are the default path; the
    undocumented ``NtSetSystemInformation`` purge is opt-in behind an
    ``aggressive`` flag because indiscriminate clearing of the standby
    list demonstrably trades RAM% for re-read disk I/O.

@dependencies stdlib only for the ``core`` and ``engine`` modules; the
    ``tray`` module additionally needs ``pystray`` + ``Pillow``, declared
    under the ``[memory-relief-tray]`` extras so headless deployments do
    not pull GUI deps.

Layered surfaces (lazy-imported so a CLI-only consumer never pays the
GUI import cost):

* :mod:`concinno.memory_relief.core` — Win32 ctypes primitives.
* :mod:`concinno.memory_relief.engine` — before/after orchestration.
* :mod:`concinno.memory_relief.tray` — system-tray right-click cleaner.
* :mod:`concinno.tools.builtin.memory_relief` — agent ``Tool`` wrapper.

Re-exports the small public surface most callers need; everything else
is reachable via the submodule path.
"""

from __future__ import annotations

from .core import (
    AVAILABLE_BYTES,
    COMMIT_TOTAL_BYTES,
    MODIFIED_BYTES,
    STANDBY_BYTES,
    WORKING_SET_BYTES,
    MemorySnapshot,
    PerformanceInfo,
    PrivilegeError,
    empty_working_set_for_pid,
    get_memory_snapshot,
    get_performance_info,
    is_admin,
    purge_low_priority_standby_list,
    purge_modified_page_list,
    purge_standby_list,
    set_system_file_cache_minimal,
)
from .engine import (
    CleanupMode,
    CleanupReport,
    PerProcessTrim,
    StageResult,
    run_cleanup,
)

__all__ = [
    # Snapshot keys
    "AVAILABLE_BYTES",
    "COMMIT_TOTAL_BYTES",
    "MODIFIED_BYTES",
    "STANDBY_BYTES",
    "WORKING_SET_BYTES",
    # Models
    "MemorySnapshot",
    "PerformanceInfo",
    "PrivilegeError",
    "CleanupMode",
    "CleanupReport",
    "PerProcessTrim",
    "StageResult",
    # Public API
    "empty_working_set_for_pid",
    "get_memory_snapshot",
    "get_performance_info",
    "is_admin",
    "purge_low_priority_standby_list",
    "purge_modified_page_list",
    "purge_standby_list",
    "set_system_file_cache_minimal",
    "run_cleanup",
]
