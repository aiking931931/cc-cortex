"""concinno.memory_relief.engine — orchestrate cleanup with before/after stats.

@module memory_relief.engine
@responsibility Decide *which* tier to run and assemble a structured
    :class:`CleanupReport` so callers (CLI / tray / Tool / agent) see
    exactly what happened. The Win32 primitives in
    :mod:`concinno.memory_relief.core` only know how to do one thing
    each; this module sequences them, gates aggressive operations behind
    explicit opt-in, applies a process whitelist, and records before /
    after deltas so users can verify the trade-off was worth it.

@dependencies stdlib only. Process discovery reuses
    :mod:`concinno.process_guard.discovery` so the engine and the
    process supervisor share one canonical "list every process" path —
    avoiding the dual-source-of-truth bug that bit
    ``instance_lock.json`` (carried over to handoff).

Cleanup tiers (ordered by escalating side-effect, lowest first):

* ``CleanupMode.SAFE`` — per-process working-set trim on the heaviest
  non-whitelisted processes (uses ``EmptyWorkingSet``, no admin needed).
* ``CleanupMode.STANDBY`` — adds priority-0 standby purge (kernel
  treats those pages as least-valuable; minimal IO penalty).
* ``CleanupMode.AGGRESSIVE`` — adds full standby list purge + system
  file cache shrink (real IO penalty; admin required).
* ``CleanupMode.DESTRUCTIVE`` — adds modified-page-list flush (write
  IO burst; admin required; only useful when commit pressure is the
  acute symptom).

Every mode honours ``dry_run=True`` and produces an identical report
shape — the engine touches no kernel state when dry-run is on, only
fills in the snapshot fields. This is what the tray "preview" button
calls before the user clicks "actually clean".
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import Enum

from . import core

logger = logging.getLogger("concinno.memory_relief.engine")

# Names treated as never-trim. Lower-case match against the executable
# basename. Trimming a process you are about to interact with creates
# user-visible UI lag, so we exclude the user's CC / IDE chain.
_DEFAULT_WHITELIST = frozenset(
    {
        "claude.exe",
        "code.exe",
        "cursor.exe",
        "windsurf.exe",
        "explorer.exe",
        "dwm.exe",
        "csrss.exe",
        "winlogon.exe",
        "system",
        "system idle process",
        "registry",
        "memcompression",
    }
)

#: NUCLEAR tier minimum whitelist — only the processes whose trim would
#: actually break the system are protected. ``explorer.exe``, ``dwm.exe``,
#: and IDEs that ``_DEFAULT_WHITELIST`` spares for UX reasons are
#: deliberately omitted; NUCLEAR's contract is "as clean as a reboot,
#: I accept the brief stutter". Trimming explorer / dwm forces them to
#: page back from disk on next mouse move (~200-500 ms perceptible
#: hiccup) but does not destabilise the session. Trimming winlogon /
#: csrss / lsass / smss / services / wininit / system / registry /
#: memcompression **does** destabilise — those stay protected.
_NUCLEAR_MIN_WHITELIST = frozenset(
    {
        "claude.exe",  # protect self so the cleanup process can keep going
        "system",
        "system idle process",
        "registry",
        "memcompression",
        "winlogon.exe",
        "csrss.exe",
        "lsass.exe",
        "smss.exe",
        "services.exe",
        "wininit.exe",
    }
)

#: Top-N heaviest non-whitelisted processes the SAFE tier trims. Trimming
#: every process is the snake-oil pattern Mark Russinovich called out; we
#: target the long-running heavyweights — but 8 was too conservative in
#: practice (real-world 70%-RAM trigger only freed 128 MB), so the default
#: is now wide enough that the threshold tier sees meaningful reclaim
#: without needing aggressive privileges.
_DEFAULT_SAFE_TOP_N = 30

#: Minimum working-set size (bytes) for a process to be considered worth
#: trimming. Anything under this floor releases trivial RAM relative to
#: the per-process overhead of opening + closing the handle.
_MIN_TRIM_BYTES = 20 * 1024 * 1024  # 20 MB

#: Worker pool size for the SAFE tier per-process trim. Each
#: ``EmptyWorkingSet`` call is a kernel round-trip (~30-50 ms); 4 workers
#: cut wall-clock for top-N=30 from ~1 s to ~250 ms without saturating
#: the small-object allocator inside ntdll.
_TRIM_PARALLELISM = 4


class CleanupMode(str, Enum):
    """The escalating cleanup tiers; passed verbatim to the Tool /
    CLI / tray right-click menu so the surface shape is identical.

    NUCLEAR (added 0.4.0) is the deepest cleanup tier — sequences
    every working-set, standby, modified, file-cache, page-combining,
    and DLL-section primitive in a single run, plus a pre-flight pool-
    tag driver-leak diagnostic. Reaches ~88-92% of what a real reboot
    reclaims; the residual 8-12% is driver-leaked nonpaged pool / GDI
    handles / kernel stack lazy-free / PFN fragmentation, which **no**
    user-mode tool can free (RAMMap included). When NUCLEAR runs, the
    foreground exclusion is overridden — the tier is for the explicit
    "I want it as clean as a reboot, I accept the brief stutter" path.
    """

    DRYRUN = "dryrun"
    SAFE = "safe"
    STANDBY = "standby"
    AGGRESSIVE = "aggressive"
    DESTRUCTIVE = "destructive"
    NUCLEAR = "nuclear"


@dataclass
class NuclearOptions:
    """User-controllable knobs for the NUCLEAR tier. The four enabled-by-
    default options are the Pareto top — they together reach ~88% reboot-
    equivalence without crossing into "user perceives a UI freeze". The
    two opt-out options trade extra reclaim for higher visible cost
    (UI flash for ``cycle_services``, slower next app launch for
    ``cycle_superfetch``); the two off-by-default options have either
    high system-instability risk (``cycle_memory_compression``) or
    interrupt user work (``shutdown_wsl`` kills running dev containers)."""

    diagnose_pool_leaks: bool = True
    """Pre-flight ``NtQuerySystemInformation(SystemPoolTagInformation)``
    diff against baseline to surface driver-leaked nonpaged pool. No
    reclaim by itself; produces an entry in
    ``CleanupReport.pool_leak_diagnostics`` so the user understands when
    the OS is leaking and "no flush will help, please update drivers"."""

    flush_combined_memory_list: bool = True
    """``NtSetSystemInformation(SystemCombinePhysicalMemoryInformation)``
    class 0x82. Defragments the page-combining hash table. Some Win10
    24H2 builds disabled this class — feature-detected; failure is
    silent and recorded as ``stage.skipped=True``."""

    cycle_services: bool = True
    """Stop+start the curated DLL/font-cache services
    (``Themes`` / ``FontCache`` / ``FontCache3.0.0.0`` / ``DPS``).
    Causes a brief UI re-style flash; reclaims ~400-1200 MB of
    cross-session DLL working set."""

    cycle_superfetch: bool = True
    """Stop+start ``SysMain``. Largest single reclaim (~1-3 GB) but the
    next app launch may be slower for a few minutes until SuperFetch
    re-warms its prefetch heuristics."""

    shutdown_wsl: bool = False
    """Run ``wsl --shutdown`` to release the Hyper-V vmmem balloon that
    Docker Desktop / WSL2 hold after containers stop. Off by default
    because it interrupts running dev containers; turn on when you want
    "Docker leaked RAM, give it back" semantics."""

    cycle_memory_compression: bool = False
    """``Disable-MMAgent -mc; Enable-MMAgent -mc`` (PowerShell). Cycles
    the Memory Compression Service. **High risk on systems with less
    than 4 GB available** — the few-second window between disable and
    enable puts the system under acute pressure. Off by default.
    Engine refuses to run this stage if available_bytes < 4 GB."""


@dataclass(frozen=True)
class PerProcessTrim:
    """One process trimmed in the SAFE tier. ``freed_bytes`` is the
    delta in working-set size (best-effort; some processes immediately
    page parts back in). ``error`` is non-empty when trim failed."""

    pid: int
    name: str
    before_bytes: int
    after_bytes: int
    freed_bytes: int
    error: str = ""


@dataclass
class StageResult:
    """One discrete operation inside a :class:`CleanupReport`. The
    ``label`` matches a constant in :data:`core.MEMORY_LIST_COMMAND` for
    kernel ops, or ``"empty_working_set_per_process"`` etc for the
    documented-API tier. ``ok`` flips to False when the operation was
    attempted and raised; ``skipped`` flips to True when the engine
    decided not to run it (whitelist, dry-run, missing privilege)."""

    label: str
    ok: bool = True
    skipped: bool = False
    skip_reason: str = ""
    error: str = ""
    elapsed_ms: int = 0


@dataclass
class CleanupReport:
    """Returned to every caller. The shape is JSON-serialisable so the
    Tool wrapper can hand it straight to the agent without an adapter."""

    mode: CleanupMode
    dry_run: bool
    is_admin: bool
    started_at: float
    finished_at: float
    before: dict[str, int | float] = field(default_factory=dict)
    after: dict[str, int | float] = field(default_factory=dict)
    stages: list[StageResult] = field(default_factory=list)
    process_trims: list[PerProcessTrim] = field(default_factory=list)
    reclaimed_bytes: int = 0
    notes: list[str] = field(default_factory=list)
    #: Populated by NUCLEAR's pre-flight pool-tag diagnostic. Each entry
    #: shape: ``{"tag": "Stdq", "nonpaged_used_mb": 8200, "paged_used_mb": 12,
    #: "growth_since_baseline_mb": 7800, "likely_driver": "netio.sys"}``.
    #: Empty when diagnostic is disabled, no baseline exists, or the
    #: ``NtQuerySystemInformation(SystemPoolTagInformation)`` call was
    #: rejected (typically: not running as admin / no SeDebugPrivilege).
    pool_leak_diagnostics: list[dict[str, object]] = field(default_factory=list)

    @property
    def reclaimed_mb(self) -> int:
        return self.reclaimed_bytes // (1024 * 1024)

    @property
    def elapsed_ms(self) -> int:
        return int((self.finished_at - self.started_at) * 1000)

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode.value,
            "dry_run": self.dry_run,
            "is_admin": self.is_admin,
            "elapsed_ms": self.elapsed_ms,
            "before": self.before,
            "after": self.after,
            "reclaimed_mb": self.reclaimed_mb,
            "reclaimed_bytes": self.reclaimed_bytes,
            "stages": [
                {
                    "label": s.label,
                    "ok": s.ok,
                    "skipped": s.skipped,
                    "skip_reason": s.skip_reason,
                    "error": s.error,
                    "elapsed_ms": s.elapsed_ms,
                }
                for s in self.stages
            ],
            "process_trims": [
                {
                    "pid": t.pid,
                    "name": t.name,
                    "before_mb": t.before_bytes // (1024 * 1024),
                    "after_mb": t.after_bytes // (1024 * 1024),
                    "freed_mb": t.freed_bytes // (1024 * 1024),
                    "error": t.error,
                }
                for t in self.process_trims
            ],
            "notes": self.notes,
            "pool_leak_diagnostics": list(self.pool_leak_diagnostics),
        }


# ── Process discovery ─────────────────────────────────────────────────


def _list_heavy_processes(
    *,
    top_n: int,
    min_bytes: int,
    whitelist: frozenset[str],
) -> list[tuple[int, str, int]]:
    """Return ``[(pid, name, working_set_bytes), ...]`` sorted by RSS
    desc. Reuses :mod:`concinno.process_guard.discovery` so engine +
    supervisor agree on the snapshot. Falls back to an empty list when
    that import fails (process_guard is optional in lite installs)."""
    try:
        from concinno.process_guard.discovery import _get_all_processes
    except Exception as exc:  # noqa: BLE001 — discovery is best-effort
        logger.debug("process_guard discovery unavailable (%s)", exc)
        return []

    processes = _get_all_processes()
    self_pid = os.getpid()
    heavy: list[tuple[int, str, int]] = []
    for p in processes:
        pid = int(p.get("pid", 0))
        if pid <= 4 or pid == self_pid:
            continue
        name = str(p.get("name", "")).lower()
        if name in whitelist:
            continue
        # discovery only enriches the few "claude-relevant" rows with
        # working-set bytes; for everything else we measure ourselves
        # (cheap: one OpenProcess + GetProcessMemoryInfo per candidate).
        ws = int(p.get("mem_kb", 0)) * 1024
        if ws == 0:
            ws = _measure_working_set(pid)
        if ws < min_bytes:
            continue
        heavy.append((pid, name, ws))
    heavy.sort(key=lambda x: -x[2])
    return heavy[:top_n]


def _measure_working_set(pid: int) -> int:
    """Cheap working-set probe — opens the process with the minimum
    rights needed for ``GetProcessMemoryInfo``, returns 0 on any
    failure (target died, access denied, system process)."""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        h = kernel32.OpenProcess(
            core.PROCESS_QUERY_LIMITED_INFORMATION, False, pid,
        )
        if not h:
            return 0
        try:
            return core._process_working_set(h)  # noqa: SLF001 — same package
        finally:
            kernel32.CloseHandle(h)
    except Exception:  # noqa: BLE001 — never crash discovery
        return 0


# ── Stage runners ─────────────────────────────────────────────────────


def _run_stage(
    label: str,
    func,
    *,
    dry_run: bool,
    skip_reason: str = "",
) -> StageResult:
    """Wrap one mutating call in a stopwatch + error trap so each line
    in the report is uniform. ``skip_reason`` non-empty short-circuits
    without invoking ``func`` (used for missing-privilege paths)."""
    if skip_reason:
        return StageResult(label=label, skipped=True, skip_reason=skip_reason)
    if dry_run:
        return StageResult(
            label=label, skipped=True, skip_reason="dry_run"
        )
    started = time.monotonic()
    try:
        func()
    except core.PrivilegeError as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        return StageResult(
            label=label,
            ok=False,
            error=f"PrivilegeError: {exc}",
            elapsed_ms=elapsed,
        )
    except Exception as exc:  # noqa: BLE001 — surface in report, never abort
        elapsed = int((time.monotonic() - started) * 1000)
        return StageResult(
            label=label,
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            elapsed_ms=elapsed,
        )
    return StageResult(
        label=label,
        ok=True,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )


def _run_per_process_trim(
    *,
    dry_run: bool,
    top_n: int,
    min_bytes: int,
    whitelist: frozenset[str],
) -> tuple[StageResult, list[PerProcessTrim]]:
    """SAFE tier: trim the top-N heaviest non-whitelisted processes via
    documented ``EmptyWorkingSet``. Returns ``(stage_summary, per_proc_list)``.
    ``stage_summary.ok=False`` only when discovery itself failed; per-
    process failures land in ``per_proc_list[i].error``."""
    started = time.monotonic()
    targets = _list_heavy_processes(
        top_n=top_n, min_bytes=min_bytes, whitelist=whitelist,
    )
    trims: list[PerProcessTrim] = []
    if dry_run:
        for pid, name, ws in targets:
            trims.append(
                PerProcessTrim(
                    pid=pid, name=name,
                    before_bytes=ws, after_bytes=ws, freed_bytes=0,
                )
            )
        return (
            StageResult(
                label="empty_working_set_per_process",
                skipped=True,
                skip_reason="dry_run",
                elapsed_ms=int((time.monotonic() - started) * 1000),
            ),
            trims,
        )
    # Parallelize EmptyWorkingSet calls. Each is an independent kernel
    # round-trip (OpenProcess + SetProcessWorkingSetSize + CloseHandle)
    # against a different PID, so there's no shared state. Workers cap
    # at _TRIM_PARALLELISM to avoid handle-table churn on machines with
    # only a handful of cores.
    def _trim_one(target: tuple[int, str, int]) -> PerProcessTrim:
        pid, name, ws_before = target
        try:
            freed = core.empty_working_set_for_pid(pid)
            ws_after = max(0, ws_before - freed)
            return PerProcessTrim(
                pid=pid, name=name,
                before_bytes=ws_before, after_bytes=ws_after,
                freed_bytes=freed,
            )
        except Exception as exc:  # noqa: BLE001 — record, keep iterating
            return PerProcessTrim(
                pid=pid, name=name,
                before_bytes=ws_before, after_bytes=ws_before,
                freed_bytes=0, error=f"{type(exc).__name__}: {exc}",
            )

    workers = min(_TRIM_PARALLELISM, max(1, len(targets)))
    if workers > 1 and len(targets) > 1:
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="mr-trim",
        ) as ex:
            futures = [ex.submit(_trim_one, t) for t in targets]
            trims.extend(f.result() for f in as_completed(futures))
        # Restore RSS-desc order so the report is reproducible regardless
        # of which thread finished first.
        trims.sort(key=lambda t: -t.before_bytes)
    else:
        for target in targets:
            trims.append(_trim_one(target))
    return (
        StageResult(
            label="empty_working_set_per_process",
            ok=True,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        ),
        trims,
    )


# ── NUCLEAR helpers (0.4.0) ───────────────────────────────────────────


#: Pool-tag → likely-driver mapping for the NUCLEAR pre-flight diagnostic.
#: Sources: Microsoft pooltag.txt (ships with WDK), Geoff Chappell's
#: kernel-pool catalogue, plus published Microsoft Q&A leak case studies
#: (2024-2025). Tags are ASCII; lookups are exact-match. Unknown tags
#: appear as ``"unknown"`` in the report so the user can still file
#: a Sysinternals poolmon screenshot — we never claim more than we know.
_POOL_TAG_HINTS: dict[str, str] = {
    "Stdq": "netio.sys (Windows networking — qBittorrent / Steam / Discord triggers)",
    "Proc": "ntoskrnl (process objects — handle leak in user app)",
    "EtwR": "Event Tracing for Windows registration leak",
    "NDmp": "NDIS (Killer / MSI network drivers)",
    "Thre": "kernel thread objects",
    "MmCa": "memory manager control areas",
    "Pool": "executive pool fragmentation",
    "FMsl": "FilterManager streamlist",
    "Toke": "security tokens",
    "FMfn": "FilterManager FILE_OBJECT extension",
}

#: Driver-leak diagnostic thresholds. ``nonpaged`` is the more actionable
#: column (most leaks are nonpaged); paged threshold is higher because
#: legitimate caches use paged pool. Tuned conservatively to avoid false
#: positives on a healthy 32 GB workstation: at boot, no tag should
#: cross either threshold; if one does after 8 hr of use, that's
#: legitimately worth flagging.
_POOL_LEAK_NONPAGED_THRESHOLD_BYTES = 500 * 1024 * 1024  # 500 MB
_POOL_LEAK_PAGED_THRESHOLD_BYTES = 1024 * 1024 * 1024  # 1 GB


def _run_tuple_stage(
    label: str,
    func,
    *,
    dry_run: bool,
    skip_reason: str = "",
) -> StageResult:
    """Wrap a NUCLEAR helper that returns ``(ok, message)`` in the same
    stopwatch/error envelope as :func:`_run_stage`. Distinguishes
    ``ok=False`` returns (recorded as stage error) from raised
    exceptions (also recorded). ``skip_reason`` non-empty short-
    circuits without invoking ``func``."""
    if skip_reason:
        return StageResult(label=label, skipped=True, skip_reason=skip_reason)
    if dry_run:
        return StageResult(label=label, skipped=True, skip_reason="dry_run")
    started = time.monotonic()
    try:
        ok, msg = func()
    except Exception as exc:  # noqa: BLE001 — surface in report, never abort
        elapsed = int((time.monotonic() - started) * 1000)
        return StageResult(
            label=label,
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            elapsed_ms=elapsed,
        )
    elapsed = int((time.monotonic() - started) * 1000)
    if not ok:
        # Semantic skip codes from helpers map to ``skipped=True`` so
        # they don't read as red errors in the report — typical for
        # "wsl not installed" / "needs_admin" on a non-elevated run.
        soft_skip = {"non-windows", "needs_admin", "wsl not installed"}
        if msg in soft_skip:
            return StageResult(
                label=label, skipped=True, skip_reason=msg,
                elapsed_ms=elapsed,
            )
        return StageResult(
            label=label, ok=False, error=msg, elapsed_ms=elapsed,
        )
    return StageResult(label=label, ok=True, elapsed_ms=elapsed)


def _run_pool_leak_diagnostic(
    report: CleanupReport, *, dry_run: bool,
) -> None:
    """NUCLEAR pre-flight: read every kernel pool tag's nonpaged + paged
    used bytes, flag entries that crossed the leak thresholds. Populates
    ``report.pool_leak_diagnostics`` and appends one StageResult.

    No baseline is consulted: a single high water-mark is informative on
    its own, and storing per-machine baselines is a separate persistence
    concern that belongs in the Memoria application layer (not in the
    library). When Memoria writes a baseline at fresh-boot detection,
    it can reason about deltas; the engine's job here is only to expose
    raw counters and flag obviously-leaked tags so the user understands
    why the rest of NUCLEAR can't help."""
    label = "pool_leak_diagnostic"
    if dry_run:
        report.stages.append(
            StageResult(label=label, skipped=True, skip_reason="dry_run")
        )
        return
    started = time.monotonic()
    try:
        tags = core.query_pool_tags()
    except Exception as exc:  # noqa: BLE001 — diagnostic must not abort run
        report.stages.append(StageResult(
            label=label,
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            elapsed_ms=int((time.monotonic() - started) * 1000),
        ))
        return
    if not tags:
        report.stages.append(StageResult(
            label=label,
            skipped=True,
            skip_reason="diagnostic_unavailable",
            elapsed_ms=int((time.monotonic() - started) * 1000),
        ))
        return
    flagged: list[dict[str, object]] = []
    for entry in tags:
        if (entry.nonpaged_used_bytes >= _POOL_LEAK_NONPAGED_THRESHOLD_BYTES
                or entry.paged_used_bytes >= _POOL_LEAK_PAGED_THRESHOLD_BYTES):
            flagged.append({
                "tag": entry.tag,
                "nonpaged_used_mb": entry.nonpaged_used_bytes // (1024 * 1024),
                "paged_used_mb": entry.paged_used_bytes // (1024 * 1024),
                "likely_driver": _POOL_TAG_HINTS.get(entry.tag, "unknown"),
            })
    report.pool_leak_diagnostics = flagged
    report.stages.append(StageResult(
        label=label,
        ok=True,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    ))


