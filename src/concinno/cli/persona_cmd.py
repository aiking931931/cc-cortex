"""``concinno persona {run,pin,pinned,recall}`` subcommands.

Wires the :mod:`concinno.persona` module into the Concinno CLI.
Follows the ``register(subparsers)`` pattern used by other namespace
subcommands (``skills_cmd``, ``preset_cmd``, ``new_feature_cmd``).

Subcommands:

* ``run`` — one-shot chat against a persona file. ``--provider echo``
  (default) makes it usable without LLM credentials, suitable for
  smoke-testing the loader / state pipeline.
* ``pin`` — append a pinned memory record to a state log.
* ``pinned`` — list all pinned memories in a state log.
* ``recall`` — query a state log for relevant past content.
"""

from __future__ import annotations

import argparse
import sys


def _cmd_run(args: argparse.Namespace) -> None:
    from concinno.persona.cli import run_chat

    reply = run_chat(
        persona_path=args.persona,
        state_path=args.state or "",
        provider=args.provider,
        model=args.model or "",
        message=args.message or "",
    )
    if reply:
        print(reply)


def _cmd_pin(args: argparse.Namespace) -> None:
    from concinno.persona.cli import pin_memory

    rc = pin_memory(args.state, args.content, reason=args.reason or "")
    if rc != 0:
        print("error: --content is required", file=sys.stderr)
        raise SystemExit(rc)
    print(f"OK: pinned to {args.state}")


def _cmd_pinned(args: argparse.Namespace) -> None:
    from concinno.persona.cli import format_pinned_text, list_pinned, to_json

    rows = list_pinned(args.state)
    if args.format == "json":
        print(to_json(rows))
    else:
        print(format_pinned_text(rows))


def _cmd_recall(args: argparse.Namespace) -> None:
    from concinno.persona.cli import format_recall_text, recall_memory, to_json

    rows = recall_memory(args.state, args.query, top_k=args.top_k)
    if args.format == "json":
        print(to_json(rows))
    else:
        print(format_recall_text(rows))


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``concinno persona`` namespace."""
    p = subparsers.add_parser(
        "persona",
        help="Run / pin / recall against a Concinno persona file",
    )
    sub = p.add_subparsers(dest="persona_action")

    p_run = sub.add_parser("run", help="One-shot chat against a persona file")
    p_run.add_argument("--persona", required=True, help="Path to persona MD file")
    p_run.add_argument("--state", default="", help="Path to state JSONL log (optional)")
    p_run.add_argument(
        "--provider",
        default="echo",
        help="LLM provider (echo|anthropic|openai|ollama). Default echo for offline use.",
    )
    p_run.add_argument("--model", default="", help="Model id (default: provider-specific)")
    p_run.add_argument(
        "--message",
        default="",
        help="User message (omit for an empty smoke run)",
    )
    p_run.set_defaults(func=_cmd_run)

    p_pin = sub.add_parser("pin", help="Pin a memory to a state log")
    p_pin.add_argument("--state", required=True, help="Path to state JSONL log")
    p_pin.add_argument("--content", required=True, help="Memory content")
    p_pin.add_argument("--reason", default="", help="Optional reason for the pin")
    p_pin.set_defaults(func=_cmd_pin)

    p_list = sub.add_parser("pinned", help="List pinned memories in a state log")
    p_list.add_argument("--state", required=True, help="Path to state JSONL log")
    p_list.add_argument("--format", choices=("text", "json"), default="text")
    p_list.set_defaults(func=_cmd_pinned)

    p_recall = sub.add_parser("recall", help="Recall relevant content from a state log")
    p_recall.add_argument("--state", required=True, help="Path to state JSONL log")
    p_recall.add_argument("--query", required=True, help="Query text")
    p_recall.add_argument("--top-k", dest="top_k", type=int, default=3)
    p_recall.add_argument("--format", choices=("text", "json"), default="text")
    p_recall.set_defaults(func=_cmd_recall)

    def _default(_a: argparse.Namespace) -> None:
        p.print_help()

    p.set_defaults(func=_default)


__all__ = ["register"]
