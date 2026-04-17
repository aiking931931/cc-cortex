"""concinno.cognitive_pool_inject — Pool-to-context injection layer.

@module cognitive_pool_inject
@responsibility Convert :class:`~concinno.cache.cognitive_pool.CognitivePool`
    sections into a token-capped subagent ``additionalContext`` block,
    ranked by simple keyword relevance against the incoming task prompt.
    This closes the islanded-module gap between the 1.16 cache layer and
    the 1.16 cognitive_inject knowledge router: pool sections were being
    written cross-session but never read into a subagent's primacy slot.

@dependencies concinno.cache.cognitive_pool (lazy import, fail-safe)
@exports build_pool_context, score_section, DEFAULT_MAX_SECTIONS,
    DEFAULT_MAX_CHARS, DEFAULT_MAX_SECTION_CHARS

Failure contract:
    Every public entry point catches all exceptions and returns ``""``.
    A broken pool file, a missing module, or a corrupt section MUST NOT
    break SubagentStart — pool inject is supplementary, not load-bearing.
    Tests exercise this contract explicitly.

Token budget (rough):
    DEFAULT_MAX_CHARS = 3500 ≈ 900 tokens. Sits inside the ~1500-3000t
    PromptEngine sweet spot already consumed by thinking_directives +
    rag + delivery; pool layer is the SMALLEST of the four because
    cross-session memory is the lowest-confidence signal.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from concinno.cache.cognitive_pool import PoolSection

__all__ = [
    "DEFAULT_MAX_CHARS",
    "DEFAULT_MAX_SECTIONS",
    "DEFAULT_MAX_SECTION_CHARS",
    "build_pool_context",
    "score_section",
]


# ── Constants ────────────────────────────────────────────────

#: Maximum sections to include per inject call. Three is enough to
#: surface the most relevant cross-session memories without crowding
#: out the higher-priority thinking_directives + rag layers.
DEFAULT_MAX_SECTIONS = 3

#: Maximum total characters across all included sections.
#: ~3500 chars ≈ ~900 tokens, the pool layer's slice of the 1500-3000t
#: PromptEngine sweet spot.
DEFAULT_MAX_CHARS = 3500

#: Maximum chars per single section body before truncation.
#: A truncated section is appended with a marker so the subagent can
#: recognise the cut and ask follow-up questions if needed.
DEFAULT_MAX_SECTION_CHARS = 1500

#: Minimum tokens per section needed to consider it for inject. Avoids
#: surfacing single-line stubs that waste budget.
_MIN_SECTION_TOKENS = 3

#: English stop-words stripped during tokenisation. Kept short — the
#: scoring is heuristic, not statistical, and over-aggressive filtering
#: hurts more than it helps for short user prompts.
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "of", "in", "on", "at", "to", "for",
    "is", "it", "this", "that", "with", "as", "by", "from", "be", "are",
    "was", "were", "do", "does", "did", "have", "has", "had", "but",
    "not", "no", "if", "so", "than", "then",
})

#: Letter-run tokenizer. Matches Unicode letter runs of length >= 3,
#: treating digits, underscores, and punctuation as separators. This
#: splits identifier-style titles like ``subagent_inject`` into
#: ``subagent`` + ``inject``, and keeps CJK letter runs intact.
_TOKEN_RE = re.compile(r"[^\W\d_]{3,}", re.UNICODE)


# ── Tokenisation + scoring ───────────────────────────────────


def _tokenize(text: str) -> set[str]:
    """Lowercase + word-boundary split + stop-word filter.

    Returns a set (not a multiset) — duplicate occurrences inside the
    same section don't multiply the score. Empty/None text returns an
    empty set instead of raising.
    """
    if not text:
        return set()
    return {
        token
        for token in (m.group(0).lower() for m in _TOKEN_RE.finditer(text))
        if token not in _STOPWORDS
    }


def score_section(section: "PoolSection", query_tokens: set[str]) -> float:
    """Score a pool section against a tokenised query.

    Heuristic: title overlap weighted 3x body overlap. Title matches
    are stronger because pool section titles are deliberately curated
    (``user.goals``, ``session.blockers``) while bodies drift.

    The result is normalised by query token count so a 1-token query
    matching one title token scores 3.0 / 1 = 3.0; a 5-token query
    matching three body tokens scores 3 / 5 = 0.6. Comparison across
    queries is meaningless; comparison across sections for the same
    query is the only valid use.

    Args:
        section: A :class:`PoolSection` from a loaded pool.
        query_tokens: The output of :func:`_tokenize` on the task prompt.

    Returns:
        A non-negative float. Empty queries return ``0.0`` so callers
        fall back to recency ranking.
    """
    if not query_tokens:
        return 0.0
    title_tokens = _tokenize(section.title)
    body_tokens = _tokenize(section.body)
    if not title_tokens and not body_tokens:
        return 0.0
    title_overlap = len(title_tokens & query_tokens)
    body_overlap = len(body_tokens & query_tokens)
    return (3.0 * title_overlap + body_overlap) / max(1, len(query_tokens))


def _truncate_body(body: str, limit: int) -> str:
    """Truncate ``body`` to ``limit`` chars, append a marker if cut.

    The marker is a literal ``[...truncated]`` on its own line so a
    subagent that cares can ask the parent to surface the rest. We
    leave a 20-char headroom so the marker itself fits within the cap.
    """
    if len(body) <= limit:
        return body
    return body[: max(0, limit - 20)].rstrip() + "\n[...truncated]"


# ── Public entry point ───────────────────────────────────────


def build_pool_context(
    *,
    task_prompt: str = "",
    max_sections: int = DEFAULT_MAX_SECTIONS,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_section_chars: int = DEFAULT_MAX_SECTION_CHARS,
    pool: Any | None = None,
) -> str:
    """Render top-N pool sections as injectable subagent context.

    This is the **only** public entry point and is intentionally the
    simplest possible API: one optional task prompt in, one rendered
    string out. The string is ready to append to a SubagentStart
    ``additionalContext`` payload, or to concatenate after the existing
    rag/delivery sections in :func:`concinno.cognitive_inject.build_cognitive_context`.

    Selection algorithm:
        1. Load all sections via ``pool.read_all()``.
        2. If the task prompt yields tokens, score each section and
           sort by ``(score, updated_ts)`` descending. Drop zero-score
           sections unless that would empty the result, in which case
           fall back to the top ``max_sections`` regardless of score.
        3. If the task prompt is empty, sort purely by recency
           (``updated_ts`` descending).
        4. Walk the ranking, accumulating sections until either
           ``max_sections`` or ``max_chars`` is hit.
        5. Per-section bodies are truncated to ``max_section_chars``.
        6. Return a markdown block, or ``""`` if nothing was selected.

    Args:
        task_prompt: Subagent's task description for relevance ranking.
            Empty → recency fallback.
        max_sections: Hard cap on sections included.
        max_chars: Hard cap on combined section body characters
            (rough proxy for token budget).
        max_section_chars: Per-section body truncation threshold.
        pool: Optional pre-built ``CognitivePool`` instance. Production
            callers leave this ``None`` and we instantiate a default-
            rooted pool lazily inside the try/except. Tests pass a
            stub or a real pool with a temp dir.

    Returns:
        A rendered markdown block, or ``""`` when no sections were
        loaded, when the pool failed to import/parse, or when nothing
        survived the relevance + cap pipeline.

    Failure mode:
        Catches **all** exceptions and returns ``""``. The pool inject
        layer is supplementary; subagent injection MUST keep working
        even if the pool file is corrupt or the cache module is gone.
    """
    try:
        if pool is None:
            from concinno.cache.cognitive_pool import CognitivePool

            pool = CognitivePool()
        sections = list(pool.read_all())
    except Exception:
        return ""

    if not sections:
        return ""

    # Drop sections that are too small to be worth the budget.
    sections = [
        s for s in sections
        if len(_tokenize(s.body)) >= _MIN_SECTION_TOKENS
        or len(_tokenize(s.title)) >= 1
    ]
    if not sections:
        return ""

    query = _tokenize(task_prompt)
    if query:
        scored = [(score_section(s, query), s) for s in sections]
        scored.sort(key=lambda pair: (pair[0], pair[1].updated_ts), reverse=True)
        positive = [s for sc, s in scored if sc > 0]
        ranked = positive if positive else [s for _, s in scored]
    else:
        ranked = sorted(sections, key=lambda s: s.updated_ts, reverse=True)

    selected: list[tuple[str, str, tuple[str, ...]]] = []
    total = 0
    for section in ranked[:max_sections]:
        body = _truncate_body(section.body.strip(), max_section_chars)
        # Cost = title + body + framing chars (### prefix, newlines, brackets).
        cost = len(section.title) + len(body) + 12
        if total + cost > max_chars and selected:
            # Already have at least one — stop instead of overflowing.
            break
        selected.append((section.title, body, tuple(section.tags or ())))
        total += cost
        if total >= max_chars:
            break

    if not selected:
        return ""

    lines = ["🧠 Cross-session pool (cognitive_pool):"]
    for title, body, tags in selected:
        tag_suffix = f" [{','.join(tags)}]" if tags else ""
        lines.append(f"### {title}{tag_suffix}")
        lines.append(body.rstrip())
    return "\n".join(lines)
