"""Allow ``python -m concinno.memory_relief [mode] [--no-dry-run] [--top-n N]``.

@module memory_relief.__main__
@responsibility CLI veneer over :func:`concinno.memory_relief.engine.run_cleanup`.
    Prints the report as pretty JSON so a shell pipeline (or the
    ``/memrelief`` skill) can grep ``reclaimed_mb`` directly. Default
    ``mode='dryrun'`` so a no-arg invocation never touches kernel state.
"""

from __future__ import annotations

import argparse
import json
import sys

from .engine import CleanupMode, run_cleanup


def main() -> int:
    """Entry point. Returns the process exit code (0 always for success;
    1 if an explicit mode was requested but no stage succeeded)."""
    parser = argparse.ArgumentParser(
        prog="python -m concinno.memory_relief",
        description=(
            "Windows RAM cleanup. Default mode is 'dryrun' (preview "
            "only, no kernel state touched). Output is JSON on stdout."
        ),
    )
    parser.add_argument(
        "mode",
        nargs="?",
        default=CleanupMode.DRYRUN.value,
        choices=[m.value for m in CleanupMode] + ["status"],
        help=(
            "dryrun: preview only. safe: per-process trim, no admin. "
            "standby/aggressive/destructive: escalating tiers (admin). "
            "status: print snapshot only."
        ),
    )
    parser.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Override the implicit dry-run for the chosen mode.",
    )
    parser.add_argument("--top-n", type=int, default=8)
    parser.add_argument("--min-mb", type=int, default=50)
    parser.add_argument(
        "--extra-whitelist",
        action="append",
        default=None,
        help="Process name (e.g. 'firefox.exe') to skip. May repeat.",
    )
    args = parser.parse_args()

    if args.mode == "status":
        # Snapshot-only path — never touches kernel state, ignores other flags.
        from .core import get_memory_snapshot

        snapshot = get_memory_snapshot()
        json.dump(snapshot.as_dict(), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    report = run_cleanup(
        mode=args.mode,
        dry_run=not args.no_dry_run if args.mode != CleanupMode.DRYRUN.value else True,
        top_n=args.top_n,
        min_bytes=args.min_mb * 1024 * 1024,
        extra_whitelist=args.extra_whitelist,
    )
    json.dump(report.as_dict(), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if any(s["ok"] for s in report.as_dict()["stages"]) else 1


if __name__ == "__main__":
    sys.exit(main())
