"""concinno.cli.preset_cmd — ``concinno preset`` subcommand.

@module preset_cmd
@responsibility Expose the v4 cascade from :mod:`concinno.preset_cascade`
    as a user-facing CLI: ``preset show | set | list | autotune-log``.
@dependencies concinno.preset_cascade, concinno.preset_model (via cascade)
@exports register, cmd_preset_show, cmd_preset_set, cmd_preset_list,
    cmd_preset_autotune_log

Follows the established ``session_switches_cmd.py`` argparse pattern.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def cmd_preset_show(args: argparse.Namespace) -> None:
    """Print the active preset, effective values, and origin chain."""
    from concinno.preset_cascade import (
        get_active_preset,
        get_effective_value,
        load_presets_file,
    )

    fmt = getattr(args, "format", "text")
    active = get_active_preset()
    presets = load_presets_file()
    summary = presets.presets[active].summary if active in presets.presets else {}

    rows = []
    for key in sorted(summary.keys()):
        value, origin = get_effective_value(key)
        rows.append(
            {
                "key": key,
                "value": value,
                "origin": list(origin),
                "description": presets.presets[active].index.get(key, ""),
            }
        )

    if fmt == "json":
        payload = {
            "schema": "concinno.preset.v1",
            "active_preset": active,
            "effective_values": rows,
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    # text
    print(f"active preset: {active}")
    if not rows:
        print("  (empty summary)")
        return
    for r in rows:
        origin_str = "/".join(r["origin"])
        desc = f"  — {r['description']}" if r["description"] else ""
        print(f"  {r['key']:45s} = {r['value']!r:15s} [{origin_str}]{desc}")


def cmd_preset_set(args: argparse.Namespace) -> None:
    """Switch the active preset + cascade."""
    from concinno.preset_cascade import set_preset

    try:
        report = set_preset(args.name)
    except KeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)

    fmt = getattr(args, "format", "text")
    if fmt == "json":
        print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))
        return

    print(f"active preset now: {report.preset_name}")
    if report.fields_changed:
        print(f"  fields changed ({len(report.fields_changed)}):")
        for k, o, n in report.fields_changed:
            print(f"    {k}: {o!r} -> {n!r}")
    else:
        print("  (no library switches were updated — preset was no-op or consumer-only)")
    if report.consumers_called:
        print(f"  consumers called: {', '.join(report.consumers_called)}")
    if report.errors:
        print(f"  errors ({len(report.errors)}):")
        for err in report.errors:
            print(f"    - {err}")


def cmd_preset_list(args: argparse.Namespace) -> None:
    """List every preset discovered across cascade layers."""
    from concinno.preset_cascade import get_active_preset, list_presets

    fmt = getattr(args, "format", "text")
    names = list_presets()
    active = get_active_preset()
    if fmt == "json":
        print(
            json.dumps(
                {"active": active, "presets": names},
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    if not names:
        print("(no presets discovered)")
        return
    for name in names:
        marker = "*" if name == active else " "
        print(f" {marker} {name}")


def cmd_preset_autotune_log(args: argparse.Namespace) -> None:
    """Tail the ZIQ autotune JSONL log."""
    override = os.environ.get("CONCINNO_ZIQ_AUTOTUNE_LOG", "").strip()
    if override:
        path = Path(override).expanduser()
    else:
        path = Path.home() / ".concinno" / "ziq_autotune_log.jsonl"

    if not path.is_file():
        print(f"(no autotune log at {path})")
        return

    tail_n = int(getattr(args, "tail", 0) or 0)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        print(f"error: cannot read {path}: {exc}", file=sys.stderr)
        sys.exit(1)

    if tail_n > 0:
        lines = lines[-tail_n:]

    fmt = getattr(args, "format", "text")
    if fmt == "json":
        parsed: list[dict] = []
        for line in lines:
            try:
                parsed.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        print(json.dumps(parsed, indent=2, ensure_ascii=False))
        return

    for line in lines:
        print(line)


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register ``preset`` + its subcommands on the argparse tree."""
    p = subparsers.add_parser(
        "preset",
        help="Inspect + switch the active v4 cascade preset",
    )
    sub = p.add_subparsers(dest="preset_command")

    p_show = sub.add_parser(
        "show",
        help="Print active preset + effective values with origins",
    )
    p_show.add_argument(
        "--format", choices=["text", "json"], default="text",
        help="Output format (default: text)",
    )
    p_show.set_defaults(func=cmd_preset_show)

    p_set = sub.add_parser(
        "set",
        help="Switch active preset + cascade to consumers",
    )
    p_set.add_argument("name", help="Preset name (e.g. benchmark, general, prod)")
    p_set.add_argument(
        "--format", choices=["text", "json"], default="text",
        help="Output format (default: text)",
    )
    p_set.set_defaults(func=cmd_preset_set)

    p_list = sub.add_parser(
        "list",
        help="List presets discovered across cascade layers",
    )
    p_list.add_argument(
        "--format", choices=["text", "json"], default="text",
        help="Output format (default: text)",
    )
    p_list.set_defaults(func=cmd_preset_list)

    p_log = sub.add_parser(
        "autotune-log",
        help="Tail the ZIQ autotune write-back JSONL log",
    )
    p_log.add_argument(
        "--tail", type=int, default=0,
        help="Show last N lines (0 = all)",
    )
    p_log.add_argument(
        "--format", choices=["text", "json"], default="text",
        help="Output format (default: text)",
    )
    p_log.set_defaults(func=cmd_preset_autotune_log)

    # Default behavior when `concinno preset` is invoked without a sub.
    def _default(_a: argparse.Namespace) -> None:
        p.print_help()

    p.set_defaults(func=_default)


__all__ = [
    "cmd_preset_autotune_log",
    "cmd_preset_list",
    "cmd_preset_set",
    "cmd_preset_show",
    "register",
]
