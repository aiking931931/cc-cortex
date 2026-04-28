"""concinno.memory_relief.core — Win32 ctypes primitives for RAM cleanup.

@module memory_relief.core
@responsibility Thin, side-effect-explicit wrappers over the Win32 APIs
    that read or modify the system memory lists. Each function maps 1:1
    onto a single API and is independently testable. The orchestration
    that decides *when* to call each wrapper lives in
    :mod:`concinno.memory_relief.engine`.
@dependencies stdlib only (``ctypes``, ``ctypes.wintypes``). No
    third-party deps so the import succeeds on a freshly installed
    Concinno wheel without extras.

Documented APIs called here:

* `GlobalMemoryStatusEx <https://learn.microsoft.com/en-us/windows/win32/api/sysinfoapi/nf-sysinfoapi-globalmemorystatusex>`_
* `GetPerformanceInfo <https://learn.microsoft.com/en-us/windows/win32/api/psapi/nf-psapi-getperformanceinfo>`_
* `EmptyWorkingSet <https://learn.microsoft.com/en-us/windows/win32/api/psapi/nf-psapi-emptyworkingset>`_
* `SetSystemFileCacheSize <https://learn.microsoft.com/en-us/windows/win32/api/memoryapi/nf-memoryapi-setsystemfilecachesize>`_
* `OpenProcess <https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-openprocess>`_
* `OpenProcessToken <https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-openprocesstoken>`_
* `LookupPrivilegeValueW <https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-lookupprivilegevaluew>`_
* `AdjustTokenPrivileges <https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-adjusttokenprivileges>`_

Undocumented APIs (only invoked from ``aggressive`` cleanup tier):

* ``NtQuerySystemInformation(SystemMemoryListInformation, ...)`` — read the
  per-priority standby breakdown.
* ``NtSetSystemInformation(SystemMemoryListInformation, &command, sizeof(int))`` —
  trigger one of four memory-list operations. The four commands are
  defined as :data:`MEMORY_LIST_COMMAND` constants below; their meaning
  is taken from the `RAMMap blog post
  <https://techcommunity.microsoft.com/blog/askperf/introduction-to-the-new-sysinternals-tool-rammap/374717>`_
  and the `m417z ntdoc <https://ntdoc.m417z.com/ntsetsysteminformation>`_
  reverse-engineering reference.

Anti-snake-oil note: the *only* tier of these wrappers we recommend
calling unconditionally is the snapshot family
(:func:`get_memory_snapshot`, :func:`get_performance_info`). Every
mutating API trades one resource for another (RAM% for disk re-read,
working-set size for soft page faults). The engine layer is responsible
for picking which tier is worth that trade.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import platform
from dataclasses import dataclass

logger = logging.getLogger("concinno.memory_relief.core")

_IS_WINDOWS = platform.system() == "Windows"

# ── Snapshot dict keys (stable, used by engine + tool JSON output) ────

WORKING_SET_BYTES = "working_set_bytes"
AVAILABLE_BYTES = "available_bytes"
STANDBY_BYTES = "standby_bytes"
MODIFIED_BYTES = "modified_bytes"
COMMIT_TOTAL_BYTES = "commit_total_bytes"


# ── Privilege constants (winnt.h) ─────────────────────────────────────

SE_PROFILE_SINGLE_PROCESS_NAME = "SeProfileSingleProcessPrivilege"
SE_INCREASE_QUOTA_NAME = "SeIncreaseQuotaPrivilege"

SE_PRIVILEGE_ENABLED = 0x00000002

TOKEN_ADJUST_PRIVILEGES = 0x0020
TOKEN_QUERY = 0x0008

# OpenProcess access flags. We need both QUERY_LIMITED_INFORMATION (cheap)
# and SET_QUOTA (required by EmptyWorkingSet's underlying SetProcessWorkingSetSize).
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_SET_QUOTA = 0x0100

# ── NtSetSystemInformation memory-list commands ────────────────────────
#
# The integer payload that ``NtSetSystemInformation(SystemMemoryListInformation,
# &cmd, sizeof(int))`` interprets. Naming follows the leaked Windows
# Driver Kit headers; see the Opus competitive-research notes for the
# full citation chain. Values stable since Windows Vista.

MEMORY_EMPTY_WORKING_SETS = 2
MEMORY_FLUSH_MODIFIED_LIST = 3
MEMORY_PURGE_STANDBY_LIST = 4
MEMORY_PURGE_LOW_PRIORITY_STANDBY_LIST = 5

#: Mapping of command id → (human label, requires_admin, side-effect tier).
#: Side-effect tiers: 1=cheap, 2=re-read disk possible, 3=force-flush+IO.
MEMORY_LIST_COMMAND = {
    MEMORY_EMPTY_WORKING_SETS: ("empty_working_sets", True, 2),
    MEMORY_FLUSH_MODIFIED_LIST: ("flush_modified_list", True, 3),
    MEMORY_PURGE_STANDBY_LIST: ("purge_standby_list", True, 2),
    MEMORY_PURGE_LOW_PRIORITY_STANDBY_LIST: (
        "purge_low_priority_standby_list",
        True,
        1,
    ),
}

# SYSTEM_INFORMATION_CLASS constants (only the ones we use).
_SYSTEM_PERFORMANCE_INFORMATION = 2
_SYSTEM_POOL_TAG_INFORMATION = 0x16  # 22 — NUCLEAR driver-leak diagnostic
_SYSTEM_MEMORY_LIST_INFORMATION = 0x50
_SYSTEM_COMBINE_PHYSICAL_MEMORY_INFORMATION = 0x82  # NUCLEAR page-combining flush

# NTSTATUS values we care about.
_STATUS_SUCCESS = 0
_STATUS_INFO_LENGTH_MISMATCH = 0xC0000004 - 0x100000000
_STATUS_INVALID_INFO_CLASS = 0xC0000003 - 0x100000000

# NUCLEAR: page-combining flags for SystemCombinePhysicalMemoryInformation.
# Source: Geoff Chappell — MEMORY_COMBINE_INFORMATION reverse-engineering
# https://geoffchappell.com/studies/windows/km/ntoskrnl/api/ex/sysinfo/memory_combine.htm
MMPHYS_COMBINE_DRY_MIGRATION = 0x4

# NUCLEAR: services known safe to stop+start mid-session for DLL/font
# cache eviction. Pulled from Opus 4.7 deep-research report 2026-04-28
# (Memoria 0.4.0 task af4fe4) — every entry has been confirmed restartable
# without triggering BSOD or losing the user's session. The list is
# deliberately short: the cost of a wrong entry is system instability,
# the cost of an extra entry not on the list is ~50 MB of unreleased
# DLL/font cache, so we err on conservative.
SERVICE_CYCLE_SAFELIST: tuple[str, ...] = (
    "Themes",          # UxTheme cache — UI re-styles in <300ms
    "FontCache",       # font working set
    "FontCache3.0.0.0",
    "DPS",             # Diagnostic Policy Service
    # Intentionally NOT included: csrss, dwm, winlogon, LSM, RpcSs,
    # DcomLaunch, Power, EventLog — restart = bluescreen or auto-reboot.
    # SysMain (SuperFetch) handled separately by cycle_superfetch().
    # WSearch only when idle — handled separately because indexing
    # interruption costs more than the cache it frees.
)


# ── Errors ─────────────────────────────────────────────────────────────


class PrivilegeError(PermissionError):
    """Raised when a Win32 call fails specifically because the calling
    process lacks an admin token or could not enable a required
    privilege. Distinct from generic ``OSError`` so the engine can fall
    back to a documented-only cleanup tier instead of bubbling up."""


# ── Snapshot dataclasses ───────────────────────────────────────────────


@dataclass(frozen=True)
class MemorySnapshot:
    """One read of system memory state. All fields are bytes (not pages
    or KB) so the engine can format them uniformly. ``standby_bytes``
    and ``modified_bytes`` are zero on non-Windows or when the
    undocumented query failed; the engine treats those as "unavailable"
    rather than "zero pages standby"."""

    total_bytes: int
    available_bytes: int
    used_bytes: int
    standby_bytes: int
    modified_bytes: int
    commit_total_bytes: int
    commit_limit_bytes: int
    system_cache_bytes: int
    page_size: int
    process_count: int
    handle_count: int

    @property
    def used_percent(self) -> float:
        if self.total_bytes <= 0:
            return 0.0
        return 100.0 * self.used_bytes / self.total_bytes

    @property
    def commit_percent(self) -> float:
        if self.commit_limit_bytes <= 0:
            return 0.0
        return 100.0 * self.commit_total_bytes / self.commit_limit_bytes

    def as_dict(self) -> dict[str, int | float]:
        return {
            "total_bytes": self.total_bytes,
            AVAILABLE_BYTES: self.available_bytes,
            "used_bytes": self.used_bytes,
            "used_percent": round(self.used_percent, 2),
            STANDBY_BYTES: self.standby_bytes,
            MODIFIED_BYTES: self.modified_bytes,
            COMMIT_TOTAL_BYTES: self.commit_total_bytes,
            "commit_limit_bytes": self.commit_limit_bytes,
            "commit_percent": round(self.commit_percent, 2),
            "system_cache_bytes": self.system_cache_bytes,
            "page_size": self.page_size,
            "process_count": self.process_count,
            "handle_count": self.handle_count,
        }


@dataclass(frozen=True)
class PerformanceInfo:
    """Mirror of the Win32 ``PERFORMANCE_INFORMATION`` struct, byte-sized."""

    commit_total: int
    commit_limit: int
    commit_peak: int
    physical_total: int
    physical_available: int
    system_cache: int
    kernel_total: int
    kernel_paged: int
    kernel_nonpaged: int
    page_size: int
    handle_count: int
    process_count: int
    thread_count: int


# ── ctypes structs ─────────────────────────────────────────────────────


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.wintypes.DWORD),
        ("dwMemoryLoad", ctypes.wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_uint64),
        ("ullAvailPhys", ctypes.c_uint64),
        ("ullTotalPageFile", ctypes.c_uint64),
        ("ullAvailPageFile", ctypes.c_uint64),
        ("ullTotalVirtual", ctypes.c_uint64),
        ("ullAvailVirtual", ctypes.c_uint64),
        ("ullAvailExtendedVirtual", ctypes.c_uint64),
    ]


class _PERFORMANCE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.wintypes.DWORD),
        ("CommitTotal", ctypes.c_size_t),
        ("CommitLimit", ctypes.c_size_t),
        ("CommitPeak", ctypes.c_size_t),
        ("PhysicalTotal", ctypes.c_size_t),
        ("PhysicalAvailable", ctypes.c_size_t),
        ("SystemCache", ctypes.c_size_t),
        ("KernelTotal", ctypes.c_size_t),
        ("KernelPaged", ctypes.c_size_t),
        ("KernelNonpaged", ctypes.c_size_t),
        ("PageSize", ctypes.c_size_t),
        ("HandleCount", ctypes.wintypes.DWORD),
        ("ProcessCount", ctypes.wintypes.DWORD),
        ("ThreadCount", ctypes.wintypes.DWORD),
    ]


class _SYSTEM_MEMORY_LIST_INFO(ctypes.Structure):
    """Maps the kernel struct with the same name. Page counts; multiply
    by ``PageSize`` (typically 4096) to get bytes. Field order is taken
    from the leaked WDK headers and matches RAMMap's display."""

    _fields_ = [
        ("ZeroPageCount", ctypes.c_size_t),
        ("FreePageCount", ctypes.c_size_t),
        ("ModifiedPageCount", ctypes.c_size_t),
        ("ModifiedNoWritePageCount", ctypes.c_size_t),
        ("BadPageCount", ctypes.c_size_t),
        ("PageCountByPriority", ctypes.c_size_t * 8),
        ("RepurposedPagesByPriority", ctypes.c_size_t * 8),
        ("ModifiedPageCountPageFile", ctypes.c_size_t),
    ]


