"""concinno.skills.schema — Pydantic schemas for SKILL.md frontmatter.

@module skills.schema
@responsibility Declarative contract for SKILL.md frontmatter beyond the
    bare ``name`` / ``description`` / ``triggers`` keys parsed by
    :mod:`concinno.skill_parser`. The 2.35.0 ship adds
    :class:`EventBinding` so a skill can declare *when* it should run
    (e.g. "after every Edit on a test file") rather than relying on the
    user to type a slash-command. The runtime that consumes these
    bindings lives downstream in Sancio 0.6 ``event_dispatcher`` —
    concinno owns the schema, Sancio owns the dispatcher. CC harness
    ignores unknown frontmatter keys so legacy SKILL.md files keep
    working without changes.
@dependencies pydantic (already a hard dep of concinno)
@exports EVENT_TYPES, EventBinding, parse_event_bindings

Backward compatibility contract:
    1. Adding a new optional field to :class:`EventBinding` is allowed
       without bumping concinno major.
    2. Removing or renaming an existing field is a major bump.
    3. Tightening a validator (e.g. shrinking ``EVENT_TYPES``) is a
       major bump.
    4. The wire format is the YAML/JSON dict produced by
       :mod:`concinno.skill_parser`; any change there is also a major.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Allowed Claude Code hook event names. Mirrors the public hooks docs
# at https://docs.anthropic.com/en/docs/claude-code/hooks. Sancio
# dispatcher only fires on events the host harness actually emits;
# concinno's job is to refuse SKILL.md frontmatter that misspells one.
EVENT_TYPES: frozenset[str] = frozenset(
    {
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
        "Stop",
        "SubagentStop",
        "SessionStart",
        "Notification",
    }
)


class EventBinding(BaseModel):
    """One declarative event-to-skill binding.

    SKILL.md authors declare bindings as a list under the
    ``event_bindings`` frontmatter key. Each entry maps an event name
    to a skill invocation, with optional gating (``when``), priority
    ordering, and rate limiting (``cooldown_seconds``).

    Example (in YAML)::

        event_bindings:
          - event: PostToolUse
            when: 'tool_name == "Edit" and "test_" in file_path'
            invoke: triage_failed_test
            priority: 80
          - event: Stop
            invoke: handoff_check
            cooldown_seconds: 30

    The ``when`` predicate is a string by design — concinno does **not**
    eval it. The Sancio dispatcher decides on its own evaluation
    semantics (e.g. a safe-eval AST walker or a structured DSL).
    Concinno's responsibility ends at "we round-tripped a syntactically
    valid binding".
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    event: str = Field(
        ...,
        description=(
            "Hook event name. Must be one of EVENT_TYPES; misspellings "
            "are rejected at parse time so the dispatcher never has to."
        ),
    )
    invoke: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description=(
            "What to call when the event fires. Resolved by the "
            "downstream Sancio dispatcher — typically a skill name "
            "(``my_skill``) or a slash command (``/my_skill``). "
            "Concinno does not interpret this string."
        ),
    )
    when: str | None = Field(
        default=None,
        max_length=500,
        description=(
            "Optional predicate evaluated against the hook payload. "
            "Concinno stores it as an opaque string; Sancio decides "
            "the evaluation surface."
        ),
    )
    priority: int = Field(
        default=50,
        ge=0,
        le=100,
        description=(
            "Higher values run earlier when several bindings match the "
            "same event. Ties broken in lexical order of ``invoke``."
        ),
    )
    cooldown_seconds: float = Field(
        default=0.0,
        ge=0.0,
        le=86_400.0,
        description=(
            "Minimum seconds between consecutive invocations of this "
            "binding within a single session. ``0`` disables the "
            "throttle. Sancio enforces; concinno only validates the "
            "value range."
        ),
    )

    @field_validator("event")
    @classmethod
    def _validate_event(cls, v: str) -> str:
        if v not in EVENT_TYPES:
            allowed = ", ".join(sorted(EVENT_TYPES))
            raise ValueError(
                f"unknown event {v!r}; allowed: {allowed}",
            )
        return v


def parse_event_bindings(meta: dict[str, Any]) -> list[EventBinding]:
    """Extract :class:`EventBinding` entries from a parsed frontmatter dict.

    Tolerates the absence of the key (legacy SKILL.md files), a
    non-list value (returns empty), and per-entry validation errors —
    one bad binding does not invalidate the rest. Per-entry parse
    failures are silently dropped because SKILL.md authoring is end-
    user code; we'd rather skip a malformed binding than refuse to
    load the rest of the skill.

    Args:
        meta: Output of :func:`concinno.skill_parser.parse_skill_md`.

    Returns:
        A list of validated :class:`EventBinding` instances. Empty
        when no key is present, when the value is not a list, or when
        every entry fails validation.
    """
    raw = meta.get("event_bindings")
    if not isinstance(raw, list):
        return []
    bindings: list[EventBinding] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            bindings.append(EventBinding.model_validate(item))
        except Exception:  # noqa: BLE001 - skip malformed binding, keep rest
            continue
    return bindings
