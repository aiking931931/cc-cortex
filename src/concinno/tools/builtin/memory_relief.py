"""concinno.tools.builtin.memory_relief — agent-callable RAM cleanup tool.

@module tools.builtin.memory_relief
@responsibility Expose :func:`concinno.memory_relief.engine.run_cleanup`
    as a standard ``Tool`` so a Claude Code agent can invoke it via
    ``ToolRegistry.search`` + ``get(name)`` when the harness reports
    high RAM pressure. The tool is registered as ``deferred`` (not core)
    because most sessions never need it — paying the import cost on
    every turn would punish the 99% that have RAM headroom.

@dependencies concinno.memory_relief.engine (which is stdlib-only).

JSON-output design: every field in :class:`CleanupReport` round-trips
through ``as_dict()`` so the agent receives a structured, schema-stable
payload rather than free text. This is the differentiator from Mem
Reduct / RAMMap (which only produce GUI output) — agents can read the
``reclaimed_mb`` / ``stages[].ok`` / ``process_trims[]`` fields and
decide whether to retry, escalate, or report success to the user.
"""

from __future__ import annotations

from typing import Any


class MemoryReliefTool:
    """``Tool`` Protocol implementation. See module docstring for
    surface-level rationale; the ``call()`` body is the entire contract."""

    name = "MemoryRelief"
    description = (
        "Windows RAM cleanup with before/after stats. Tiers: dryrun "
        "(preview only), safe (per-process EmptyWorkingSet, no admin), "
        "standby (adds priority-0 standby purge, needs admin), "
        "aggressive (full standby + file cache shrink, needs admin), "
        "destructive (adds modified-page-list flush, needs admin + "
        "causes write IO burst). Defaults to dryrun + top_n=8 heaviest "
        "non-whitelisted processes. Returns JSON: "
        "{mode, dry_run, before, after, reclaimed_mb, stages[], "
        "process_trims[]}. Skips kernel ops on non-Windows."
    )
    is_concurrency_safe = False  # mutates kernel memory lists

    def call(
        self,
        *,
        mode: str = "dryrun",
        dry_run: bool | None = None,
        top_n: int = 8,
        min_mb: int = 50,
        extra_whitelist: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run one cleanup pass and return the structured report.

        Args:
            mode: one of ``dryrun`` / ``safe`` / ``standby`` /
                ``aggressive`` / ``destructive``. ``dryrun`` is identical
                to ``safe`` but never touches kernel state.
            dry_run: explicit override. ``None`` defers to the implicit
                rule "mode='dryrun' implies True". Setting ``True`` on
                any other mode yields a preview of that tier.
            top_n: SAFE tier — number of heaviest processes to trim.
            min_mb: SAFE tier — minimum working-set size (MB) to consider.
            extra_whitelist: process names (lower-case, with ``.exe``)
                appended to the default never-trim list.

        Returns:
            Dict identical to :meth:`CleanupReport.as_dict`. Agents that
            need only the bottom line read ``reclaimed_mb`` and
            ``after['used_percent']``; agents that need to explain the
            outcome to a user read ``stages[]`` + ``process_trims[]``.
        """
        # Lazy-import: keeps the agent's prompt small until a session
        # actually calls this tool. Mirrors the registry's deferred
        # pattern (concinno.tools.registry).
        from concinno.memory_relief import engine

        effective_dry_run = (
            (mode == "dryrun") if dry_run is None else bool(dry_run)
        )
        report = engine.run_cleanup(
            mode=mode,
            dry_run=effective_dry_run,
            top_n=int(top_n),
            min_bytes=int(min_mb) * 1024 * 1024,
            extra_whitelist=extra_whitelist,
        )
        return report.as_dict()


__all__ = ["MemoryReliefTool"]