class _LUID(ctypes.Structure):
    _fields_ = [
        ("LowPart", ctypes.wintypes.DWORD),
        ("HighPart", ctypes.wintypes.LONG),
    ]


class _LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Luid", _LUID),
        ("Attributes", ctypes.wintypes.DWORD),
    ]


class _TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [
        ("PrivilegeCount", ctypes.wintypes.DWORD),
        ("Privileges", _LUID_AND_ATTRIBUTES * 1),
    ]


# ── Helpers ────────────────────────────────────────────────────────────


def _require_windows() -> None:
    """Guard for any function that genuinely needs the Win32 API. We
    raise ``OSError`` (not ``NotImplementedError``) to keep the failure
    mode aligned with stdlib behaviour for missing OS support."""
    if not _IS_WINDOWS:
        raise OSError(
            "concinno.memory_relief is Windows-only; called on "
            f"{platform.system()!r}"
        )


def is_admin() -> bool:
    """Return True if the current process is running with an elevated
    token. Cheap (no exception path), used by the engine to decide
    whether to attempt an admin-only tier or short-circuit to
    documented-only operations."""
    if not _IS_WINDOWS:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — defensive; never propagate to engine
        return False


def _enable_privilege(name: str) -> bool:
    """Enable a single privilege on the current process token. Returns
    True on success, False otherwise. Does not raise — callers handle
    the missing-privilege case explicitly via the ``False`` return."""
    _require_windows()
    advapi32 = ctypes.windll.advapi32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    h_token = ctypes.wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(),
        TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
        ctypes.byref(h_token),
    ):
        return False
    try:
        luid = _LUID()
        if not advapi32.LookupPrivilegeValueW(
            None,
            ctypes.c_wchar_p(name),
            ctypes.byref(luid),
        ):
            return False
        tp = _TOKEN_PRIVILEGES()
        tp.PrivilegeCount = 1
        tp.Privileges[0].Luid = luid
        tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
        if not advapi32.AdjustTokenPrivileges(
            h_token, False, ctypes.byref(tp), ctypes.sizeof(tp), None, None
        ):
            return False
        # AdjustTokenPrivileges returns success even if some privileges
        # were not assignable; check GetLastError() for ERROR_NOT_ALL_ASSIGNED.
        if ctypes.GetLastError() != 0:
            return False
        return True
    finally:
        kernel32.CloseHandle(h_token)