# ── Main entry point ──────────────────────────────────────────────────


def run_cleanup(
    mode: CleanupMode | str = CleanupMode.SAFE,
    *,
    dry_run: bool = False,
    top_n: int = _DEFAULT_SAFE_TOP_N,
    min_bytes: int = _MIN_TRIM_BYTES,
    whitelist: frozenset[str] | None = None,
    extra_whitelist: list[str] | None = None,
    nuclear_options: NuclearOptions | None = None,
) -> CleanupReport:
    """Run one cleanup pass at the requested tier.

    Args:
        mode: which tier to run. ``DRYRUN`` is an alias for SAFE +
            ``dry_run=True`` — the kernel is never touched. Other tiers
            ignore the alias and respect the explicit ``dry_run`` flag.
        dry_run: if True, stages are described in the report but not
            executed. Honoured by every tier.
        top_n: SAFE tier — trim at most this many heaviest processes.
            NUCLEAR ignores ``top_n`` and trims every non-whitelisted
            process so caller can't accidentally narrow the sweep.
        min_bytes: SAFE tier — only consider processes whose working set
            exceeds this floor. NUCLEAR uses 1 MB instead so even small
            tray apps get trimmed (every MB counts when the user's goal
            is reboot-equivalence).
        whitelist: replace the default whitelist entirely (advanced).
        extra_whitelist: append to the default whitelist (typical user
            knob — e.g. add their game / IDE here). Honoured by NUCLEAR
            too: user-defined preserves like ``ollama.exe`` are not
            trimmed even at the most aggressive tier.
        nuclear_options: opt-in/opt-out for the 6 NUCLEAR sub-stages.
            Ignored unless ``mode is CleanupMode.NUCLEAR``. Defaults to
            a sensible "Pareto top 4 on, two riskiest off" profile when
            None.

    Returns:
        :class:`CleanupReport` with ``before`` / ``after`` snapshots and
        a per-stage breakdown. Always returns; never raises.
    """
    if isinstance(mode, str):
        try:
            mode = CleanupMode(mode)
        except ValueError as exc:
            raise ValueError(
                f"unknown CleanupMode {mode!r}; expected one of "
                f"{[m.value for m in CleanupMode]}"
            ) from exc

    effective_dry_run = dry_run or mode is CleanupMode.DRYRUN
    effective_mode = CleanupMode.SAFE if mode is CleanupMode.DRYRUN else mode

    # NUCLEAR contract: foreground exclusion + IDE-friendly whitelist
    # are deliberately bypassed. The caller (Memoria) only routes to
    # NUCLEAR when the user explicitly chose "as clean as a reboot,
    # I accept the brief stutter" — sparing chrome/dwm/explorer here
    # would defeat that contract. We still honour ``extra_whitelist``
    # so a user-defined preserve like ``ollama.exe`` is respected; the
    # rule is "trim every process Windows can rebuild, preserve only
    # what the kernel itself depends on or what the user opts in".
    if effective_mode is CleanupMode.NUCLEAR:
        wl = whitelist or _NUCLEAR_MIN_WHITELIST
        nuke = nuclear_options or NuclearOptions()
        # NUCLEAR overrides top_n / min_bytes — the user signed up for
        # a deep clean; respecting a tight top_n would silently neuter
        # the tier.
        top_n = max(top_n, 500)
        min_bytes = min(min_bytes, 1 * 1024 * 1024)
    else:
        wl = whitelist or _DEFAULT_WHITELIST
        nuke = None
    if extra_whitelist:
        wl = wl | frozenset(name.lower() for name in extra_whitelist)

    started_at = time.time()
    report = CleanupReport(
        mode=mode,
        dry_run=effective_dry_run,
        is_admin=core.is_admin(),
        started_at=started_at,
        finished_at=started_at,  # filled in below
    )

    # Snapshot before — always.
    try:
        snap_before = core.get_memory_snapshot()
        report.before = snap_before.as_dict()
    except OSError as exc:
        report.notes.append(f"snapshot before failed: {exc}")
        snap_before = None

    # Stage 1: per-process trim (every tier).
    stage_pp, trims = _run_per_process_trim(
        dry_run=effective_dry_run,
        top_n=top_n, min_bytes=min_bytes, whitelist=wl,
    )
    report.stages.append(stage_pp)
    report.process_trims = trims

    # Stage 2: low-priority standby purge (STANDBY+).
    if effective_mode in (
        CleanupMode.STANDBY, CleanupMode.AGGRESSIVE, CleanupMode.DESTRUCTIVE,
    ):
        skip = "" if report.is_admin else "needs_admin"
        report.stages.append(
            _run_stage(
                "purge_low_priority_standby_list",
                core.purge_low_priority_standby_list,
                dry_run=effective_dry_run,
                skip_reason=skip,
            )
        )

    # Stage 3: full standby + file cache shrink (AGGRESSIVE+).
    if effective_mode in (CleanupMode.AGGRESSIVE, CleanupMode.DESTRUCTIVE):
        skip = "" if report.is_admin else "needs_admin"
        report.stages.append(
            _run_stage(
                "purge_standby_list",
                core.purge_standby_list,
                dry_run=effective_dry_run,
                skip_reason=skip,
            )
        )
        report.stages.append(
            _run_stage(
                "set_system_file_cache_minimal",
                core.set_system_file_cache_minimal,
                dry_run=effective_dry_run,
                skip_reason=skip,
            )
        )

    # Stage 4: modified-page-list flush (DESTRUCTIVE only — write IO).
    if effective_mode is CleanupMode.DESTRUCTIVE:
        skip = "" if report.is_admin else "needs_admin"
        report.stages.append(
            _run_stage(
                "purge_modified_page_list",
                core.purge_modified_page_list,
                dry_run=effective_dry_run,
                skip_reason=skip,
            )
        )

    # ── NUCLEAR pipeline ────────────────────────────────────────────
    #
    # Order matters: pre-flight diagnostic first (no flush will help if
    # it's a driver leak); modified-flush before standby-purge (otherwise
    # dirty pages are stranded); MmEmptyAllWorkingSets after per-process
    # iter (catches system processes user-mode can't OpenProcess);
    # combined-list flush after standby (defrags the now-clean cache);
    # service cycles last (UI flash is the most user-visible side
    # effect — best done when other reclaim is already booked).
    if effective_mode is CleanupMode.NUCLEAR:
        opts = nuke or NuclearOptions()
        admin_skip = "" if report.is_admin else "needs_admin"

        # N2 — pre-flight pool-tag leak diagnostic (read-only).
        if opts.diagnose_pool_leaks:
            _run_pool_leak_diagnostic(report, dry_run=effective_dry_run)

        # N5 — modified-page flush (write IO; before standby purge).
        report.stages.append(_run_stage(
            "purge_modified_page_list",
            core.purge_modified_page_list,
            dry_run=effective_dry_run,
            skip_reason=admin_skip,
        ))

        # N6 — full standby list purge (incl. high priority).
        report.stages.append(_run_stage(
            "purge_standby_list",
            core.purge_standby_list,
            dry_run=effective_dry_run,
            skip_reason=admin_skip,
        ))

        # N4 — kernel-side MmEmptyAllWorkingSets (catches system processes).
        report.stages.append(_run_stage(
            "empty_all_working_sets_via_nt",
            core.empty_all_working_sets_via_nt,
            dry_run=effective_dry_run,
            skip_reason=admin_skip,
        ))

        # N8 — system file cache shrink to minimum.
        report.stages.append(_run_stage(
            "set_system_file_cache_minimal",
            core.set_system_file_cache_minimal,
            dry_run=effective_dry_run,
            skip_reason=admin_skip,
        ))

        # N7 — page-combining flush (feature-detected; class 0x82).
        if opts.flush_combined_memory_list:
            report.stages.append(_run_stage(
                "purge_combined_memory_list",
                core.purge_combined_memory_list,
                dry_run=effective_dry_run,
                skip_reason=admin_skip,
            ))

        # N9 — DLL/font-cache service cycle (4 services, each its own stage).
        if opts.cycle_services:
            for svc in core.SERVICE_CYCLE_SAFELIST:
                report.stages.append(_run_tuple_stage(
                    f"cycle_service:{svc}",
                    (lambda n=svc: core.cycle_service(n)),
                    dry_run=effective_dry_run,
                    skip_reason=admin_skip,
                ))

        # N10 — SuperFetch / SysMain cycle.
        if opts.cycle_superfetch:
            report.stages.append(_run_tuple_stage(
                "cycle_superfetch",
                core.cycle_superfetch,
                dry_run=effective_dry_run,
                skip_reason=admin_skip,
            ))

        # WSL2 vmmem release (opt-in; interrupts running dev containers).
        if opts.shutdown_wsl:
            report.stages.append(_run_tuple_stage(
                "shutdown_wsl",
                core.shutdown_wsl,
                dry_run=effective_dry_run,
            ))

    # Snapshot after.
    try:
        snap_after = core.get_memory_snapshot()
        report.after = snap_after.as_dict()
        if snap_before is not None:
            report.reclaimed_bytes = max(
                0, snap_before.used_bytes - snap_after.used_bytes,
            )
    except OSError as exc:
        report.notes.append(f"snapshot after failed: {exc}")

    if effective_dry_run:
        report.notes.append(
            "dry_run: no kernel state was modified; per-process trims "
            "are predictions, not measurements."
        )
    if not report.is_admin and effective_mode is not CleanupMode.SAFE:
        report.notes.append(
            "not running as admin: STANDBY/AGGRESSIVE/DESTRUCTIVE stages "
            "were skipped. Re-run from an elevated shell to enable them."
        )

    report.finished_at = time.time()
    return report
