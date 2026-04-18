"""concinno.cli.config_cmd — ``concinno config`` subcommands.

Thin argparse shim around :mod:`concinno.config`. Five verbs:

    concinno config                   # show merged config + per-key source
    concinno config get <key>         # print one value
    concinno config set <key> <val>   # write to user layer
    concinno config set --project ... # write to project layer instead
    concinno config unset <key>       # remove from user layer
    concinno config path              # print config file paths

Exit codes:
    0 on success, 1 on user error (invalid key/value, unknown subcommand).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .. import config as cfg


def _fmt_value(value: object) -> str:
    """Render a config value for CLI display. Booleans lowercase."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _coerce_cli_value(key: str, raw: str) -> object:
    """Parse a CLI string into the expected type for ``key``.

    Booleans accept ``true/false/1/0/yes/no/on/off``. Everything else is
    returned as-is and left to :func:`concinno.config.validate` to reject.
    """
    if key in cfg._BOOL_KEYS:
        low = raw.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
        raise ValueError(
            f"Invalid boolean for {key}: {raw!r}. Use true/false/1/0.",
        )
    return raw


def cmd_config_show(_args: argparse.Namespace) -> None:
    """Print merged config + where each value came from."""
    merged = cfg.load()
    source_map = cfg.sources()
    print("concinno config:")
    for key in sorted(merged):
        val = _fmt_value(merged[key])
        source = source_map.get(key, "default")
        print(f"  {key:22s} {val:12s}  ({source})")


def cmd_config_get(args: argparse.Namespace) -> None:
    """Print a single config value."""
    try:
        value = cfg.get(args.key)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    print(_fmt_value(value))


def cmd_config_set(args: argparse.Namespace) -> None:
    """Write a config value to user layer (default) or project layer."""
    try:
        value = _coerce_cli_value(args.key, args.value)
        if args.project:
            cfg.set_project(args.key, value)
            target = cfg.project_config_path()
        else:
            cfg.set_user(args.key, value)
            target = cfg.user_config_path()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    print(f"set {args.key}={_fmt_value(value)} in {target}")


def cmd_config_unset(args: argparse.Namespace) -> None:
    """Remove a key from the user layer."""
    try:
        removed = cfg.unset_user(args.key)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    if removed:
        print(f"unset {args.key} (user layer)")
    else:
        print(f"{args.key} not set in user layer; nothing to do")


def cmd_config_path(_args: argparse.Namespace) -> None:
    """Print layer paths so the user knows where to look."""
    print(f"user:    {cfg.user_config_path()}")
    print(f"project: {cfg.project_config_path()}")


def register(subparsers: argparse._SubParsersAction) -> None:
    """Attach the ``config`` subcommand tree to an existing argparse subparser.

    Called from :mod:`concinno.cli.main` during parser construction.
    """
    p_cfg = subparsers.add_parser(
        "config",
        help="View or edit concinno user settings (mode / locale / flags)",
    )
    cfg_sub = p_cfg.add_subparsers(dest="config_command")

    p_get = cfg_sub.add_parser("get", help="Print a single config value")
    p_get.add_argument("key", help="Config key (mode/locale/auto_compact/memory_file_enabled)")
    p_get.set_defaults(func=cmd_config_get)

    p_set = cfg_sub.add_parser("set", help="Write a config value")
    p_set.add_argument(
        "--project",
        action="store_true",
        help="Write to <cwd>/.concinno/config.json instead of ~/.concinno/config.json",
    )
    p_set.add_argument("key", help="Config key")
    p_set.add_argument("value", help="New value")
    p_set.set_defaults(func=cmd_config_set)

    p_unset = cfg_sub.add_parser("unset", help="Remove a key from the user config")
    p_unset.add_argument("key", help="Config key")
    p_unset.set_defaults(func=cmd_config_unset)

    p_path = cfg_sub.add_parser("path", help="Print config file paths")
    p_path.set_defaults(func=cmd_config_path)

    # Default when no sub-verb given: show merged state + sources.
    p_cfg.set_defaults(
        func=lambda a: cmd_config_show(a) if not a.config_command else None,
    )


__all__ = [
    "cmd_config_get",
    "cmd_config_path",
    "cmd_config_set",
    "cmd_config_show",
    "cmd_config_unset",
    "register",
]


# Silence "Path unused" when only re-exported as type hint — kept for future
# expansion; the module may grow to accept explicit ``cwd`` overrides.
_ = Path