# ── Snapshot APIs (read-only, no privilege required) ───────────────────


def get_memory_snapshot() -> MemorySnapshot:
    """Combined snapshot via ``GlobalMemoryStatusEx`` + ``GetPerformanceInfo``
    + (best-effort) ``NtQuerySystemInformation(SystemMemoryListInformation)``.

    Standby and modified bytes are zero on non-Windows or when the
    undocumented query was rejected (rare, but happens on hardened
    enterprise installs). All other fields are populated from
    documented APIs and are reliable on every Windows build since 7."""
    if not _IS_WINDOWS:
        # Return a zero snapshot so non-Windows callers (CI, docs build)
        # get a consistent shape instead of an OSError.
        return MemorySnapshot(0, 0, 0, 0, 0, 0, 0, 0, 4096, 0, 0)

    perf = get_performance_info()
    standby_bytes, modified_bytes = _query_memory_list_bytes(perf.page_size)

    return MemorySnapshot(
        total_bytes=perf.physical_total,
        available_bytes=perf.physical_available,
        used_bytes=max(0, perf.physical_total - perf.physical_available),
        standby_bytes=standby_bytes,
        modified_bytes=modified_bytes,
        commit_total_bytes=perf.commit_total,
        commit_limit_bytes=perf.commit_limit,
        system_cache_bytes=perf.system_cache,
        page_size=perf.page_size,
        process_count=perf.process_count,
        handle_count=perf.handle_count,
    )


