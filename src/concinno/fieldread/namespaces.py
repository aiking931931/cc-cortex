# SPDX-License-Identifier: AGPL-3.0-or-later
"""5 fixed semantic namespaces (Cigito v3 patent moat axis 3, governance side).

Per Cigito v3 strategic anchor (2026-04-29 SOTA synthesis under 2026-only
filter): patent novelty axis 3 = FieldRead 5 fixed semantic namespaces +
breadcrumb chain. All 9 compressed-retrieval / RAG papers (2024-2026)
surveyed are superseded by V4 §3.6 heterogeneous + on-disk KV under the
2026-only filter — **no 2026 frontier paper** formalises a fixed semantic
namespace partition with breadcrumb-tracked retrieval audit trail as an
architectural contract.

Two-side ship rationale (license firewall):
    * **Lyceum-adapter** (`lyceum_adapter.field_read`): 5 namespaces
      tuned to Lyceum substrate (user_pref / dev_practice /
      project_context / external_facts / runtime_state).
    * **aiking_core** (`aiking_core.fieldread.namespaces`, AGPL):
      governance-runtime mirror that delegates heavy parsing to
      ``aiking_core.fieldread._core``.
    * **Concinno main** (this module, AGPL): governance-library
      canonical 5 namespaces — **no aiking_core runtime dependency**
      (Concinno is upstream of aiking_core; aiking-core depends on
      concinno via the deprecation shim, not the other way).

Distinction vs prior art:
    * RAG (vanilla): unstructured similarity retrieval, no namespace.
    * GraphRAG / LightRAG: dynamic graph traversal, no fixed partition.
    * V4 §3.6 heterogeneous KV: storage-layer optimisation, not semantic.

The five namespaces map onto Concinno's governance source-of-truth folders:

    cognition / skills / feedback / handoff / audit

Cigito v3 ZIQ binding:
    P(namespace | query) ∝ SPS(domain) × FTRL(outcome)
    SPS slot = lexical priors + path priors (this module's heuristics).
    FTRL slot = retrieval outcome learning (delegated to caller's ZIQ
    wrapper — :class:`Breadcrumb` chains carry the audit trail).
"""

from __future__ import annotations

import re
from typing import Final

__all__ = [
    "AUDIT",
    "COGNITION",
    "FEEDBACK",
    "HANDOFF",
    "Namespace",
    "NAMESPACES",
    "SKILLS",
    "is_namespace",
    "route",
]


# ── Namespace constants (the patent-moat fixed partition) ──────────

#: Cognition layer artifacts — CBUA decisions, red-team verdicts,
#: commander rulings, rule files, premise gates.
COGNITION: Final[str] = "cognition"

#: Skill registry / drafts / proposals / accept-reject log.
SKILLS: Final[str] = "skills"

#: Sediment / corrections / kb_*/L3 / regret journal / MEMORY.md.
FEEDBACK: Final[str] = "feedback"

#: Handoff Index / Summary / Archive routers (the 三層 architecture).
HANDOFF: Final[str] = "handoff"

#: Token audits / switches audit / publish lock journal / runtime traces.
AUDIT: Final[str] = "audit"

#: Canonical ordering of the 5 namespaces. **Do not reorder** —
#: downstream callers index into this tuple for ZIQ FTRL slot lookup.
NAMESPACES: Final[tuple[str, ...]] = (
    COGNITION,
    SKILLS,
    FEEDBACK,
    HANDOFF,
    AUDIT,
)

#: Type alias for callers preferring an explicit name.
Namespace = str  # str-typed; validated via :func:`is_namespace`.


# ── Lexical priors (SPS slot of P ∝ SPS × FTRL) ───────────────────

