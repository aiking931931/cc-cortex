"""concinno.cli.l2_index_cmd — ``concinno l2-index`` subcommand.

@module l2_index_cmd
@responsibility Wrap :mod:`concinno.l2_index` (build + query) as a
    user-facing CLI subcommand following the
    ``session_switches_cmd`` / ``preset_cmd`` argparse pattern.
@dependencies concinno.l2_index
@exports register, cmd_l2_index_build, cmd_l2_index_query

Sub-agent K wave-2 (4.4.0). Plan v1 line 64.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def cmd_l2_index_build(args: argparse.Namespace) -> None:
    """Walk skill roots, build reverse index, persist to ``_triggers.json``."""
    from concinno.l2_index import (
        build_reverse_index,
        default_skill_roots,
        walk_skills,
        write_triggers_json,
    )

    roots = (
        [Path(p) for p in args.root]
        if getattr(args, "root", None)
        else default_skill_roots()
    )
    entries = walk_skills(roots)
    rev = build_reverse_index(entries)
    out_path = Path(args.out) if getattr(args, "out", None) else None
    target = write_triggers_json(rev, path=out_path, skills_scanned=len(entries))
    valid = sum(1 for e in entries if e.is_valid)
    print(
        f"l2-index build: scanned={len(entries)} valid={valid} "
        f"triggers={len(rev)} → {target}",
        file=sys.stderr,
    )


def cmd_l2_index_query(args: argparse.Namespace) -> None:
    """Look up ``args.keyword`` in the persisted reverse index."""
    from concinno.l2_index import query_trigger

    in_path = Path(args.in_path) if getattr(args, "in_path", None) else None
    hits = query_trigger(args.keyword, path=in_path)
    if not hits:
        print(
            f"l2-index query: no skills for {args.keyword!r}",
            file=sys.stderr,
        )
        sys.exit(1)
    for name in hits:
        print(name)


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register ``l2-index`` + its subcommands on the argparse tree."""
    p = subparsers.add_parser(
        "l2-index",
        help="L2 SKILL.md frontmatter walker + reverse trigger index",
    )
    sub = p.add_subparsers(dest="l2_index_command")

    p_build = sub.add_parser(
        "build",
        help="Walk skills, build reverse index, write _triggers.json",
    )
    p_build.add_argument(
        "--root",
        action="append",
        default=None,
        help="Override skill root (repeatable). Default = user + project.",
    )
    p_build.add_argument(
        "--out",
        default=None,
        help="Override output path (default = _AI_BRAIN/_triggers.json).",
    )
    p_build.set_defaults(func=cmd_l2_index_build)

    p_query = sub.add_parser(
        "query",
        help="Look up a trigger keyword in the persisted index",
    )
    p_query.add_argument("keyword", help="Trigger keyword to look up.")
    p_query.add_argument(
        "--in",
        dest="in_path",
        default=None,
        help="Override input path.",
    )
    p_query.set_defaults(func=cmd_l2_index_query)
