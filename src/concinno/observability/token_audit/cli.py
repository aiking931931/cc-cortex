"""concinno.observability.token_audit.cli — `concinno token-audit` CLI.

Two subcommands:

* ``concinno token-audit summary`` — print the latest session's
  summary (or ``--session=<id>`` for a specific session) by reading
  the most recent jsonl record under ``~/.concinno/audit/``.
* ``concinno token-audit advisor`` — list current archive
  recommendations. ``--accept <skill>`` / ``--reject <skill>``
  feed the FTRL learner.

The CLI is registered into the main ``concinno`` argparse tree by
:func:`register` — the consumer in ``concinno/cli/main.py`` calls
``register(sub)`` once at startup.

Output formats:

* ``--format=text`` (default) — human-readable summary.
* ``--format=json`` — machine-readable, suitable for piping into
  ``jq`` / ``python -m json.tool``.

Disabled mode: if the ``token_audit_autopilot`` feature is off, the
``summary`` subcommand still works because the audit jsonl is the
last persisted snapshot — the operator can always inspect history.
The ``advisor`` subcommand prints a one-line note that the feature
is off and exits 0; we deliberately do not raise so a user piping
the CLI into a pre-commit / CI script does not break.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from concinno.observability.token_audit.archive_advisor import (
    ArchiveAdvisor,
)
from concinno.observability.token_audit.audit import (
    TokenAuditSummary,
)
from concinno.observability.token_audit.overhead_tracker import (
    breakdown_from_summary,
)


def _audit_dir() -> Path:
    return Path.home() / ".concinno" / "audit"


def _read_latest_jsonl(session: str | None = None) -> dict[str, Any] | None:
    """Return the most-recent record from the audit jsonl directory.

    If ``session`` is given, read that specific session file's last
    line; otherwise scan all jsonl files and return the line with the
    largest ``ended_at``.
    """
    base = _audit_dir()
    if not base.exists():
        return None
    if session:
        path = base / f"token_audit_{session}.jsonl"
        if not path.exists():
            return None
        return _read_last_line(path)
    best: dict[str, Any] | None = None
    best_ended = -1.0
    for path in base.glob("token_audit_*.jsonl"):
        rec = _read_last_line(path)
        if rec is None:
            continue
        ended = float(rec.get("ended_at") or 0.0)
        if ended > best_ended:
            best_ended = ended
            best = rec
    return best


def _read_last_line(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.strip():
        return None
    last_line = text.strip().splitlines()[-1]
    try:
        rec = json.loads(last_line)
    except json.JSONDecodeError:
        return None
    if not isinstance(rec, dict):
        return None
    return rec


# ─── Summary subcommand ────────────────────────────────────────────


def cmd_summary(args: argparse.Namespace) -> int:
    """``concinno token-audit summary`` — print the latest session summary."""
    rec = _read_latest_jsonl(getattr(args, "session", None))
    if rec is None:
        msg = "no token-audit records found under ~/.concinno/audit/"
        if args.format == "json":
            sys.stdout.write(json.dumps({"error": msg}) + "\n")
        else:
            sys.stderr.write(msg + "\n")
        return 0
    if args.format == "json":
        sys.stdout.write(json.dumps(rec, ensure_ascii=False, indent=2))
        sys.stdout.write("\n")
        return 0
    # Text mode — reconstruct a summary + breakdown.
    summary = _summary_from_dict(rec)
    breakdown = breakdown_from_summary(summary)
    sys.stdout.write(
        f"Token audit (session={summary.session_id})\n"
        f"  enabled              : {summary.enabled}\n"
        f"  total estimated      : {summary.total_estimated_tokens} tok\n"
        f"  system prompt floor  : {summary.system_prompt_floor} tok\n"
        f"  skills loaded        : {summary.skills_loaded} "
        f"({summary.skills_total_overhead} tok)\n"
        f"  mcp tools loaded     : {summary.mcp_tools_loaded} "
        f"({summary.mcp_total_overhead} tok)\n"
        f"  sub-agents spawned   : {summary.subagents_spawned} "
        f"({summary.subagents_total_overhead} tok)\n"
        f"  tail buffer          : {summary.tail_buffer} tok\n"
    )
    if breakdown.skills_top:
        sys.stdout.write("\nTop skills (by overhead):\n")
        for name, tok in breakdown.skills_top.items():
            sys.stdout.write(f"  - {name:<32s} {tok} tok\n")
    if breakdown.mcp_top:
        sys.stdout.write("\nTop MCP tools (by overhead):\n")
        for name, tok in breakdown.mcp_top.items():
            sys.stdout.write(f"  - {name:<32s} {tok} tok\n")
    return 0


def _summary_from_dict(rec: dict[str, Any]) -> TokenAuditSummary:
    """Rebuild a ``TokenAuditSummary`` dataclass from a jsonl dict.

    Defaults fill any missing fields so tests that fixture an older
    schema don't crash this code path.
    """
    return TokenAuditSummary(
        session_id=str(rec.get("session_id", "")),
        enabled=bool(rec.get("enabled", True)),
        started_at=float(rec.get("started_at") or 0.0),
        ended_at=(
            float(rec["ended_at"]) if rec.get("ended_at") is not None else None
        ),
        skills_loaded=int(rec.get("skills_loaded", 0)),
        skills_total_overhead=int(rec.get("skills_total_overhead", 0)),
        skills_breakdown=dict(rec.get("skills_breakdown") or {}),
        subagents_spawned=int(rec.get("subagents_spawned", 0)),
        subagents_total_overhead=int(rec.get("subagents_total_overhead", 0)),
        mcp_tools_loaded=int(rec.get("mcp_tools_loaded", 0)),
        mcp_total_overhead=int(rec.get("mcp_total_overhead", 0)),
        mcp_breakdown=dict(rec.get("mcp_breakdown") or {}),
        system_prompt_floor=int(rec.get("system_prompt_floor", 0)),
        tail_buffer=int(rec.get("tail_buffer", 0)),
        total_estimated_tokens=int(rec.get("total_estimated_tokens", 0)),
        tool_search_enabled=bool(rec.get("tool_search_enabled", False)),
    )


# ─── Advisor subcommand ────────────────────────────────────────────


def cmd_advisor(args: argparse.Namespace) -> int:
    """``concinno token-audit advisor`` — list / accept / reject."""
    advisor = ArchiveAdvisor(
        days_threshold=int(getattr(args, "days", 30) or 30),
    )
    if getattr(args, "accept", None):
        advisor.accept(args.accept)
        if args.format == "json":
            sys.stdout.write(
                json.dumps({"action": "accept", "skill": args.accept}) + "\n",
            )
        else:
            sys.stdout.write(
                f"recorded accept for skill '{args.accept}'\n",
            )
        return 0
    if getattr(args, "reject", None):
        advisor.reject(args.reject)
        if args.format == "json":
            sys.stdout.write(
                json.dumps({"action": "reject", "skill": args.reject}) + "\n",
            )
        else:
            sys.stdout.write(
                f"recorded reject for skill '{args.reject}'\n",
            )
        return 0

    candidates = advisor.suggest_archives(retention_days=args.days)
    if args.format == "json":
        out = [c.to_dict() for c in candidates]
        sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2))
        sys.stdout.write("\n")
        return 0
    if not candidates:
        sys.stdout.write("no archive candidates found\n")
        return 0
    sys.stdout.write(
        f"{len(candidates)} archive candidate(s) "
        f"(threshold={args.days} days):\n",
    )
    for c in candidates:
        sys.stdout.write(
            f"  - {c.skill:<32s} score={c.score:.2f}  "
            f"unused {c.days_unused:.1f} days  "
            f"last_used={c.last_used or 'unknown'}\n",
        )
    sys.stdout.write(
        "\nTo act:  concinno token-audit advisor --accept <skill>\n"
        "         concinno token-audit advisor --reject <skill>\n",
    )
    return 0


# ─── Module-level CLI registration ─────────────────────────────────


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register the ``token-audit`` subcommand tree on ``concinno``.

    Call once from ``concinno/cli/main.py`` after the other
    ``_register_*`` helpers — the integration is one line, mirroring
    every other CLI sub-tree.
    """
    p = subparsers.add_parser(
        "token-audit",
        help="Per-session token overhead audit + ZIQ FTRL archive advisor",
    )
    p.add_argument(
        "--format", choices=("text", "json"), default="text",
        help="Output format (default: text)",
    )
    audit_sub = p.add_subparsers(dest="token_audit_command")

    p_sum = audit_sub.add_parser(
        "summary",
        help="Print the latest session's audit summary",
    )
    p_sum.add_argument(
        "--session", default=None,
        help="Specific session id (default: latest)",
    )
    p_sum.add_argument(
        "--format", choices=("text", "json"), default="text",
        help="Output format (default: text)",
    )
    p_sum.set_defaults(func=lambda a: sys.exit(cmd_summary(a)))

    p_adv = audit_sub.add_parser(
        "advisor",
        help="List / accept / reject stale-skill archive recommendations",
    )
    p_adv.add_argument(
        "--days", type=int, default=30,
        help="Days-unused threshold (default: 30)",
    )
    p_adv.add_argument(
        "--accept", default=None,
        help="Accept (move to archive) the named skill",
    )
    p_adv.add_argument(
        "--reject", default=None,
        help="Reject the recommendation for the named skill",
    )
    p_adv.add_argument(
        "--format", choices=("text", "json"), default="text",
        help="Output format (default: text)",
    )
    p_adv.set_defaults(func=lambda a: sys.exit(cmd_advisor(a)))

    def _default(_a: argparse.Namespace) -> None:
        p.print_help()

    p.set_defaults(func=_default)


__all__ = [
    "cmd_advisor",
    "cmd_summary",
    "register",
]
