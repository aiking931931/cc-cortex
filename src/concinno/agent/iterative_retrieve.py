"""concinno.agent.iterative_retrieve — Letta-inspired three-layer cascade retrieval.

@module agent.iterative_retrieve
@responsibility Orchestrate a three-layer retrieval cascade:
    L3 (cognitive_pool keyword search, 0 LLM cost) →
    L2 (l2_distill fast-path, 0 LLM cost) →
    L1 (caller-supplied RAG, full cost).
    When L1 discovers new facts, optionally schedule async A-MEM
    evolution back into L2/L3. Tracks per-layer hit-rate statistics
    for introspection and ZIQ routing decisions.

    The design mirrors the Letta benchmark pattern (74% on LOCOMO)
    where letting an agent grep its own notes before falling through
    to dense retrieval is the single biggest measured uplift.

@dependencies concinno.cache.cognitive_pool, concinno.cache.l2_distill
@exports RetrievalResult, L1Retriever, EvolutionScheduler,
    CascadeConfig, CascadeStats, IterativeRetriever
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from concinno.cache.cognitive_pool import CognitivePool, PoolSection
from concinno.cache.l2_distill import L2Distiller, RawHit

__all__ = [
    "CascadeConfig",
    "CascadeStats",
    "EvolutionScheduler",
    "IterativeRetriever",
    "L1Retriever",
    "RetrievalResult",
]

# ---------------------------------------------------------------------------
# Tokenisation (same contract as l2_distill._tokenize and fewshot)
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9_]{3,}")


def _tokenize(text: str) -> set[str]:
    """Extract matchable keyword tokens from *text*.

    Three-char minimum, lowercased, underscore-preserving — identical
    to the contract in :mod:`concinno.cache.l2_distill` so L3 keyword
    search and L2 fast-path produce consistent token bags.
    """
    return set(_TOKEN_RE.findall(text.lower()))


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass
class RetrievalResult:
    """Unified result from the three-layer cascade.

    Attributes:
        sections: Pool sections gathered from L3 and/or L2.
        raw_hits: L1 retrieval results (empty when L3/L2 sufficed).
        source_layer: Human-readable tag indicating which layers
            contributed: ``"L3"`` / ``"L2"`` / ``"L1"`` / ``"L3+L1"``
            / ``"L2+L1"``.
        l3_hit: Whether L3 returned sufficient sections.
        l2_hit: Whether L2 returned sufficient sections.
        l1_hit: Whether L1 returned any hits.
        evolution_scheduled: Whether L1 hits triggered an A-MEM
            evolution schedule.
    """

    sections: list[PoolSection]
    raw_hits: list[RawHit]
    source_layer: str
    l3_hit: bool
    l2_hit: bool
    l1_hit: bool
    evolution_scheduled: bool


# ---------------------------------------------------------------------------
# Caller-supplied protocols
# ---------------------------------------------------------------------------


class L1Retriever(Protocol):
    """Caller supplies L1 RAG (e.g. ZIQ v6 BM25+dense+FTRL)."""

    def retrieve(self, query: str, *, top_k: int = 5) -> list[RawHit]: ...


class EvolutionScheduler(Protocol):
    """Caller supplies async A-MEM evolution trigger.

    CCC is sync-only; the scheduler is expected to queue the work for
    later (e.g. via a background thread or task queue).
    """

    def schedule(self, query: str, hits: list[RawHit]) -> None: ...


# ---------------------------------------------------------------------------
# Configuration and statistics
# ---------------------------------------------------------------------------


@dataclass
class CascadeConfig:
    """Tuning knobs for the three-layer cascade.

    Attributes:
        l3_min_sections: Minimum L3 hits to consider L3 sufficient.
        l2_min_sections: Minimum L2 hits to consider L2 sufficient.
        l1_top_k: ``top_k`` passed to the L1 retriever.
        always_check_l1: When ``True``, L1 runs even when L3/L2 hit
            (useful for background evolution without blocking on L1).
        schedule_evolution: When ``True``, L1 new hits trigger an
            A-MEM evolution schedule via the caller's scheduler.
    """

    l3_min_sections: int = 1
    l2_min_sections: int = 1
    l1_top_k: int = 5
    always_check_l1: bool = False
    schedule_evolution: bool = True


@dataclass
class CascadeStats:
    """In-memory counters for cascade behaviour introspection.

    All counters are cumulative and reset only via
    :meth:`IterativeRetriever.reset_stats`.
    """

    queries: int = 0
    l3_sufficient: int = 0
    l2_sufficient: int = 0
    l1_fallthrough: int = 0
    evolutions_scheduled: int = 0


# ---------------------------------------------------------------------------
# IterativeRetriever
# ---------------------------------------------------------------------------


class IterativeRetriever:
    """Three-layer cascade retriever: L3 → L2 → L1.

    Args:
        pool: The L3 :class:`~concinno.cache.cognitive_pool.CognitivePool`.
        distiller: The L2 :class:`~concinno.cache.l2_distill.L2Distiller`.
        l1: Optional caller-supplied L1 retriever. When ``None``, the
            cascade stops at L2 — no L1 fallthrough occurs.
        scheduler: Optional A-MEM evolution scheduler. When ``None``,
            evolution scheduling is silently skipped even if L1 hits.
        config: Cascade tuning. ``None`` → defaults.
    """

    def __init__(
        self,
        *,
        pool: CognitivePool,
        distiller: L2Distiller,
        l1: L1Retriever | None = None,
        scheduler: EvolutionScheduler | None = None,
        config: CascadeConfig | None = None,
    ) -> None:
        self._pool = pool
        self._distiller = distiller
        self._l1 = l1
        self._scheduler = scheduler
        self._config = config or CascadeConfig()
        self._stats = CascadeStats()

    # ---- public API ------------------------------------------------------

    def retrieve(self, query: str) -> RetrievalResult:
        """Run the three-layer cascade and return a unified result.

        Flow:
        1. Try L3 keyword search over cognitive_pool sections.
        2. If L3 insufficient → try L2 fast-path via distiller.retrieve.
        3. If still insufficient (or ``always_check_l1``) → call L1.
        4. If L1 finds hits and evolution is enabled → schedule A-MEM.
        5. Merge sections (dedup by title, L3 wins), build result.
        """
        cfg = self._config
        self._stats.queries += 1

        l3_sections: list[PoolSection] = []
        l2_sections: list[PoolSection] = []
        raw_hits: list[RawHit] = []
        l3_hit = False
        l2_hit = False
        l1_hit = False
        evolution_scheduled = False

        # --- Step 1: L3 keyword search ------------------------------------
        query_tokens = _tokenize(query)
        if query_tokens:
            all_sections = self._pool.read_all()
            l3_sections = self._keyword_filter(all_sections, query_tokens)

        if len(l3_sections) >= cfg.l3_min_sections:
            l3_hit = True
            self._stats.l3_sufficient += 1

        # --- Step 2: L2 fast-path -----------------------------------------
        if not l3_hit:
            l2_sections = self._distiller.retrieve(
                query=query,
                max_sections=cfg.l2_min_sections * 2,
            )
            if len(l2_sections) >= cfg.l2_min_sections:
                l2_hit = True
                self._stats.l2_sufficient += 1

        # --- Step 3: L1 fallthrough ---------------------------------------
        need_l1 = (not l3_hit and not l2_hit) or cfg.always_check_l1
        if need_l1 and self._l1 is not None:
            raw_hits = self._l1.retrieve(query, top_k=cfg.l1_top_k)
            l1_hit = len(raw_hits) > 0
            if l1_hit:
                self._stats.l1_fallthrough += 1

        # --- Step 4: evolution scheduling ---------------------------------
        if l1_hit and cfg.schedule_evolution and self._scheduler is not None:
            self._scheduler.schedule(query, raw_hits)
            evolution_scheduled = True
            self._stats.evolutions_scheduled += 1

        # --- Step 5: merge and dedup --------------------------------------
        merged_sections = self._dedup_sections(l3_sections, l2_sections)

        # --- Step 6: source_layer tag -------------------------------------
        source_layer = self._compute_source_layer(l3_hit, l2_hit, l1_hit)

        return RetrievalResult(
            sections=merged_sections,
            raw_hits=raw_hits,
            source_layer=source_layer,
            l3_hit=l3_hit,
            l2_hit=l2_hit,
            l1_hit=l1_hit,
            evolution_scheduled=evolution_scheduled,
        )

    def stats(self) -> CascadeStats:
        """Return the current cascade statistics snapshot."""
        return self._stats

    def reset_stats(self) -> None:
        """Reset all cascade counters to zero."""
        self._stats = CascadeStats()

    # ---- internal helpers ------------------------------------------------

    @staticmethod
    def _keyword_filter(
        sections: list[PoolSection],
        query_tokens: set[str],
    ) -> list[PoolSection]:
        """Return sections whose title or tags overlap with *query_tokens*.

        Scoring is token-set intersection size — identical to the L2
        fast-path in :meth:`L2Distiller.retrieve`. Sections with zero
        overlap are excluded. Sorted by score descending, stable by
        title.
        """
        scored: list[tuple[int, PoolSection]] = []
        for section in sections:
            bag = _tokenize(section.title + " " + " ".join(section.tags))
            score = len(query_tokens & bag)
            if score > 0:
                scored.append((score, section))

        if not scored:
            return []

        scored.sort(key=lambda item: item[1].title)
        scored.sort(key=lambda item: item[0], reverse=True)
        return [section for _score, section in scored]

    @staticmethod
    def _dedup_sections(
        l3: list[PoolSection],
        l2: list[PoolSection],
    ) -> list[PoolSection]:
        """Merge L3 and L2 sections, deduplicating by title.

        L3 wins on title collision (fresher, from the pool directly).
        """
        seen_titles: set[str] = set()
        merged: list[PoolSection] = []

        for section in l3:
            if section.title not in seen_titles:
                seen_titles.add(section.title)
                merged.append(section)

        for section in l2:
            if section.title not in seen_titles:
                seen_titles.add(section.title)
                merged.append(section)

        return merged

    @staticmethod
    def _compute_source_layer(
        l3_hit: bool,
        l2_hit: bool,
        l1_hit: bool,
    ) -> str:
        """Derive a human-readable source tag from the hit flags."""
        if l3_hit and l1_hit:
            return "L3+L1"
        if l2_hit and l1_hit:
            return "L2+L1"
        if l3_hit:
            return "L3"
        if l2_hit:
            return "L2"
        if l1_hit:
            return "L1"
        return "none"
