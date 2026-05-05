"""Programmatic CLI helpers for the persona module.

The argparse subparser registration lives in
:mod:`concinno.cli.persona_cmd` (alongside the other ``concinno``
subcommands). This module exposes the underlying handler functions
so they can be tested directly without going through argparse.
"""

from __future__ import annotations

import json
from pathlib import Path

from concinno.persona.persona import InProcessBackend, Persona


def run_chat(
    persona_path: str,
    state_path: str = "",
    *,
    provider: str = "echo",
    model: str = "",
    message: str = "",
) -> str:
    """One-shot chat invocation. Returns the assistant reply.

    ``provider="echo"`` is the default so the CLI works without any
    LLM credentials (suitable for smoke testing). Use
    ``provider="anthropic"`` etc. to hit a real backend.
    """
    backend = InProcessBackend(provider=provider, model=model)
    p = Persona.load(persona_path, state=state_path or None, backend=backend)
    if not message:
        return ""
    reply = p.chat(message)
    if state_path:
        p.save(state_path)
    return reply


def pin_memory(state_path: str, content: str, reason: str = "") -> int:
    """Append a pin record to a persona state log. Returns 0 on success."""
    if not content:
        return 2
    p = _load_minimal(state_path)
    p.pin_memory(content, reason=reason or None)
    p.save(state_path)
    return 0


def list_pinned(state_path: str) -> list[dict[str, str]]:
    """Return all pinned memories from a state log as plain dicts."""
    p = _load_minimal(state_path)
    return [
        {"content": m.content, "pinned_at": m.pinned_at, "reason": m.reason or ""}
        for m in p.pinned()
    ]


def recall_memory(state_path: str, query: str, top_k: int = 3) -> list[dict[str, str]]:
    """Return top-k recall hits as plain dicts for JSON-friendly CLI output."""
    p = _load_minimal(state_path)
    hits = p.recall(query, top_k=top_k)
    return [{"score": f"{h.score:.4f}", "text": h.text} for h in hits]


def _load_minimal(state_path: str) -> Persona:
    """Load a Persona using a placeholder schema when only a state log exists.

    Pin / list-pinned / recall don't need the original persona MD —
    they operate on the state log alone. We synthesise a minimal
    schema so Persona can construct.
    """
    from concinno.persona.schema import PersonaSchema
    from concinno.persona.state import PersonaState

    schema = PersonaSchema(name=Path(state_path).stem or "persona")
    state = PersonaState.load(state_path) if state_path else PersonaState.empty()
    return Persona(schema=schema, state=state, backend=InProcessBackend(provider="echo"))


def format_pinned_text(rows: list[dict[str, str]]) -> str:
    if not rows:
        return "(no pinned memories)"
    lines: list[str] = []
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. {r['content']}")
        if r.get("reason"):
            lines.append(f"   reason: {r['reason']}")
        if r.get("pinned_at"):
            lines.append(f"   pinned: {r['pinned_at']}")
    return "\n".join(lines)


def format_recall_text(rows: list[dict[str, str]]) -> str:
    if not rows:
        return "(no matches)"
    return "\n".join(f"{r['score']}  {r['text']}" for r in rows)


def to_json(rows: list[dict[str, str]]) -> str:
    return json.dumps(rows, ensure_ascii=False, indent=2)


__all__ = [
    "format_pinned_text",
    "format_recall_text",
    "list_pinned",
    "pin_memory",
    "recall_memory",
    "run_chat",
    "to_json",
]