# Order matters — first match wins. Patterns are case-insensitive and
# match against the path *string* (filename + parent directory tokens).
# Path priors dominate filename priors when both fire.
_PATH_PRIORS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # SKILL.md filename wins over any ancestor directory — the file's
    # specific identity outranks its parent's name.
    (SKILLS, re.compile(r"(?:^|[/\\])SKILL\.md$", re.IGNORECASE)),
    (FEEDBACK, re.compile(r"(?:^|[/\\])feedback_", re.IGNORECASE)),
    (FEEDBACK, re.compile(r"(?:^|[/\\])kb_", re.IGNORECASE)),
    (FEEDBACK, re.compile(r"(?:^|[/\\])MEMORY\.md$", re.IGNORECASE)),
    (FEEDBACK, re.compile(r"(?:^|[/\\])memory[/\\]", re.IGNORECASE)),
    (SKILLS, re.compile(r"(?:^|[/\\])skills?[/\\]", re.IGNORECASE)),
    (SKILLS, re.compile(r"(?:^|[/\\])skill_drafts?[/\\]", re.IGNORECASE)),
    (HANDOFF, re.compile(r"(?:^|[/\\])交接", re.IGNORECASE)),
    (HANDOFF, re.compile(r"(?:^|[/\\])handoff", re.IGNORECASE)),
    (HANDOFF, re.compile(r"(?:^|[/\\])06_Handoffs[/\\]", re.IGNORECASE)),
    (AUDIT, re.compile(r"(?:^|[/\\])audit[/\\]", re.IGNORECASE)),
    (AUDIT, re.compile(r"(?:^|[/\\]).*_audit\.(md|jsonl?|log)$", re.IGNORECASE)),
    (AUDIT, re.compile(r"(?:^|[/\\])token_audit", re.IGNORECASE)),
    (AUDIT, re.compile(r"(?:^|[/\\])trace[s]?[/\\]", re.IGNORECASE)),
    (COGNITION, re.compile(r"(?:^|[/\\])cbua", re.IGNORECASE)),
    (COGNITION, re.compile(r"(?:^|[/\\])cognition[/\\]", re.IGNORECASE)),
    (COGNITION, re.compile(r"(?:^|[/\\])rules?[/\\]", re.IGNORECASE)),
    (COGNITION, re.compile(r"(?:^|[/\\])L[01]\.md$", re.IGNORECASE)),
)

# Lexical priors fire on raw query keywords (not paths). Lower priority
# than path priors but higher than the default fallback.
_LEXICAL_PRIORS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (FEEDBACK, re.compile(
        r"\b(feedback|sediment|correction|memory|kb_|lesson)\b",
        re.IGNORECASE,
    )),
    (HANDOFF, re.compile(
        r"\b(handoff|交接|next[_-]?step|takeover)\b",
        re.IGNORECASE,
    )),
    (SKILLS, re.compile(
        r"\b(skill|skill\.md|skill_drafts?)\b",
        re.IGNORECASE,
    )),
    (AUDIT, re.compile(
        r"\b(audit|trace|token[_-]?audit|decision[_-]?log)\b",
        re.IGNORECASE,
    )),
    (COGNITION, re.compile(
        r"\b(cbua|cognition|rule|reasoning|wiredo|premise[_-]?gate)\b",
        re.IGNORECASE,
    )),
)


# ── Public API ─────────────────────────────────────────────────────


def is_namespace(value: str) -> bool:
    """Return ``True`` iff ``value`` is one of :data:`NAMESPACES`."""
    return value in NAMESPACES


def route(query: str) -> str:
    """Route a query (path or keyword string) to its primary namespace.

    Resolution order (Cigito v3 SPS slot):

    1. **Path priors** — if ``query`` looks like a path and matches one
       of the canonical Concinno folder patterns, use that namespace.
    2. **Lexical priors** — keyword regex on the raw string.
    3. **Default fallback** — :data:`COGNITION` (the broadest namespace;
       safer than over-routing to feedback / audit).

    Args:
        query: A path string, filename, or keyword phrase.

    Returns:
        One of :data:`NAMESPACES`.

    Examples:
        >>> route("交接_concinno.md")
        'handoff'
        >>> route("feedback_intent_drift.md")
        'feedback'
        >>> route("CBUA pipeline question")
        'cognition'
        >>> route("")
        'cognition'
    """
    if not query:
        return COGNITION

    # Path prior pass — checked even on plain filenames (no separator).
    for ns, pat in _PATH_PRIORS:
        if pat.search(query):
            return ns

    # Lexical prior pass — for non-path queries.
    for ns, pat in _LEXICAL_PRIORS:
        if pat.search(query):
            return ns

    return COGNITION
