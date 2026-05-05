"""concinno.cache.anthropic_helpers — reusable cache_control wrappers.

@module cache.anthropic_helpers
@responsibility Apply Anthropic ``cache_control`` blocks to message /
    system payloads from a single, tested code path. Concinno-internal
    callers (escalation chain, llm-as-judge, A2A agent loop, GAIA
    eval runner) historically rolled their own inline breakpoints or
    skipped caching entirely. The result: every Opus→Sonnet→Haiku
    escalation paid the full prompt cost every turn. This module is
    the one place that decides what to mark cacheable.

@dependencies (none — stdlib only)
@exports with_cache_control, system_with_cache, cache_breakpoint,
    STRATEGIES, DEFAULT_STRATEGY

Source contract:
    Sancio's ``providers/anthropic.py`` ``_prepare()`` is the reference
    implementation (:class:`persona.providers.anthropic.AnthropicProvider`).
    This helper supports the same five caching modes:

    ``legacy``    — auto-mark first user message when conversation has
                    ``>1`` turn. Matches 2.6.x Sancio default.
    ``disabled``  — strip every cache_control block. For short prompts
                    that can't hit Anthropic's 1024-token cache floor.
    ``explicit``  — caller passes a list of message indices to mark.
                    Negative indices count from the end, like slicing.
    ``multiturn`` — mark first user + third-from-last user turn. Good
                    for long conversations where the user anchors the
                    context early and the most recent turn is volatile.
    ``length-guard`` — legacy behaviour but only when
                    ``len(system_text) >= cache_min_chars``.

Failure contract:
    Invalid strategy raises :class:`ValueError`. Every other caller
    error (out-of-range index, missing key) is also ``ValueError``.
    Callers upstream convert to whatever domain error type they use.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "DEFAULT_STRATEGY",
    "STRATEGIES",
    "cache_breakpoint",
    "system_with_cache",
    "with_cache_control",
]

#: Supported strategy names. Mirrors
#: ``persona.providers.anthropic._prepare`` modes 1:1 so we can swap
#: the provider implementation under Sancio to use this helper later
#: without a user-visible behaviour change.
STRATEGIES: tuple[str, ...] = (
    "legacy",
    "disabled",
    "explicit",
    "multiturn",
    "length-guard",
)

#: Default when the caller doesn't pass ``strategy``. ``legacy``
#: matches the 2.6.x behaviour of caching only the first user message
#: so existing Concinno-internal consumers see no change.
DEFAULT_STRATEGY = "legacy"


def _cache_control_block(ttl: str | None = None) -> dict[str, Any]:
    """Return the Anthropic ``cache_control`` block for ``ttl``.

    ``ttl=None`` / ``"5m"`` → ``{"type": "ephemeral"}`` (stable,
    no beta header required). ``"1h"`` → the beta-gated extended TTL
    shape. Anything else raises :class:`ValueError` — this mirrors the
    same validation Sancio does so bad values fail here instead of as
    a 400 from Anthropic.
    """
    if ttl is None or ttl == "5m":
        return {"type": "ephemeral"}
    if ttl == "1h":
        return {"type": "ephemeral", "ttl": "1h"}
    msg = f"unsupported cache_ttl: {ttl!r} (valid: None, '5m', '1h')"
    raise ValueError(msg)


def cache_breakpoint(ttl: str | None = None) -> dict[str, Any]:
    """Public alias for :func:`_cache_control_block`.

    Use when a caller hand-rolls the content structure and just needs
    the block to drop in. Prefer :func:`with_cache_control` when you
    have an existing messages list — that function handles mode
    resolution and negative-index translation for you.
    """
    return _cache_control_block(ttl)


def _wrap_content(content: Any, ttl: str | None) -> Any:
    """Attach ``cache_control`` to a message's content.

    Anthropic accepts two shapes:

    * **String content** → wrap in a single text block so we can
      attach ``cache_control``. Non-destructive upgrade.
    * **List-of-blocks** → mutate the LAST block in-place. This
      matches how Sancio's ``_wrap_with_cache`` works: cache the tail
      of the message so earlier blocks (tool_use / images / etc.) stay
      unaffected.

    Returns the wrapped content. The input is not mutated when it's a
    string; lists are copied before mutation to keep caller payloads
    pure.
    """
    block = _cache_control_block(ttl)
    if isinstance(content, str):
        return [
            {"type": "text", "text": content, "cache_control": block},
        ]
    if isinstance(content, list) and content:
        copy = [dict(b) if isinstance(b, dict) else b for b in content]
        last = copy[-1]
        if isinstance(last, dict):
            last["cache_control"] = block
        return copy
    # Fallback: unknown content shape — wrap as text so we still
    # emit SOMETHING cacheable rather than silently swallowing.
    return [
        {
            "type": "text",
            "text": str(content) if content is not None else "",
            "cache_control": block,
        },
    ]


def _resolve_indices(
    strategy: str,
    messages: list[dict[str, Any]],
    *,
    explicit_indices: list[int] | None,
) -> set[int]:
    """Compute which message indices receive a cache_control block.

    Centralised so every strategy branch goes through the same index
    normalisation (negative → positive, bounds check, dedup). Returns
    an empty set when ``strategy == "disabled"`` or when the message
    list is too short for the strategy to make sense.
    """
    n = len(messages)
    if n == 0 or strategy == "disabled":
        return set()

    if strategy == "legacy":
        if n <= 1:
            return set()
        for i, m in enumerate(messages):
            if isinstance(m, dict) and m.get("role") == "user":
                return {i}
        return set()

    if strategy == "explicit":
        if explicit_indices is None:
            msg = "explicit strategy requires cache_breakpoints list"
            raise ValueError(msg)
        out: set[int] = set()
        for raw in explicit_indices:
            real = raw if raw >= 0 else n + raw
            if real < 0 or real >= n:
                msg = f"cache_breakpoints index out of range: {raw}"
                raise ValueError(msg)
            out.add(real)
        return out

    if strategy == "multiturn":
        user_positions = [
            i for i, m in enumerate(messages)
            if isinstance(m, dict) and m.get("role") == "user"
        ]
        out = set()
        if user_positions and n > 1:
            out.add(user_positions[0])
        if len(user_positions) >= 3:
            out.add(user_positions[-3])
        return out

    if strategy == "length-guard":
        # Length-guard defers to legacy but only when the CALLER has
        # already decided the prompt is long enough. Here the guard
        # has passed — behaviour is identical to legacy.
        return _resolve_indices(
            "legacy", messages, explicit_indices=None,
        )

    msg = (
        f"unknown cache strategy: {strategy!r} "
        f"(valid: {', '.join(STRATEGIES)})"
    )
    raise ValueError(msg)


def with_cache_control(
    messages: list[dict[str, Any]],
    *,
    strategy: str = DEFAULT_STRATEGY,
    breakpoints: list[int] | None = None,
    ttl: str | None = None,
) -> list[dict[str, Any]]:
    """Return a copy of ``messages`` with cache_control blocks applied.

    The input list is never mutated — callers can keep the original
    for retry logic or logging. Shallow-copied message dicts retain
    everything except the content field on cached positions, which is
    replaced with a list-of-blocks carrying ``cache_control``.

    Args:
        messages: List of Anthropic-format message dicts. Each MUST
            have a ``role`` key (``"user"``/``"assistant"``/``"system"``)
            and a ``content`` field (str or list-of-blocks).
        strategy: One of :data:`STRATEGIES`. Defaults to ``legacy``.
        breakpoints: Required when ``strategy == "explicit"``. Integer
            indices; negatives count from the end. Ignored otherwise.
        ttl: ``None`` / ``"5m"`` / ``"1h"``. Controls the TTL embedded
            in every cache_control block. Extended TTL requires the
            ``extended-cache-ttl-2025-04-11`` beta header upstream —
            this helper does NOT add headers (that's the caller's job).

    Returns:
        A new list with zero or more messages rewritten. Shape is
        preserved so downstream SDK code sees a drop-in replacement.

    Raises:
        ValueError: Unknown strategy, missing explicit indices, index
            out of range, or unsupported TTL.
    """
    if not isinstance(messages, list):
        msg = "messages must be a list of dicts"
        raise TypeError(msg)

    indices = _resolve_indices(
        strategy, messages, explicit_indices=breakpoints,
    )
    if not indices:
        # Return a shallow copy so callers that store the return in
        # place don't end up with an alias to the original.
        return [dict(m) if isinstance(m, dict) else m for m in messages]

    out: list[dict[str, Any]] = []
    for i, m in enumerate(messages):
        if not isinstance(m, dict):
            out.append(m)
            continue
        if i not in indices:
            out.append(dict(m))
            continue
        wrapped = dict(m)
        wrapped["content"] = _wrap_content(m.get("content", ""), ttl)
        out.append(wrapped)
    return out


def system_with_cache(
    system_text: str,
    *,
    ttl: str | None = None,
) -> list[dict[str, Any]]:
    """Return the Anthropic ``system`` block with a cache_control block.

    Anthropic's API accepts ``system`` as either a string (implicit
    single text block, uncacheable) or a list of typed blocks. We
    always return the block form so the caller can drop us into
    ``messages.create(system=...)`` and get caching for free.

    Empty / whitespace-only text falls back to a placeholder prompt
    to avoid 400s from the upstream API. Callers that want truly
    empty system should just not call this function.
    """
    text = system_text.strip() if isinstance(system_text, str) else ""
    if not text:
        text = "You are a helpful assistant."
    return [{
        "type": "text",
        "text": text,
        "cache_control": _cache_control_block(ttl),
    }]