def get_performance_info() -> PerformanceInfo:
    """Mirror of `GetPerformanceInfo
    <https://learn.microsoft.com/en-us/windows/win32/api/psapi/nf-psapi-getperformanceinfo>`_
    in psapi. All sizes returned in bytes (the API returns pages; we
    multiply by ``PageSize`` once here so callers never have to)."""
    _require_windows()
    psapi = ctypes.windll.psapi  # type: ignore[attr-defined]
    info = _PERFORMANCE_INFORMATION()
    info.cb = ctypes.sizeof(_PERFORMANCE_INFORMATION)
    if not psapi.GetPerformanceInfo(ctypes.byref(info), info.cb):
        raise OSError(
            f"GetPerformanceInfo failed (GetLastError={ctypes.GetLastError()})"
        )

    page = info.PageSize or 4096
    return PerformanceInfo(
        commit_total=info.CommitTotal * page,
        commit_limit=info.CommitLimit * page,
        commit_peak=info.CommitPeak * page,
        physical_total=info.PhysicalTotal * page,
        physical_available=info.PhysicalAvailable * page,
        system_cache=info.SystemCache * page,
        kernel_total=info.KernelTotal * page,
        kernel_paged=info.KernelPaged * page,
        kernel_nonpaged=info.KernelNonpaged * page,
        page_size=page,
        handle_count=info.HandleCount,
        process_count=info.ProcessCount,
        thread_count=info.ThreadCount,
    )


def _query_memory_list_bytes(page_size: int) -> tuple[int, int]:
    """Best-effort standby + modified bytes via undocumented
    ``NtQuerySystemInformation``. Returns ``(0, 0)`` if the query was
    rejected — caller treats that as "unavailable", never "zero"."""
    try:
        ntdll = ctypes.windll.ntdll  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return 0, 0
    info = _SYSTEM_MEMORY_LIST_INFO()
    return_len = ctypes.wintypes.ULONG(0)
    status = ntdll.NtQuerySystemInformation(
        _SYSTEM_MEMORY_LIST_INFORMATION,
        ctypes.byref(info),
        ctypes.sizeof(info),
        ctypes.byref(return_len),
    )
    if status != _STATUS_SUCCESS:
        logger.debug(
            "NtQuerySystemInformation(SystemMemoryListInformation) "
            "returned 0x%08x; standby/modified will be reported as 0",
            status & 0xFFFFFFFF,
        )
        return 0, 0
    standby_pages = sum(info.PageCountByPriority[i] for i in range(8))
    return standby_pages * page_size, info.ModifiedPageCount * page_size


