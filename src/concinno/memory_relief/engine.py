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
    """The four escalating cleanup tiers; passed verbatim to the Tool /
    CLI / tray right-click menu so the surface shape is identical."""

    DRYRUN = "dryrun"
    SAFE = "safe"
    STANDBY = "standby"
    AGGRESSIVE = "aggressive"
    DESTRUCTIVE = "destructive"


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


# ── Main entry point ──────────────────────────────────────────────────


def run_cleanup(
    mode: CleanupMode | str = CleanupMode.SAFE,
    *,
    dry_run: bool = False,
    top_n: int = _DEFAULT_SAFE_TOP_N,
    min_bytes: int = _MIN_TRIM_BYTES,
    whitelist: frozenset[str] | None = None,
    extra_whitelist: list[str] | None = None,
) -> CleanupReport:
    """Run one cleanup pass at the requested tier.

    Args:
        mode: which tier to run. ``DRYRUN`` is an alias for SAFE +
            ``dry_run=True`` — the kernel is never touched. Other tiers
            ignore the alias and respect the explicit ``dry_run`` flag.
        dry_run: if True, stages are described in the report but not
            executed. Honoured by every tier.
        top_n: SAFE tier — trim at most this many heaviest processes.
        min_bytes: SAFE tier — only consider processes whose working set
            exceeds this floor.
        whitelist: replace the default whitelist entirely (advanced).
        extra_whitelist: append to the default whitelist (typical user
            knob — e.g. add their game / IDE here).

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

    wl = whitelist or _DEFAULT_WHITELIST
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
