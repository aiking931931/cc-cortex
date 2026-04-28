"""``concinno approval-mode {get, set, status}`` CLI.

Surfaces :mod:`concinno.approval_mode` to operators so the
``manual / smart / off`` switch can be toggled without hand-editing
``~/.concinno/approval_mode.json``.

Subcommands:

    get             — print the active mode (one word for piping).
    set <mode>      — switch to ``manual`` / ``smart`` / ``off`` and persist.
    status          — multi-line summary including FTRL state.
"""

from __future__ import annotations

import argparse
import sys

from concinno.approval_mode import (
    ApprovalMode,
    describe_current_config,
    load_config,
    save_config,
)

__all__ = ["register"]


def _cmd_get(_args: argparse.Namespace) -> int:
    print(load_config().mode.value)
    return 0


def _cmd_set(args: argparse.Namespace) -> int:
    raw = (args.mode or "").strip().lower()
    if raw not in {m.value for m in ApprovalMode}:
        valid = ", ".join(m.value for m in ApprovalMode)
        print(f"unknown mode {args.mode!r}; choose one of: {valid}", file=sys.stderr)
        return 2
    cfg = load_config()
    new_cfg = type(cfg)(
        mode=ApprovalMode(raw),
        ftrl=cfg.ftrl,
        source=cfg.source,
        warnings=cfg.warnings,
    )
    save_config(new_cfg)
    print(f"approval_mode set to {raw}")
    return 0


def _cmd_status(_args: argparse.Namespace) -> int:
    print(describe_current_config())
    return 0


def _dispatch(args: argparse.Namespace) -> None:
    action = args.approval_action
    if action == "get":
        sys.exit(_cmd_get(args))
    if action == "set":
        sys.exit(_cmd_set(args))
    if action == "status":
        sys.exit(_cmd_status(args))
    print("usage: concinno approval-mode {get|set|status}", file=sys.stderr)
    sys.exit(2)


def register(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    """Register the ``concinno approval-mode ...`` namespace."""
    p = subparsers.add_parser(
        "approval-mode",
        help=(
            "Get / set / inspect the AskUser approval routing mode "
            "(manual / smart / off). Layered ABOVE destruction_guard "
            "(R0-R4 still enforced) and release_authorization."
        ),
    )
    sub = p.add_subparsers(dest="approval_action")

    p_get = sub.add_parser("get", help="Print the active mode (one word)")
    p_get.set_defaults(func=_dispatch)

    p_set = sub.add_parser("set", help="Set the approval mode and persist")
    p_set.add_argument(
        "mode",
        choices=[m.value for m in ApprovalMode],
        help="One of manual / smart / off",
    )
    p_set.set_defaults(func=_dispatch)

    p_status = sub.add_parser(
        "status",
        help="Multi-line summary including FTRL posterior state",
    )
    p_status.set_defaults(func=_dispatch)
