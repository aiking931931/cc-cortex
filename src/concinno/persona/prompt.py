"""Prompt template builder for Persona.chat().

Plain string formatting — no Jinja2 dep. The persona module is
deliberately zero-extra-deps for Track 1 ship; consumers wanting
templated prompts can compose richer pipelines downstream.

The system prompt is intentionally short and explicit so the
backend LLM has a stable instruction surface across providers
(Anthropic / OpenAI / local). Pinned memories appear at the top
of the system prompt because their job is to anchor identity
facts above any consolidation drift.
"""

from __future__ import annotations

from concinno.persona.pinned_memories import PinnedMemoryStore
from concinno.persona.schema import PersonaSchema


def render_system_prompt(
    schema: PersonaSchema,
    pins: PinnedMemoryStore | None = None,
    extra_context: str = "",
) -> str:
    """Return the system prompt string.

    Layout::

        You are <name>.

        [Personality] <free text>
        [Voice] <free text>

        [Pinned facts — must remain consistent]
        - <pinned 1>
        - <pinned 2>
        ...

        [Background memories]
        - <seed 1>
        - <seed 2>
        ...

        [Emotional baseline] <default> (intensity=<f>)

        [Context] <extra>
    """
    lines: list[str] = []
    lines.append(f"You are {schema.name}.")
    lines.append("")

    if schema.personality:
        lines.append(f"[Personality] {schema.personality}")
    if schema.voice:
        lines.append(f"[Voice] {schema.voice}")
    if schema.personality or schema.voice:
        lines.append("")

    pinned_list: list[str] = []
    if pins is not None:
        pinned_list = [m.content for m in pins.all()]
    # Honour both the static schema pins and the live store.
    for m in schema.pinned_memories:
        if m.content not in pinned_list:
            pinned_list.append(m.content)
    if pinned_list:
        lines.append("[Pinned facts — must remain consistent across the conversation]")
        for p in pinned_list:
            lines.append(f"- {p}")
        lines.append("")

    if schema.memory_seed:
        lines.append("[Background memories]")
        for s in schema.memory_seed:
            lines.append(f"- {s}")
        lines.append("")

    em = schema.emotional_state
    lines.append(
        f"[Emotional baseline] {em.default} (intensity={em.intensity:.2f},"
        f" decay_rate={em.decay_rate:.2f})"
    )

    if extra_context:
        lines.append("")
        lines.append(f"[Context] {extra_context}")

    return "\n".join(lines).strip() + "\n"


def render_recall_context(hits: list[str], header: str = "Relevant past turns") -> str:
    """Format retrieval hits into a context block for the next turn."""
    if not hits:
        return ""
    lines = [f"[{header}]"]
    for h in hits:
        first_line = h.splitlines()[0] if h else ""
        if len(h) > 240:
            h = h[:240] + " ..."
        lines.append(f"- {h}" if not first_line else f"- {h}")
    return "\n".join(lines) + "\n"


__all__ = ["render_system_prompt", "render_recall_context"]