# ── EmptyWorkingSet (per-process, no privilege escalation) ─────────────


def empty_working_set_for_pid(pid: int) -> int:
    """Trim a single process's working set. Returns the number of bytes
    its working set shrank by (best-effort: ``GetProcessMemoryInfo``
    before/after delta); 0 on any failure including missing rights.

    Wraps `EmptyWorkingSet
    <https://learn.microsoft.com/en-us/windows/win32/api/psapi/nf-psapi-emptyworkingset>`_.
    Does **not** raise: callers iterate over many PIDs and a single
    failure (target died, access denied) should not abort the loop."""
    _require_windows()
    if pid <= 0:
        return 0
    psapi = ctypes.windll.psapi  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    h = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SET_QUOTA,
        False,
        pid,
    )
    if not h:
        return 0
    try:
        before = _process_working_set(h)
        if not psapi.EmptyWorkingSet(h):
            return 0
        after = _process_working_set(h)
        return max(0, before - after)
    finally:
        kernel32.CloseHandle(h)


class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.wintypes.DWORD),
        ("PageFaultCount", ctypes.wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _process_working_set(h_process: int) -> int:
    """Bytes currently resident in the process's working set, or 0 on
    failure (handle closed, GetProcessMemoryInfo rejected)."""
    psapi = ctypes.windll.psapi  # type: ignore[attr-defined]
    pmc = _PROCESS_MEMORY_COUNTERS()
    pmc.cb = ctypes.sizeof(_PROCESS_MEMORY_COUNTERS)
    if not psapi.GetProcessMemoryInfo(h_process, ctypes.byref(pmc), pmc.cb):
        return 0
    return int(pmc.WorkingSetSize)


# ── SetSystemFileCacheSize (documented, requires SeIncreaseQuotaPrivilege) ──


def set_system_file_cache_minimal() -> None:
    """Shrink the system file cache to its minimum (``-1, -1, 0``).

    Wraps `SetSystemFileCacheSize
    <https://learn.microsoft.com/en-us/windows/win32/api/memoryapi/nf-memoryapi-setsystemfilecachesize>`_.
    Required when an SMB server / Plex / file server has bloated the
    cache to multiple GB. Raises :class:`PrivilegeError` if the
    SeIncreaseQuotaPrivilege could not be enabled."""
    _require_windows()
    if not _enable_privilege(SE_INCREASE_QUOTA_NAME):
        raise PrivilegeError(
            "SetSystemFileCacheSize requires SeIncreaseQuotaPrivilege "
            "(typically: run as administrator)"
        )
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    minimum = ctypes.c_size_t(-1)
    maximum = ctypes.c_size_t(-1)
    if not kernel32.SetSystemFileCacheSize(minimum, maximum, 0):
        raise OSError(
            f"SetSystemFileCacheSize failed (GetLastError={ctypes.GetLastError()})"
        )


# ── NtSetSystemInformation tier (undocumented, requires admin) ─────────


def _trigger_memory_command(command: int) -> None:
    """Issue one ``NtSetSystemInformation(SystemMemoryListInformation, &cmd)``
    call. The four ``MEMORY_*`` constants above are the only valid
    inputs; passing anything else raises ``ValueError`` so a typo can't
    silently corrupt kernel state."""
    if command not in MEMORY_LIST_COMMAND:
        raise ValueError(
            f"unknown SystemMemoryListInformation command {command!r}; "
            f"expected one of {sorted(MEMORY_LIST_COMMAND)}"
        )
    _require_windows()
    if not is_admin():
        raise PrivilegeError(
            f"command {MEMORY_LIST_COMMAND[command][0]!r} requires admin "
            "(elevated process token)"
        )
    if not _enable_privilege(SE_PROFILE_SINGLE_PROCESS_NAME):
        raise PrivilegeError(
            f"command {MEMORY_LIST_COMMAND[command][0]!r} requires "
            "SeProfileSingleProcessPrivilege (could not enable on token)"
        )
    ntdll = ctypes.windll.ntdll  # type: ignore[attr-defined]
    cmd_buf = ctypes.c_int(command)
    status = ntdll.NtSetSystemInformation(
        _SYSTEM_MEMORY_LIST_INFORMATION,
        ctypes.byref(cmd_buf),
        ctypes.sizeof(cmd_buf),
    )
    if status != _STATUS_SUCCESS:
        raise OSError(
            f"NtSetSystemInformation({MEMORY_LIST_COMMAND[command][0]}) "
            f"returned NTSTATUS 0x{status & 0xFFFFFFFF:08x}"
        )


def purge_standby_list() -> None:
    """Drop every page on the standby list. Side-effect tier 2: every
    file-cache page that was on standby will be re-read from disk on
    next access. Use only when standby is genuinely bloated and the
    user accepts the IO trade-off."""
    _trigger_memory_command(MEMORY_PURGE_STANDBY_LIST)


def purge_low_priority_standby_list() -> None:
    """Drop only priority-0 standby pages (those Windows itself has
    flagged as least valuable). Side-effect tier 1: this is the safest
    aggressive operation — almost no IO penalty because only repurposed
    or never-touched pages get evicted."""
    _trigger_memory_command(MEMORY_PURGE_LOW_PRIORITY_STANDBY_LIST)


def purge_modified_page_list() -> None:
    """Force-flush the modified page list to its backing store
    (pagefile / mapped file), then move the pages to standby. Side-
    effect tier 3: causes a write IO burst. Useful before ``purge_standby_list``
    if standby has already been thoroughly evicted but commit pressure
    is still high."""
    _trigger_memory_command(MEMORY_FLUSH_MODIFIED_LIST)


def empty_all_working_sets_via_nt() -> None:
    """Trim every process's working set via the kernel. Equivalent to
    iterating :func:`empty_working_set_for_pid` over all PIDs but in a
    single kernel call. Side-effect tier 2: most pages will page back
    in within seconds, so this is best paired with the standby purge
    that follows it (otherwise you just churn the standby list)."""
    _trigger_memory_command(MEMORY_EMPTY_WORKING_SETS)


# ── NUCLEAR tier helpers (Memoria 0.4.0) ───────────────────────────────


@dataclass(frozen=True)
class PoolTagEntry:
    """One row from ``NtQuerySystemInformation(SystemPoolTagInformation)``.

    ``tag`` is the 4-byte ASCII allocation tag (e.g. ``"Stdq"`` for
    ``netio.sys`` networking allocations). ``nonpaged_used_bytes`` is the
    most actionable column for driver-leak hunting; the paged columns
    are reported for completeness. Tag → likely-driver mapping lives in
    a separate static lookup so this primitive stays string-agnostic."""

    tag: str
    paged_allocs: int
    paged_frees: int
    paged_used_bytes: int
    nonpaged_allocs: int
    nonpaged_frees: int
    nonpaged_used_bytes: int


class _SYSTEM_POOLTAG(ctypes.Structure):
    _fields_ = [
        ("Tag", ctypes.c_ubyte * 4),
        ("PagedAllocs", ctypes.wintypes.ULONG),
        ("PagedFrees", ctypes.wintypes.ULONG),
        ("PagedUsed", ctypes.c_size_t),
        ("NonPagedAllocs", ctypes.wintypes.ULONG),
        ("NonPagedFrees", ctypes.wintypes.ULONG),
        ("NonPagedUsed", ctypes.c_size_t),
    ]


def query_pool_tags() -> list[PoolTagEntry]:
    """Read every kernel pool allocation tag via the same syscall
    ``poolmon.exe`` uses internally. Returns empty list on any failure
    (non-Windows, no SeDebugPrivilege, hardened build).

    Used by NUCLEAR tier's pre-flight driver-leak diagnostic to detect
    "RAM consumed but no big app open" cases that no flush can fix —
    the only honest cure is to update the buggy driver or reboot. By
    surfacing the leak signature ("`Stdq` tag grew 8.2 GB since boot"),
    the user understands why Memoria can't reclaim it; otherwise NUCLEAR
    looks like it failed when actually the OS is leaking.

    Privilege: typically requires ``SeDebugPrivilege``. We fall back
    silently to an empty list when denied — caller treats that as
    "diagnostic unavailable", never "no leaks present"."""
    if not _IS_WINDOWS:
        return []
    try:
        ntdll = ctypes.windll.ntdll  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return []
    # Header is one ULONG count followed by SYSTEM_POOLTAG[Count].
    buf_size = 256 * 1024
    return_len = ctypes.wintypes.ULONG(0)
    for _ in range(8):
        buf = (ctypes.c_ubyte * buf_size)()
        status = ntdll.NtQuerySystemInformation(
            _SYSTEM_POOL_TAG_INFORMATION,
            buf, buf_size, ctypes.byref(return_len),
        )
        if status == _STATUS_SUCCESS:
            break
        if status & 0xFFFFFFFF == _STATUS_INFO_LENGTH_MISMATCH & 0xFFFFFFFF:
            buf_size = max(int(return_len.value) + 64 * 1024, buf_size * 2)
            continue
        logger.debug(
            "NtQuerySystemInformation(SystemPoolTagInformation) "
            "returned 0x%08x; pool-tag diagnostic unavailable",
            status & 0xFFFFFFFF,
        )
        return []
    else:
        return []
    base = ctypes.addressof(buf)
    count = ctypes.cast(base, ctypes.POINTER(ctypes.wintypes.ULONG))[0]
    pool_tag_size = ctypes.sizeof(_SYSTEM_POOLTAG)
    array_base = base + ctypes.sizeof(ctypes.wintypes.ULONG)
    out: list[PoolTagEntry] = []
    for i in range(count):
        try:
            entry = _SYSTEM_POOLTAG.from_address(array_base + i * pool_tag_size)
        except (ValueError, OSError):
            break
        tag_bytes = bytes(entry.Tag).rstrip(b"\x00")
        try:
            tag_str = tag_bytes.decode("ascii", errors="replace").strip()
        except Exception:
            tag_str = ""
        if not tag_str:
            continue
        out.append(PoolTagEntry(
            tag=tag_str,
            paged_allocs=int(entry.PagedAllocs),
            paged_frees=int(entry.PagedFrees),
            paged_used_bytes=int(entry.PagedUsed),
            nonpaged_allocs=int(entry.NonPagedAllocs),
            nonpaged_frees=int(entry.NonPagedFrees),
            nonpaged_used_bytes=int(entry.NonPagedUsed),
        ))
    out.sort(key=lambda e: -(e.nonpaged_used_bytes + e.paged_used_bytes))
    return out


class _MEMORY_COMBINE_INFORMATION_EX(ctypes.Structure):
    """Layout per Geoff Chappell's reverse-engineering reference. Field
    sizes match Windows 10+ 64-bit kernel; older Win10 builds may not
    accept this class at all (we feature-detect via NTSTATUS)."""

    _fields_ = [
        ("Handle", ctypes.c_void_p),
        ("PagesCombined", ctypes.c_size_t),
        ("Flags", ctypes.wintypes.ULONG),
    ]


def purge_combined_memory_list() -> int:
    """Trigger one page-combining defrag pass. Returns the number of
    pages combined (best-effort) or 0 when the API is unavailable on
    this build.

    Wraps ``NtSetSystemInformation(SystemCombinePhysicalMemoryInformation,
    &info, sizeof)`` with ``Flags=MMPHYS_COMBINE_DRY_MIGRATION``. Class
    ``0x82`` per Geoff Chappell. **Some Win10 24H2 builds disable this
    class** — we feature-detect by NTSTATUS and treat as "unavailable"
    (caller must not crash on zero return)."""
    _require_windows()
    if not is_admin():
        raise PrivilegeError(
            "purge_combined_memory_list requires admin token"
        )
    if not _enable_privilege(SE_PROFILE_SINGLE_PROCESS_NAME):
        raise PrivilegeError(
            "purge_combined_memory_list requires "
            "SeProfileSingleProcessPrivilege"
        )
    ntdll = ctypes.windll.ntdll  # type: ignore[attr-defined]
    info = _MEMORY_COMBINE_INFORMATION_EX()
    info.Handle = None
    info.PagesCombined = 0
    info.Flags = MMPHYS_COMBINE_DRY_MIGRATION
    status = ntdll.NtSetSystemInformation(
        _SYSTEM_COMBINE_PHYSICAL_MEMORY_INFORMATION,
        ctypes.byref(info), ctypes.sizeof(info),
    )
    if status != _STATUS_SUCCESS:
        # STATUS_INVALID_INFO_CLASS / STATUS_NOT_IMPLEMENTED: build
        # disabled the class. Don't raise — log and return 0 so the
        # NUCLEAR sequence skips this stage cleanly.
        logger.info(
            "page-combining flush unavailable (NTSTATUS 0x%08x); "
            "skipping — not all Win10/11 builds expose class 0x82",
            status & 0xFFFFFFFF,
        )
        return 0
    return int(info.PagesCombined)


def cycle_service(
    name: str,
    *,
    timeout_seconds: float = 10.0,
    settle_seconds: float = 0.3,
) -> tuple[bool, str]:
    """Stop a Windows service then restart it. Used by NUCLEAR to evict
    DLL/font cache held in svchost session pool. Returns
    ``(ok, message)``. ``ok=False`` on any failure including missing
    privilege; never raises so the NUCLEAR sequence never aborts on
    one bad service.

    Uses ``sc.exe`` rather than the ``win32service`` Python binding so
    Memoria has zero pywin32 runtime dependency on a fresh install.
    Both invocations are short-lived subprocesses; total wall-clock
    typically 800ms - 2s."""
    if not _IS_WINDOWS:
        return False, "non-windows"
    if not is_admin():
        return False, "needs_admin"
    if not name or any(c in name for c in (" ", '"', "'", "\\", "/")):
        # Hard reject malformed names — never let an attacker-controlled
        # service name reach sc.exe.
        return False, "invalid_name"
    if name not in SERVICE_CYCLE_SAFELIST and name != "SysMain":
        return False, f"refused: {name!r} not on safelist"
    import subprocess
    import time as _time
    try:
        subprocess.run(
            ["sc", "stop", name],
            capture_output=True, text=True,
            timeout=timeout_seconds,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return False, "stop timeout"
    except OSError as exc:
        return False, f"stop spawn failed: {exc}"
    # sc returns non-zero when service was already stopped (1062). That's
    # fine — proceed to start so we end in the desired "running" state.
    _time.sleep(max(0.0, settle_seconds))
    try:
        start = subprocess.run(
            ["sc", "start", name],
            capture_output=True, text=True,
            timeout=timeout_seconds,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return False, "start timeout"
    except OSError as exc:
        return False, f"start spawn failed: {exc}"
    if start.returncode != 0:
        return False, f"start exit={start.returncode}: {start.stderr[:200].strip()}"
    return True, "cycled"


def cycle_superfetch(*, timeout_seconds: float = 10.0) -> tuple[bool, str]:
    """Cycle the SuperFetch / SysMain service to evict its prefetched
    standby pages. Same wall-clock and risk profile as
    :func:`cycle_service` but separated so callers can opt in/out
    independently — SuperFetch shed has the largest single reclaim
    (1-3 GB typical) but the next app launch may be slower for a few
    minutes until SysMain re-warms its prefetch heuristics."""
    return cycle_service(
        "SysMain",
        timeout_seconds=timeout_seconds,
        settle_seconds=2.0,
    )


def shutdown_wsl(*, timeout_seconds: float = 30.0) -> tuple[bool, str]:
    """Run ``wsl --shutdown`` to release the Hyper-V vmmem balloon that
    Docker Desktop / WSL2 keep holding after containers stop. Returns
    ``(ok, message)``. Opt-in only at the engine layer because it
    interrupts any running dev container — but for users who just
    closed Docker and want their RAM back, it is the only user-mode
    cure for the leaked vmmem allocation."""
    if not _IS_WINDOWS:
        return False, "non-windows"
    import subprocess
    try:
        result = subprocess.run(
            ["wsl", "--shutdown"],
            capture_output=True, text=True,
            timeout=timeout_seconds,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError:
        return False, "wsl not installed"
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except OSError as exc:
        return False, f"spawn failed: {exc}"
    if result.returncode != 0:
        return False, (
            f"exit={result.returncode}: {result.stderr[:200].strip()}"
        )
    return True, "wsl shutdown"


__all__ = [
    # Constants
    "AVAILABLE_BYTES",
    "COMMIT_TOTAL_BYTES",
    "MEMORY_EMPTY_WORKING_SETS",
    "MEMORY_FLUSH_MODIFIED_LIST",
    "MEMORY_LIST_COMMAND",
    "MEMORY_PURGE_LOW_PRIORITY_STANDBY_LIST",
    "MEMORY_PURGE_STANDBY_LIST",
    "MODIFIED_BYTES",
    "SE_INCREASE_QUOTA_NAME",
    "SE_PROFILE_SINGLE_PROCESS_NAME",
    "STANDBY_BYTES",
    "WORKING_SET_BYTES",
    # Errors
    "PrivilegeError",
    # Models
    "MemorySnapshot",
    "PerformanceInfo",
    # Snapshot APIs
    "get_memory_snapshot",
    "get_performance_info",
    "is_admin",
    # Mutating APIs
    "empty_all_working_sets_via_nt",
    "empty_working_set_for_pid",
    "purge_low_priority_standby_list",
    "purge_modified_page_list",
    "purge_standby_list",
    "set_system_file_cache_minimal",
]
