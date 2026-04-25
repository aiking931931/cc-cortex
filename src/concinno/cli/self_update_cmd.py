"""concinno.cli.self_update_cmd -- ``concinno self-update`` subcommand.

Wires :mod:`concinno.auto_update.tier2_self_update` into the CLI. The
heavy lifting (PyPI fetch / detached spawn / in-use detection) lives in
the engine module; this file only handles argparse + result rendering.

@module concinno.cli.self_update_cmd
@responsibility Translate CLI flags into ``self_update()`` kwargs, print
    a human-readable report, and exit with an appropriate status code.
@dependencies stdlib only (argparse, sys)
@exports cmd_self_update, register
"""

from __future__ import annotations

import argparse
import sys


def cmd_self_update(args: argparse.Namespace) -> int:
    """Handler for ``concinno self-update``.

    Returns the desired exit code. ``main()`` does not currently honour
    the return value of subcommand functions for legacy commands, but
    callers (including the test suite) can call this directly.
    """
    from concinno.auto_update.tier2_self_update import self_update

    result = self_update(
        target_version=args.version,
        pre_release=args.pre,
        skip_in_use_check=args.skip_in_use_check,
        dry_run=args.dry_run,
    )

    print(f"Current: {result.current_version}")
    if result.latest_version is not None:
        print(f"Latest:  {result.latest_version}")
    else:
        print("Latest:  (unknown — PyPI fetch failed)")
    if result.target_version:
        print(f"Target:  {result.target_version}")
    print(f"Method:  {result.method}")

    if result.in_use_warning:
        print("In-use evidence:")
        for line in result.in_use_warning:
            print(f"  - {line}")

    if result.error:
        print(f"\nError: {result.error}", file=sys.stderr)
        return 1

    if result.dry_run:
        print("\n(dry-run — no helper spawned)")
        return 0

    if result.current_version == (result.target_version or result.latest_version):
        print("\n✓ Already on target version. Nothing to do.")
        return 0

    if result.helper_spawned:
        print(
            f"\n✓ Update started in background (PID {result.helper_pid}). "
            f"Restart Claude Code to use new version."
        )
        if result.log_path:
            print(f"  Helper log: {result.log_path}")
        return 0

    # Reached only if neither error nor spawn (defensive fall-through).
    print("\nNo action taken.")
    return 0


def _cmd_self_update_main(args: argparse.Namespace) -> None:
    """argparse-friendly wrapper: convert int return to SystemExit."""
    rc = cmd_self_update(args)
    if rc != 0:
        sys.exit(rc)


def register(subparsers: argparse._SubParsersAction) -> None:
    """Attach the ``self-update`` subcommand to the CLI parser."""
    p = subparsers.add_parser(
        "self-update",
        help="Upgrade concinno via detached helper (Tier 2 auto-update)",
    )
    p.add_argument(
        "--version",
        default=None,
        help="Target version (default: latest stable from PyPI)",
    )
    p.add_argument(
        "--pre",
        action="store_true",
        help="Allow alpha/beta/rc/dev pre-releases when picking latest",
    )
    p.add_argument(
        "--skip-in-use-check",
        dest="skip_in_use_check",
        action="store_true",
        help="Skip 'is concinno in use' probe (Windows only blocks otherwise)",
    )
    p.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Inspect everything but do not spawn the pip helper",
    )
    p.set_defaults(func=_cmd_self_update_main)


__all__ = ["cmd_self_update", "register"]
