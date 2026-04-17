"""CLI entry point for process guard.

@module process_guard.cli
@responsibility Parse args, run guard, print results
"""

from __future__ import annotations

import logging

from ._base import IDLE_MINUTES, MEMORY_CRITICAL_PERCENT, STALE_MINUTES
from .guard import run_guard


def main() -> None:
    """CLI: python -m concinno.process_guard [--dry-run] [--verbose]"""
    import argparse

    parser = argparse.ArgumentParser(description="Claude Code Process Guard")
    parser.add_argument("--dry-run", action="store_true", help="Log but don't kill")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--lock-path", help="Path to instance_lock.json")
    parser.add_argument("--idle-minutes", type=int, default=IDLE_MINUTES)
    parser.add_argument("--stale-minutes", type=int, default=STALE_MINUTES)
    parser.add_argument(
        "--memory-critical-percent",
        type=float,
        default=MEMORY_CRITICAL_PERCENT,
        help=f"System RAM %% threshold for emergency relief (default {MEMORY_CRITICAL_PERCENT})",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    result = run_guard(
        lock_path=args.lock_path,
        idle_minutes=args.idle_minutes,
        stale_minutes=args.stale_minutes,
        memory_critical_percent=args.memory_critical_percent,
        dry_run=args.dry_run,
    )

    for action in result.actions:
        print(action)
    for warning in result.warnings:
        print(f"WARN: {warning}")

    print(
        f"{'DRY-RUN' if args.dry_run else 'DONE'}: "
        f"scanned={result.scanned}, killed={result.killed}, "
        f"freed={result.freed_mb}MB, lock_cleaned={result.lock_cleaned}"
    )
