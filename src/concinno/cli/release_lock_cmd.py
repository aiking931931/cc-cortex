"""concinno.cli.release_lock_cmd — atomic release lock CLI.

@module release_lock_cmd
@responsibility Drive :class:`concinno.coordination.release_lock.ReleaseLock`
    from the operator's shell so a release coordinator can:

      ``concinno release-lock acquire <pkg> <version>``
      ``concinno release-lock release <pkg>``
      ``concinno release-lock list``
      ``concinno release-lock check <pkg>``

    The lock state is the file-level handoff that prevents the PyPI 400
    already-exists race that hit Concinno 4.2.1 — markdown-section
    self-validation cannot survive concurrent reads, but
    ``msvcrt.locking`` / ``fcntl.flock`` on a sentinel file can.

@exports register, cmd_acquire, cmd_release, cmd_list, cmd_check
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path


def _resolve_session() -> str:
    """Best-effort session identity from env or instance_lock.json."""
    for var in ("CCC_SESSION", "CC_SESSION_ID", "CLAUDE_SESSION_ID"):
        val = os.environ.get(var, "").strip()
        if val:
            return val
    lock_path = Path.home() / ".claude" / "token_state" / "instance_lock.json"
    if lock_path.exists():
        try:
            data = json.loads(lock_path.read_text(encoding="utf-8"))
            sessions = data.get("sessions", {})
            if sessions:
                # Pick newest session by 'started' if available.
                def _start(item: tuple[str, dict]) -> str:
                    return str(item[1].get("started", ""))

                key, _ = max(sessions.items(), key=_start)
                return key
        except (json.JSONDecodeError, OSError):
            pass
    return f"unknown-{socket.gethostname()}"


def cmd_acquire(args: argparse.Namespace) -> None:
    from concinno.coordination.release_lock import ReleaseLock

    lock = ReleaseLock()
    session = args.session or _resolve_session()
    ok = lock.acquire(args.package, args.version, session=session)
    if ok:
        print(f"acquired: {args.package} {args.version} (session={session})")
        sys.exit(0)
    held = lock.check(args.package)
    holder = held.get("holder_session", "?") if held else "?"
    held_ver = held.get("version", "?") if held else "?"
    print(
        f"BLOCKED: {args.package} lock held by session {holder!r} "
        f"for version {held_ver!r}",
        file=sys.stderr,
    )
    sys.exit(1)


def cmd_release(args: argparse.Namespace) -> None:
    from concinno.coordination.release_lock import ReleaseLock

    ReleaseLock().release(args.package)
    print(f"released: {args.package}")


def cmd_list(_args: argparse.Namespace) -> None:
    from concinno.coordination.release_lock import ReleaseLock

    locks = ReleaseLock().list_active()
    if not locks:
        print("(no active release locks)")
        return
    for entry in locks:
        print(
            f"  {entry.get('pkg', '?'):20s} {entry.get('version', '?'):12s}  "
            f"holder={entry.get('holder_session', '?')}  "
            f"host={entry.get('host', '?')}  "
            f"acquired_at={entry.get('acquired_at', '?')}"
        )


def cmd_check(args: argparse.Namespace) -> None:
    from concinno.coordination.release_lock import ReleaseLock

    held = ReleaseLock().check(args.package)
    if held is None:
        print(f"free: {args.package}")
        sys.exit(0)
    print(json.dumps(held, indent=2, ensure_ascii=False))


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``release-lock`` subcommand tree."""
    p = subparsers.add_parser(
        "release-lock",
        help="Atomic per-package release lock (prevents PyPI 400 races)",
    )
    sub = p.add_subparsers(dest="release_lock_command")

    p_acq = sub.add_parser("acquire", help="Acquire the release lock")
    p_acq.add_argument("package", help="Package name (e.g. concinno)")
    p_acq.add_argument("version", help="Target version (e.g. 4.2.3)")
    p_acq.add_argument(
        "--session",
        default="",
        help="Session id (default: from CCC_SESSION env or instance_lock.json)",
    )
    p_acq.set_defaults(func=cmd_acquire)

    p_rel = sub.add_parser("release", help="Release the release lock")
    p_rel.add_argument("package", help="Package name")
    p_rel.set_defaults(func=cmd_release)

    p_ls = sub.add_parser("list", help="List active release locks")
    p_ls.set_defaults(func=cmd_list)

    p_chk = sub.add_parser("check", help="Show current lock content (or 'free')")
    p_chk.add_argument("package", help="Package name")
    p_chk.set_defaults(func=cmd_check)

    def _default(_a: argparse.Namespace) -> None:
        p.print_help()

    p.set_defaults(func=_default)


__all__: list[str] = [
    "cmd_acquire",
    "cmd_release",
    "cmd_list",
    "cmd_check",
    "register",
]
