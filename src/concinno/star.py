"""concinno.star — Stimulus-Triggered Agentic Retrieval (STAR) Engine.

I am a perfectionist who builds retrieval systems, but I know when to stop.
Perfection in retrieval is not "find everything" — it is "find exactly enough."
Three pieces of gold outweigh ten pieces of mixed ore.

@module star
@responsibility Unified cognitive RAG engine for AI agents.
    Routes question complexity → retrieval depth → token budget.
    Integrates with Riverbed Memory (concinno.riverbed) for depth-based recall.
    Precision-first: 3 high-quality results > 5 mixed (Liu 2023, Cuconasu 2024).
@dependencies concinno.rag (optional vector backbone), concinno.riverbed (optional depth),
    concinno.knowledge (optional corrections/learnings)
@exports STAREngine, RetrievalResult, ConfidenceVerdict, RetrievalTier,
    QueryPlanner, MultiSourceRetriever, ConfidenceGate, CRAGCorrector,
    FreshnessScorer, AssociativeIndex, SharedMemoryBus, SessionCache

Relationship to RMT (concinno.riverbed):
    RMT handles HOW memories form, decay, and emotionally charge.
    STAR handles WHEN to retrieve, HOW DEEP, and WHETHER to trust the result.

CBUA Integration (Cognitive-Behavioral Unified Architecture):
    C0 Perception → route complexity → choose tier → allocate budget
    B0 Fast → L0 Index (known pattern, direct recall)
    B1 Structured → L1 Summary (rerank + compress)
    B2 Deep → L2 Full (multi-source + CRAG verify + web fallback + associate)
    B4 Metacognition → confidence cap enforcement + freshness decay
    A5 Defense → noise ratio guard + adaptive forgetting

Original contributions:
    1. Cognitive-RAG Routing — complexity → tier → budget as unified decision chain
    2. Adaptive Forgetting (Adaptive Forgetting) — 4-strategy ranked eviction (beyond TTL/LRU)

Built on prior art (credited, not claimed as original):
    - CRAG (Yan 2024): Corrective retrieval actions
    - Adaptive RAG (Jeong 2024): Complexity-based routing concept
    - Ebbinghaus (1885) + SM-2: Temporal decay with reinforcement
    - Collins & Loftus (1975): Spreading activation for association
    - Liu 2023: Lost in the Middle — 3 docs optimal, >5 harmful
    - Cuconasu 2024: Noise ratio >30% actively harmful

Three-Tier Architecture (maps to CBUA C0 cognitive depth):

    L0 Index    — Metadata scan only. File paths + headings + scores.
                   ~10 tokens, <50ms. Confidence cap: ≤60%.
                   Max 1 source. Use: "do I know about this?"

    L1 Summary  — Vector search → Rerank → Compress.
                   ~100 tokens, <500ms. Confidence cap: ≤85%.
                   Max 2 sources. Use: "what does the knowledge say?"

    L2 Full     — Multi-source → CRAG verify → Web fallback → Associate.
                   ~500 tokens, <2s. No confidence cap.
                   Max 3 sources (Liu 2023: 3 precise > 5 mixed).
                   Use: "what should I do and why?"

Usage::

    from concinno.star import STAREngine, RetrievalTier

    engine = STAREngine(project_dir=".")

    # Auto-detect tier from complexity
    results = engine.retrieve("refactor hook system", tier=RetrievalTier.AUTO)

    # Force specific tier
    results = engine.retrieve("what file has deploy config?", tier=RetrievalTier.L0_INDEX)

    # Format for hook injection
    injection = engine.format_injection(results, max_tokens=500)

    # Multi-agent: share knowledge with sub-agents
    engine.share()  # Writes cache to shared dir
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from enum import Enum

# ── Core Data Structures ─────────────────────────────────


class RetrievalTier(str, Enum):
    """Three-tier retrieval depth."""

    AUTO = "auto"  # Let STAR decide based on query complexity
    L0_INDEX = "l0_index"  # Metadata only (~10 tokens, <50ms)
    L1_SUMMARY = "l1_summary"  # Reranked + compressed (~100 tokens, <500ms)
    L2_FULL = "l2_full"  # Multi-source + multi-hop (~500 tokens, <2s)


class SearchMode(str, Enum):
    """Convergent vs divergent search strategy.

    Convergent: focused query → precise answer. 3 results optimal.
    Divergent: exploration → comprehensive coverage. Multi-round Map-Reduce.
    """

    CONVERGENT = "convergent"  # Default: 3 precise results (Liu 2023)
    DIVERGENT = "divergent"  # Map-Reduce: scatter → gather → gap-check


class RetrievalProfile(str, Enum):
    """Three championship modes — one engine, three world-firsts.

    PRECISION: #1 in precision. Reject uncertain results. Cuconasu 2024
        strict noise control. Liu 2023 optimal 3-doc window. Best for:
        code generation, critical decisions, factual queries.

    RECALL: #1 in recall. Cast widest net. Relax noise guard, lower
        confidence bar, include web results aggressively. Best for:
        exploration, brainstorming, "find everything about X".

    BALANCED: #1 in cost-performance. Dynamic equilibrium between
        precision and recall. Adapts per-query: simple queries get
        precision treatment, complex queries get recall treatment.
        Best for: general-purpose, mixed workloads, production default.
    """

    PRECISION = "precision"  # 精準模式 — World #1 Precision
    RECALL = "recall"  # 召回模式 — World #1 Recall
    BALANCED = "balanced"  # 平衡模式 — World #1 CP Value


# Profile-specific overrides (merged onto TIER_CONFIG at runtime)
PROFILE_CONFIG = {
    RetrievalProfile.PRECISION: {
        "noise_ratio_max": 0.30,  # Cuconasu 2024 strict
        "confidence_accept": 0.70,  # High bar
        "confidence_caution": 0.40,
        "optimal_results": 3,  # Liu 2023 sweet spot
        "max_web": 3,
        "crag_on_incorrect": "reject",  # No uncertain answers
        "web_policy": "last_resort",  # Only when internal fails
        "position_aware": True,  # Liu 2023 primacy/recency
        "tier_source_scale": 1.0,  # Use TIER_CONFIG as-is
    },
    RetrievalProfile.RECALL: {
        "noise_ratio_max": 0.60,  # Relaxed for coverage
        "confidence_accept": 0.35,  # Low bar — include more
        "confidence_caution": 0.15,  # Almost nothing skipped
        "optimal_results": 10,  # Cast wide net
        "max_web": 8,  # Aggressive web search
        "crag_on_incorrect": "downgrade",  # Keep but lower score
        "web_policy": "always",  # Web on every L1+ query
        "position_aware": False,  # Score-sorted, no reorder
        "tier_source_scale": 2.5,  # 2.5× more sources per tier
    },
    RetrievalProfile.BALANCED: {
        "noise_ratio_max": 0.40,  # Middle path
        "confidence_accept": 0.50,  # Moderate bar
        "confidence_caution": 0.25,
        "optimal_results": 5,  # Practical sweet spot
        "max_web": 5,
        "crag_on_incorrect": "keep_low",  # Keep with penalty
        "web_policy": "supplement",  # Web when internal < 3
        "position_aware": True,  # Still optimize layout
        "tier_source_scale": 1.5,  # 1.5× sources per tier
    },
}


class SourceType(str, Enum):
    """Knowledge source types in the STAR ensemble."""

    KB_SKILL = "kb_skill"  # File-based KB Skill (.claude/skills/kb_*)
    RAG_VECTOR = "rag_vector"  # ChromaDB vector search (dense)
    BM25 = "bm25"  # BM25 sparse retrieval (term frequency)
    RIVERBED = "riverbed"  # RMT depth-based recall
    LEARNING = "learning"  # Correction/learning from knowledge.py
    WEB_SEARCH = "web_search"  # External knowledge (the ocean)


class CRAGAction(str, Enum):
    """CRAG corrective actions (Yan 2024)."""

    CORRECT = "correct"  # Retrieval is relevant, use directly
    AMBIGUOUS = "ambiguous"  # Partially relevant, decompose + re-retrieve
    INCORRECT = "incorrect"  # Irrelevant, try different source or skip


@dataclass
class SourceResult:
    """A single result from one knowledge source."""

    source: SourceType
    text: str
    score: float  # Source-specific relevance score (0-1)
    file: str = ""
    heading: str = ""
    depth: float = 0.0  # Riverbed depth (0 if not from RMT)
    emotional_charge: float = 0.0  # [-1,1] Riverbed emotional intensity (0 if not from RMT)
    timestamp: float = 0.0  # When this knowledge was created/updated (epoch)
    freshness: float = 1.0  # 0-1 temporal relevance (decays over time)
    metadata: dict = field(default_factory=dict)


@dataclass
class ConfidenceVerdict:
    """Confidence assessment across all sources."""

    score: float  # 0.0-1.0 calibrated
    action: str  # "accept" (≥0.7) / "use_with_caution" (0.4-0.7) / "skip" (<0.4)
    reason: str
    tier: RetrievalTier = RetrievalTier.AUTO


@dataclass
class RetrievalResult:
    """A single verified retrieval result."""

    question: str
    answer: str
    confidence: float  # 0.0-1.0
    tier: RetrievalTier = RetrievalTier.AUTO
    sources: list[SourceResult] = field(default_factory=list)
    reasoning: str = ""
    crag_action: CRAGAction = CRAGAction.CORRECT
    tokens_used: int = 0  # Estimated tokens consumed


# ── Constants ────────────────────────────────────────────

# ── Research-Backed Retrieval Limits ─────────────────────
# Liu 2023 (Lost in the Middle): 3 docs ~450 words = 85% accuracy (peak).
#   5 docs = 78%, 10 docs = 55%. Middle-positioned info drops 40%.
# Cuconasu 2024 (Noise-Robust RAG): Irrelevant docs reduce faithfulness 15%.
#   Noise ratio >30% = actively harmful. Padding ≈ no retrieval.
# Conclusion: Precision > Recall. 3 precise > 5 mixed. Always.
OPTIMAL_RESULTS_PER_QUERY = 3  # Sweet spot (Liu 2023)
MAX_WEB_RESULTS = 3  # Hard ceiling for external search
NOISE_RATIO_MAX = 0.30  # Web/unverified can't exceed 30% of total context
OPTIMAL_CONTEXT_WORDS_EN = 450  # ~3 docs × 150 words (peak accuracy zone)
SHARED_BUS_MAX_INJECT = 2  # Max shared results injected per query

# Tier configuration — budget-aware routing (CBUA C0)
# Each tier's max_sources is the HARD CEILING — not a target.
# Reranker runs BEFORE source limit to ensure only the best survive.
TIER_CONFIG = {
    RetrievalTier.L0_INDEX: {
        "max_tokens": 30,
        "max_sources": 1,
        "max_hops": 0,
        "confidence_cap": 0.60,
        "use_reranker": False,
        "use_compression": False,
        "use_crag": False,
        "use_web": False,
        "use_association": False,
    },
    RetrievalTier.L1_SUMMARY: {
        "max_tokens": 150,
        "max_sources": 2,
        "max_hops": 1,
        "confidence_cap": 0.85,
        "use_reranker": True,
        "use_compression": True,
        "use_crag": False,
        "use_web": False,
        "use_association": False,
    },
    RetrievalTier.L2_FULL: {
        "max_tokens": 500,
        "max_sources": 3,  # 3 precise > 5 mixed (Liu 2023)
        "max_hops": 2,
        "confidence_cap": 1.0,
        "use_reranker": True,
        "use_compression": True,
        "use_crag": True,
        "use_web": True,  # Web fallback (hard-limited to MAX_WEB_RESULTS)
        "use_association": True,  # Associative expansion (post-filter)
    },
}

# Source reliability weights (higher = more trusted)
SOURCE_WEIGHTS = {
    SourceType.KB_SKILL: 0.9,  # Human-curated, highest trust
    SourceType.LEARNING: 0.8,  # Battle-tested corrections
    SourceType.RIVERBED: 0.7,  # Depth-weighted recall
    SourceType.BM25: 0.65,  # BM25 sparse retrieval (term frequency, lexical match)
    SourceType.RAG_VECTOR: 0.6,  # Vector similarity (dense, semantic)
    SourceType.WEB_SEARCH: 0.4,  # External, unverified (the ocean)
}

CONFIDENCE_ACCEPT = 0.7
CONFIDENCE_CAUTION = 0.4

# Temporal freshness constants (Ebbinghaus + SM-2)
FRESHNESS_HALF_LIFE_DAYS = 7.0  # Code knowledge halves in relevance per week
FRESHNESS_MIN = 0.1  # Even old knowledge has 10% baseline relevance
ACCESS_REINFORCEMENT = 0.15  # Each re-access slows decay by 15%

# Associative index constants (Collins & Loftus 1975)
ASSOCIATION_DECAY = 0.95  # Co-occurrence strength decays per session
ASSOCIATION_MIN_STRENGTH = 0.1  # Prune below this
ASSOCIATION_MAX_EXPANSION = 3  # Max associated results to add

# Session cache constants
SESSION_CACHE_MAX = 200  # Max cached results per session

# Shared memory bus constants
SHARED_BUS_TTL_SECONDS = 1800  # 30 minutes

# Adaptive Forgetting constants (original: 4-strategy ranked eviction)
FORGET_STALE_DAYS = 30  # Forget if unused for 30 days
FORGET_LOW_CONFIDENCE = 0.25  # Forget below this confidence
FORGET_MAX_POOL_SIZE = 500  # Target pool size (triggers forgetting above this)

# Divergent search constants (Map-Reduce with precision constraint)
DIVERGENT_MAX_BRANCHES = 7  # Max parallel facets to explore
DIVERGENT_RESULTS_PER_BRANCH = OPTIMAL_RESULTS_PER_QUERY  # 3 per branch
DIVERGENT_GAP_CHECK_ROUNDS = 2  # Max gap-check iterations

# Stopwords stripped from KB keyword queries to prevent planner-prefix dilution
_QUERY_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "for", "in", "on", "of", "to", "is", "it",
    "with", "by", "at", "from", "as", "that", "this", "be", "are", "was",
    "do", "does", "did", "has", "have", "had", "not", "no", "but", "so",
    "if", "what", "where", "which", "who", "how", "when", "why",
})

# Query complexity detection patterns (for AUTO tier routing)
_SIMPLE_PATTERNS = re.compile(
    r"^(?:what|where|which|who)\s+(?:file|path|dir|config|setting)",
    re.IGNORECASE,
)
_COMPLEX_PATTERNS = re.compile(
    r"(?:refactor|rewrite|migrate|debug|why|how\s+should|architect|design)",
    re.IGNORECASE,
)
_DESTRUCTIVE_PATTERNS = re.compile(
    r"(?:rm\s+-rf|delete|drop|force|push|reset|destroy|remove|kill|wipe)",
    re.IGNORECASE,
)

# Category → preferred source mapping
CATEGORY_SOURCE_MAP = {
    "safety": [SourceType.KB_SKILL, SourceType.LEARNING],
    "convention": [SourceType.KB_SKILL, SourceType.BM25, SourceType.RAG_VECTOR],
    "history": [SourceType.RIVERBED, SourceType.LEARNING],
    "prerequisite": [SourceType.RAG_VECTOR, SourceType.BM25, SourceType.KB_SKILL],
    "blocker": [SourceType.LEARNING, SourceType.RIVERBED],
    "location": [SourceType.KB_SKILL, SourceType.BM25, SourceType.RAG_VECTOR],
    "external": [SourceType.WEB_SEARCH, SourceType.RAG_VECTOR],
}


# ── Temporal Freshness Scorer (Ebbinghaus + SM-2) ─────


class FreshnessScorer:
    """Ebbinghaus-inspired temporal decay with SM-2 reinforcement.

    Cognitive science basis:
    - Ebbinghaus (1885): Memory retention = e^(-t/S) where S = stability
    - SM-2 (Wozniak 1987): Repeated access increases stability
    - Hyperthymesia: Strong temporal-contextual binding = high retention

    AI adaptation:
    - Each knowledge item has a timestamp and access_count
    - Freshness decays exponentially with half-life
    - Each access reinforces (slows decay) — like SM-2 interval growth
    - Result: frequently-used knowledge stays fresh, unused fades
    """

    def __init__(
        self,
        half_life_days: float = FRESHNESS_HALF_LIFE_DAYS,
        min_freshness: float = FRESHNESS_MIN,
        reinforcement: float = ACCESS_REINFORCEMENT,
    ):
        self._half_life_hours = half_life_days * 24.0
        self._min = min_freshness
        self._reinforcement = reinforcement

    def score(
        self,
        timestamp: float,
        access_count: int = 0,
        now: float = 0.0,
    ) -> float:
        """Compute freshness score (0-1) for a knowledge item.

        Args:
            timestamp: When knowledge was created/updated (epoch seconds).
            access_count: How many times this knowledge has been retrieved.
            now: Current time (epoch seconds). 0 = use time.time().

        Returns:
            Freshness score in [min_freshness, 1.0].
        """
        try:
            timestamp = float(timestamp)
        except (TypeError, ValueError):
            return self._min
        if timestamp <= 0:
            return self._min  # No timestamp = assume old

        now = now or time.time()
        age_hours = max(0.0, (now - timestamp) / 3600.0)

        if age_hours <= 0:
            return 1.0

        # SM-2 inspired: each access increases effective half-life
        effective_half_life = self._half_life_hours * (
            1.0 + self._reinforcement * min(access_count, 20)
        )

        # Ebbinghaus decay: freshness = e^(-0.693 * age / half_life)
        decay = math.exp(-0.693 * age_hours / effective_half_life)

        return max(self._min, min(1.0, decay))

    def apply_to_results(
        self,
        candidates: list[SourceResult],
        now: float = 0.0,
    ) -> list[SourceResult]:
        """Apply freshness scoring to all candidates in-place.

        If a candidate has a file path, use file mtime as timestamp
        (more accurate than stored timestamp — code may not have changed).
        """
        now = now or time.time()
        for c in candidates:
            ts = c.timestamp
            # Prefer file mtime: old file that hasn't changed is still fresh
            if c.file:
                try:
                    mtime = os.path.getmtime(c.file)
                    if mtime > 0:
                        ts = mtime
                except OSError:
                    pass
            access_count = c.metadata.get("access_count", 0)
            c.freshness = self.score(ts, access_count, now)
        return candidates


# ── Associative Index (Collins & Loftus 1975) ────────────


class BM25Index:
    """BM25 sparse retrieval index for lexical matching.

    Complements dense vector search (RAG_VECTOR) — BM25 excels at exact term
    matching while dense vectors handle semantic similarity. Together = hybrid.

    Lazy init: builds index on first query from available KB files + learnings.
    """

    # English stopwords (most common, zero-dependency)
    _STOPWORDS = frozenset(
        "a an the is are was were be been being have has had do does "
        "did will would shall should may might can could of in to for "
        "with on at by from as into through during before after above "
        "below between out off over under again further then once here "
        "there when where why how all both each few more most other "
        "some such no nor not only own same so than too very and but "
        "or if while that this these those it its he she they them "
        "his her their what which who whom".split()
    )

    def __init__(self):
        self._index = None  # BM25Okapi instance
        self._corpus: list[dict] = []  # [{text, file, heading}]

    @classmethod
    def _tokenize(cls, text: str) -> list[str]:
        """Tokenize with lowercasing, punctuation removal, stopwords."""
        import re as _re

        # Remove punctuation, lowercase, split
        clean = _re.sub(r"[^\w\s]", " ", text.lower())
        return [
            w for w in clean.split()
            if w and w not in cls._STOPWORDS and len(w) > 1
        ]

    def build(self, documents: list[dict]) -> None:
        """Build BM25 index from documents [{text, file, heading}]."""
        try:
            from rank_bm25 import BM25Okapi
        except ImportError:
            return

        self._corpus = documents
        tokenized = [self._tokenize(doc["text"]) for doc in documents]
        if tokenized:
            self._index = BM25Okapi(tokenized)

    def query(self, text: str, top_k: int = 5) -> list[SourceResult]:
        """Query BM25 index, return SourceResults."""
        if not self._index or not self._corpus:
            return []

        tokenized_query = self._tokenize(text)
        if not tokenized_query:
            return []
        scores = self._index.get_scores(tokenized_query)

        # Pair with documents, sort by score
        scored = sorted(
            zip(self._corpus, scores),
            key=lambda x: x[1],
            reverse=True,
        )

        results = []
        for doc, score in scored[:top_k]:
            if score <= 0:
                break
            # Normalize BM25 score to 0-1 range
            norm_score = min(1.0, score / max(scores) if max(scores) > 0 else 0)
            results.append(SourceResult(
                source=SourceType.BM25,
                text=doc["text"][:500],
                score=round(norm_score, 4),
                file=doc.get("file", ""),
                heading=doc.get("heading", ""),
                timestamp=doc.get("timestamp", 0.0),
            ))
        return results

    @property
    def is_ready(self) -> bool:
        return self._index is not None


# ── Confluence RAG ──────────────────────────────────────


@dataclass
class ConfluencePath:
    """One independent retrieval path and its results."""

    query: str
    results: list[SourceResult] = field(default_factory=list)


@dataclass
class ConfluencePoint:
    """A convergence point where multiple paths meet.

    When N independent retrieval paths independently surface the same
    document/chunk, that convergence is evidence of a hidden multi-hop
    relationship — without ever building a graph.
    """

    file: str
    heading: str
    paths_hit: int  # How many independent paths found this
    total_paths: int
    confluence_score: float  # paths_hit / total_paths, weighted by source scores
    best_text: str
    source_scores: list[float] = field(default_factory=list)


class ConfluenceRAG:
    """Convergent multi-path retrieval — Graph RAG competitor.

    Instead of Graph RAG's approach (build graph → traverse from one point),
    Confluence RAG fires N independent retrieval paths from different angles
    and finds where they converge. Convergence = evidence of hidden multi-hop
    relationships WITHOUT graph construction cost or noise amplification.

    Why this beats Graph RAG for precision:
    - Graph RAG: one bad edge → noise cascades through traversal
    - Confluence: each path is independent, convergence filters noise
    - Graph RAG: O(V+E) construction cost, stale when docs change
    - Confluence: zero pre-computation, always fresh

    Patent claims:
    1. Multi-path independent retrieval with convergence detection
    2. Confluence scoring: paths_hit / total_paths × avg_score
    3. Implicit multi-hop discovery without explicit graph construction
    4. Noise cancellation via independent path agreement

    Usage::

        confluence = ConfluenceRAG()
        points = confluence.search(
            query="how does auth affect deploy?",
            retriever=my_retriever,
            tier=RetrievalTier.L2_FULL,
        )
        # points[0].confluence_score = 0.75 means 3/4 paths agreed
    """

    # Minimum paths that must converge to count as signal
    MIN_CONVERGENCE: int = 2

    def decompose(self, query: str) -> list[str]:
        """Decompose query into independent retrieval angles.

        Each angle probes a different facet of the question.
        Independence is key — correlated queries defeat the purpose.
        """
        words = query.lower().split()
        angles: list[str] = [query]  # Original query always included

        # Extract noun phrases / key concepts for independent probes
        # Heuristic: split on connectors to find independent facets
        connectors = {"and", "or", "but", "with", "when", "how", "does"}
        segments: list[list[str]] = [[]]
        for w in words:
            if w in connectors and len(segments[-1]) >= 2:
                segments.append([])
            else:
                segments[-1].append(w)

        for seg in segments:
            if len(seg) >= 2:
                probe = " ".join(seg)
                if probe != query.lower():
                    angles.append(probe)

        # If we still have < 3 angles, add keyword-focused probes
        meaningful = [
            w for w in words
            if w not in _QUERY_STOPWORDS and len(w) > 2
        ]
        if len(angles) < 3 and len(meaningful) >= 2:
            # Probe each keyword independently
            for kw in meaningful[:3]:
                angles.append(kw)

        # Deduplicate preserving order
        seen: set[str] = set()
        unique: list[str] = []
        for a in angles:
            key = a.strip().lower()
            if key not in seen:
                seen.add(key)
                unique.append(a)

        return unique[:5]  # Cap at 5 paths

    def find_convergence(
        self, paths: list[ConfluencePath]
    ) -> list[ConfluencePoint]:
        """Find documents where multiple paths converge.

        The core insight: if 3 out of 4 independent search paths
        all surface the same document, that document is almost
        certainly relevant — even if no single path gave it a
        high score. This is implicit multi-hop discovery.
        """
        # Build file → path hits mapping
        file_hits: dict[str, list[tuple[int, SourceResult]]] = {}
        for path_idx, path in enumerate(paths):
            for result in path.results:
                key = f"{result.file}::{result.heading}"
                if key not in file_hits:
                    file_hits[key] = []
                file_hits[key].append((path_idx, result))

        total_paths = len(paths)
        points: list[ConfluencePoint] = []

        for key, hits in file_hits.items():
            # Count unique paths (same path hitting twice doesn't count)
            unique_paths = len({pid for pid, _ in hits})
            if unique_paths < self.MIN_CONVERGENCE:
                continue

            # Best text from highest-scoring hit
            best_hit = max(hits, key=lambda h: h[1].score)
            scores = [h[1].score for h in hits]

            # Confluence score: convergence ratio × average quality
            convergence_ratio = unique_paths / total_paths
            avg_score = sum(scores) / len(scores)
            confluence_score = round(
                convergence_ratio * 0.6 + avg_score * 0.4, 4
            )

            points.append(ConfluencePoint(
                file=best_hit[1].file,
                heading=best_hit[1].heading,
                paths_hit=unique_paths,
                total_paths=total_paths,
                confluence_score=confluence_score,
                best_text=best_hit[1].text,
                source_scores=scores,
            ))

        points.sort(key=lambda p: p.confluence_score, reverse=True)
        return points

    def search(
        self,
        query: str,
        retriever: MultiSourceRetriever,
        tier: RetrievalTier = RetrievalTier.L2_FULL,
        max_per_path: int = 5,
    ) -> list[ConfluencePoint]:
        """Full Confluence RAG pipeline.

        1. Decompose query into independent angles
        2. Fire each angle as independent retrieval path
        3. Find convergence points
        4. Return sorted by confluence score
        """
        angles = self.decompose(query)
        if len(angles) < 2:
            return []  # Need ≥2 paths for convergence

        paths: list[ConfluencePath] = []
        for angle in angles:
            sq = SubQuestion(
                text=angle,
                category="concept",
                source_hint=None,
            )
            results = retriever.retrieve(sq, tier, max_per_path)
            paths.append(ConfluencePath(
                query=angle, results=results,
            ))

        return self.find_convergence(paths)


class AssociativeIndex:
    """Co-occurrence graph with spreading activation for related discovery.

    Cognitive science basis:
    - Collins & Loftus (1975): Spreading activation in semantic networks
    - Method of Loci: Spatial/contextual anchoring strengthens associations
    - Savant pattern: Extraordinary recall via dense pattern links

    AI adaptation:
    - Track which knowledge items co-occur in retrievals
    - When item A is retrieved, check for associated items B, C
    - Association strength decays over time (prune weak links)
    - Result: "I found X — and I recall Y is related to X"

    Implementation:
    - Adjacency dict: {key_a: {key_b: strength, key_c: strength}}
    - Key = source_type:file:heading (or text hash for headless items)
    - Strength in (0, 1], decays by ASSOCIATION_DECAY per session
    """

    def __init__(self):
        self._graph: dict[str, dict[str, float]] = {}

    def record_cooccurrence(self, results: list[SourceResult]) -> None:
        """Record that these results co-occurred in one retrieval."""
        keys = [self._key(r) for r in results if r.text]
        # All pairs strengthen each other
        for i, k1 in enumerate(keys):
            for k2 in keys[i + 1:]:
                self._strengthen(k1, k2)

    def expand(
        self,
        candidates: list[SourceResult],
        all_known: dict[str, SourceResult],
        max_expansion: int = ASSOCIATION_MAX_EXPANSION,
    ) -> list[SourceResult]:
        """Spreading activation: find associated results not already retrieved.

        Args:
            candidates: Current retrieval results.
            all_known: Cache of previously retrieved results (key → SourceResult).
            max_expansion: Max extra results to add.

        Returns:
            New associated results (not in candidates).
        """
        if not self._graph or not candidates:
            return []

        candidate_keys = {self._key(c) for c in candidates}
        # Collect associated keys with activation strength
        activations: dict[str, float] = {}

        for c in candidates:
            key = self._key(c)
            neighbors = self._graph.get(key, {})
            for neighbor_key, strength in neighbors.items():
                if neighbor_key not in candidate_keys:
                    # Spreading activation: candidate_score × link_strength
                    activation = c.score * strength
                    if neighbor_key in activations:
                        activations[neighbor_key] = max(
                            activations[neighbor_key], activation
                        )
                    else:
                        activations[neighbor_key] = activation

        if not activations:
            return []

        # Sort by activation strength, take top N
        sorted_keys = sorted(
            activations.items(), key=lambda x: x[1], reverse=True
        )[:max_expansion]

        # Resolve keys to SourceResults from cache
        expanded: list[SourceResult] = []
        for key, activation in sorted_keys:
            if key in all_known:
                result = all_known[key]
                # Adjust score to reflect association (not direct hit)
                result.score = round(min(result.score, activation * 0.8), 4)
                result.metadata["associated"] = True
                expanded.append(result)

        return expanded

    def decay_all(self, factor: float = ASSOCIATION_DECAY) -> int:
        """Decay all association strengths. Prune weak links. Returns pruned count."""
        pruned = 0
        keys_to_remove: list[str] = []

        for key, neighbors in self._graph.items():
            weak: list[str] = []
            for nkey in neighbors:
                neighbors[nkey] *= factor
                if neighbors[nkey] < ASSOCIATION_MIN_STRENGTH:
                    weak.append(nkey)
            for w in weak:
                del neighbors[w]
                pruned += 1
            if not neighbors:
                keys_to_remove.append(key)

        for k in keys_to_remove:
            del self._graph[k]

        return pruned

    def _strengthen(self, key_a: str, key_b: str, amount: float = 0.2) -> None:
        """Strengthen bidirectional association."""
        if key_a not in self._graph:
            self._graph[key_a] = {}
        if key_b not in self._graph:
            self._graph[key_b] = {}

        self._graph[key_a][key_b] = min(
            1.0, self._graph[key_a].get(key_b, 0.0) + amount
        )
        self._graph[key_b][key_a] = min(
            1.0, self._graph[key_b].get(key_a, 0.0) + amount
        )

    @staticmethod
    def _key(r: SourceResult) -> str:
        """Generate a stable key for a SourceResult."""
        if r.file:
            return f"{r.source.value}:{r.file}:{r.heading}"
        # Fallback: first 60 chars of text as key
        return f"{r.source.value}::{r.text[:60]}"

    def size(self) -> int:
        """Number of nodes in the association graph."""
        return len(self._graph)

    def to_dict(self) -> dict:
        """Serialize for persistence."""
        return dict(self._graph)

    def load_dict(self, data: dict) -> None:
        """Load from serialized dict."""
        self._graph = {k: dict(v) for k, v in data.items()}


# ── Session Cache ──────────────────────────


class SessionCache:
    """AI-native advantage: perfect recall within a session.

    Unlike human memory, AI doesn't forget within a conversation.
    Once retrieved, results are cached and instantly re-available.
    This eliminates redundant retrievals and guarantees consistency.

    The cache also serves as the 'all_known' pool for associative expansion.
    """

    def __init__(self, max_size: int = SESSION_CACHE_MAX):
        self._cache: dict[str, RetrievalResult] = {}
        self._source_cache: dict[str, SourceResult] = {}
        self._max = max_size
        self._hits = 0
        self._misses = 0

    def get(self, query: str, tier: RetrievalTier) -> list[RetrievalResult] | None:
        """Check cache for exact query+tier match."""
        key = f"{tier.value}:{query.lower().strip()}"
        if key in self._cache:
            self._hits += 1
            return [self._cache[key]]
        self._misses += 1
        return None

    def put(self, result: RetrievalResult) -> None:
        """Cache a retrieval result."""
        if len(self._cache) >= self._max:
            # Evict oldest (FIFO)
            oldest_key = next(iter(self._cache))
            del self._cache[oldest_key]

        key = f"{result.tier.value}:{result.question.lower().strip()}"
        self._cache[key] = result

        # Also cache individual source results for associative expansion
        for sr in result.sources:
            sr_key = AssociativeIndex._key(sr)
            self._source_cache[sr_key] = sr

    def get_source_cache(self) -> dict[str, SourceResult]:
        """Get all cached source results (for associative expansion)."""
        return self._source_cache

    def stats(self) -> dict:
        """Cache hit/miss statistics."""
        total = self._hits + self._misses
        return {
            "cached": len(self._cache),
            "source_cached": len(self._source_cache),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total, 3) if total > 0 else 0.0,
        }

    def clear(self) -> None:
        """Clear all cached results."""
        self._cache.clear()
        self._source_cache.clear()
        self._hits = 0
        self._misses = 0


# ── Adaptive Forgetter (original: Adaptive Forgetting) ──


@dataclass
class ForgetCandidate:
    """A knowledge item being evaluated for forgetting."""

    key: str
    forget_score: float  # Higher = more forgettable (0-1)
    reason: str  # Why this should be forgotten


class AdaptiveForgetter:
    """Yin-yang balance: remember AND forget for optimal retrieval.

    Cognitive science basis:
    - Lost in the Middle (Liu 2023): Too much context HURTS LLM performance
    - Noise-Robust RAG (Cuconasu 2024): Irrelevant docs reduce faithfulness ~15%
    - Ebbinghaus: Natural forgetting is a feature, not a bug
    - Equilibrium: Balance of forgetting and remembering

    AI adaptation:
    - Knowledge pool has an optimal size (5-10 results per query)
    - Beyond optimal, noise increases and accuracy drops
    - Forgetting strategy ranked by CP value:
      1. CRAG-rejected (highest CP: already proven irrelevant)
      2. Stale + unused (high CP: age × no access = dead weight)
      3. Superseded (high CP: newer version exists)
      4. Low confidence (medium CP: never trusted anyway)

    Integration: Called during SessionCache eviction and RAG index maintenance.
    """

    def __init__(
        self,
        stale_days: float = FORGET_STALE_DAYS,
        low_confidence: float = FORGET_LOW_CONFIDENCE,
        max_pool: int = FORGET_MAX_POOL_SIZE,
    ):
        self._stale_threshold = stale_days * 86400  # Convert to seconds
        self._low_confidence = low_confidence
        self._max_pool = max_pool
        self._rejected_keys: set[str] = set()  # Keys CRAG rejected
        self._superseded: dict[str, str] = {}  # old_key → new_key

    def mark_rejected(self, key: str) -> None:
        """Mark a key as CRAG-rejected (highest forget priority)."""
        self._rejected_keys.add(key)

    def mark_superseded(self, old_key: str, new_key: str) -> None:
        """Mark old_key as superseded by new_key."""
        self._superseded[old_key] = new_key

    def evaluate(
        self,
        items: list[SourceResult],
        now: float = 0.0,
    ) -> list[ForgetCandidate]:
        """Evaluate which items should be forgotten, ranked by CP.

        Returns ForgetCandidates sorted by forget_score (most forgettable first).
        """
        now = now or time.time()
        candidates: list[ForgetCandidate] = []

        for item in items:
            key = AssociativeIndex._key(item)
            score, reason = self._score_item(item, key, now)
            if score > 0:
                candidates.append(ForgetCandidate(
                    key=key, forget_score=score, reason=reason,
                ))

        candidates.sort(key=lambda c: c.forget_score, reverse=True)
        return candidates

    def select_to_forget(
        self,
        items: list[SourceResult],
        target_size: int = 0,
        now: float = 0.0,
    ) -> list[str]:
        """Select keys to forget to reach target pool size.

        Args:
            items: Current knowledge pool.
            target_size: Desired pool size (0 = use self._max_pool).
            now: Current epoch time.

        Returns:
            List of keys to remove, ordered by forget priority.
        """
        target = target_size or self._max_pool
        excess = len(items) - target
        if excess <= 0:
            return []

        candidates = self.evaluate(items, now)
        return [c.key for c in candidates[:excess]]

    def _score_item(
        self,
        item: SourceResult,
        key: str,
        now: float,
    ) -> tuple[float, str]:
        """Score a single item's forgettability (0 = keep, 1 = forget).

        Priority order (by CP value):
        1. CRAG-rejected: 0.95 (proven irrelevant)
        2. Superseded: 0.85 (replaced by newer)
        3. Stale + unused: age_factor × 0.7
        4. Low confidence: 0.5
        5. Protected (KB_SKILL with high score): 0.0 (never forget)
        """
        # Protected: high-quality curated knowledge
        if item.source == SourceType.KB_SKILL and item.score >= 0.7:
            return 0.0, "protected:curated"

        # 1. CRAG-rejected (highest CP)
        if key in self._rejected_keys:
            return 0.95, "crag_rejected"

        # 2. Superseded by newer version
        if key in self._superseded:
            return 0.85, f"superseded_by:{self._superseded[key][:30]}"

        # 3. Stale + unused
        age = now - item.timestamp if item.timestamp > 0 else self._stale_threshold
        access_count = item.metadata.get("access_count", 0)
        if age > self._stale_threshold and access_count == 0:
            age_factor = min(1.0, age / (self._stale_threshold * 3))
            return round(0.7 * age_factor, 3), "stale_unused"

        # 4. Low confidence
        if item.score < self._low_confidence:
            return 0.5, "low_confidence"

        # 5. Moderate staleness (with some access)
        if age > self._stale_threshold:
            decay = min(0.4, 0.1 * (age / self._stale_threshold))
            return round(decay, 3), "aging"

        return 0.0, "keep"

    def stats(self) -> dict:
        """Forgetting statistics."""
        return {
            "rejected_keys": len(self._rejected_keys),
            "superseded_keys": len(self._superseded),
            "max_pool": self._max_pool,
            "stale_days": self._stale_threshold / 86400,
        }


# ── Multi-Agent Knowledge Bus ─────────────


class SharedMemoryBus:
    """File-based knowledge sharing between parent and sub-agents.

    Problem: Sub-agents start with blank context. Only the parent has
    accumulated knowledge, making sub-agent task quality worse.

    Solution: Parent writes retrieval cache to a shared JSONL file.
    Sub-agents read from the same file. Session-scoped with TTL.

    Design: Append-friendly JSONL format (no locking needed).
    Each line: {"query": ..., "answer": ..., "confidence": ..., "ts": ...}
    """

    def __init__(self, cache_dir: str = "", session_id: str = ""):
        self._cache_dir = cache_dir
        self._session_id = session_id or os.environ.get("CC_SESSION_ID", "default")
        self._shared_dir = os.path.join(cache_dir, "shared") if cache_dir else ""

    def publish(self, results: list[RetrievalResult]) -> int:
        """Write retrieval results to shared bus. Returns count written."""
        if not self._shared_dir:
            return 0

        os.makedirs(self._shared_dir, exist_ok=True)
        path = os.path.join(self._shared_dir, f"star_{self._session_id}.jsonl")

        written = 0
        try:
            with open(path, "a", encoding="utf-8") as f:
                for r in results:
                    if r.confidence < CONFIDENCE_CAUTION:
                        continue  # Don't share low-confidence results
                    entry = {
                        "query": r.question,
                        "answer": r.answer,
                        "confidence": r.confidence,
                        "tier": r.tier.value,
                        "ts": time.time(),
                        "session": self._session_id,
                        "sources": [s.source.value for s in r.sources],
                    }
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    written += 1
        except Exception:
            pass

        return written

    def consume(
        self, query: str = "", max_age: float = SHARED_BUS_TTL_SECONDS,
    ) -> list[SourceResult]:
        """Read shared knowledge from all sessions. Returns as SourceResults."""
        if not self._shared_dir or not os.path.isdir(self._shared_dir):
            return []

        now = time.time()
        results: list[SourceResult] = []
        q_words = set(query.lower().split()) if query else set()

        try:
            for filename in os.listdir(self._shared_dir):
                if not filename.startswith("star_") or not filename.endswith(".jsonl"):
                    continue
                filepath = os.path.join(self._shared_dir, filename)
                results.extend(
                    self._parse_shared_file(filepath, now, max_age, q_words)
                )
        except Exception:
            pass

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:OPTIMAL_RESULTS_PER_QUERY]  # Precision-first cap

    def _parse_shared_file(
        self,
        filepath: str,
        now: float,
        max_age: float,
        q_words: set[str],
    ) -> list[SourceResult]:
        """Parse a single shared JSONL file into SourceResults."""
        results: list[SourceResult] = []
        try:
            with open(filepath, encoding="utf-8") as f:
                for raw_line in f:
                    entry = self._parse_shared_line(raw_line, now, max_age, q_words)
                    if entry:
                        results.append(entry)
        except Exception:
            pass
        return results

    @staticmethod
    def _parse_shared_line(
        raw_line: str,
        now: float,
        max_age: float,
        q_words: set[str],
    ) -> SourceResult | None:
        """Parse a single JSONL line into a SourceResult or None."""
        line = raw_line.strip()
        if not line:
            return None
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            return None

        ts = entry.get("ts", 0)
        if now - ts > max_age:
            return None

        if q_words:
            a_words = set(entry.get("answer", "").lower().split())
            if not q_words & a_words:
                return None

        return SourceResult(
            source=SourceType.KB_SKILL,
            text=entry.get("answer", ""),
            score=entry.get("confidence", 0.5) * 0.8,
            timestamp=ts,
            freshness=1.0,
            metadata={
                "shared": True,
                "from_session": entry.get("session", ""),
            },
        )

    def cleanup(self, max_age: float = SHARED_BUS_TTL_SECONDS) -> int:
        """Remove expired shared files. Returns count removed."""
        if not self._shared_dir or not os.path.isdir(self._shared_dir):
            return 0

        now = time.time()
        removed = 0
        try:
            for filename in os.listdir(self._shared_dir):
                filepath = os.path.join(self._shared_dir, filename)
                try:
                    mtime = os.path.getmtime(filepath)
                    if now - mtime > max_age:
                        os.remove(filepath)
                        removed += 1
                except Exception:
                    continue
        except Exception:
            pass
        return removed


# ── Adaptive Router (CBUA C0) ─────────────


class AdaptiveRouter:
    """Routes queries to the appropriate retrieval tier.

    Based on Adaptive RAG (Jeong 2024) but extended with:
    - Three tiers instead of binary
    - Confidence caps per tier (CBUA integration)
    - Destructive operation detection → force L2
    """

    def route(self, query: str, forced_tier: RetrievalTier = RetrievalTier.AUTO) -> RetrievalTier:
        """Determine retrieval tier for a query."""
        if forced_tier != RetrievalTier.AUTO:
            return forced_tier

        # Destructive operations always get full retrieval
        if _DESTRUCTIVE_PATTERNS.search(query):
            return RetrievalTier.L2_FULL

        # Simple location/lookup queries — but cross-check for complexity words
        # Prevents "what file has the complex OAuth2 PKCE architecture" → L0
        if _SIMPLE_PATTERNS.search(query) and len(query) < 80:
            if not _COMPLEX_PATTERNS.search(query) and len(query.split()) <= 5:
                return RetrievalTier.L0_INDEX

        # Complex tasks needing deep context
        if _COMPLEX_PATTERNS.search(query):
            return RetrievalTier.L2_FULL

        # Default: summary tier (best balance)
        return RetrievalTier.L1_SUMMARY


# ── Query Planner ────────────────────────────────────────


@dataclass
class SubQuestion:
    """A decomposed knowledge sub-question."""

    text: str
    category: str  # safety, convention, history, prerequisite, blocker, location, external
    priority: str = "normal"  # critical, high, normal, low
    source_hint: SourceType | None = None


class QueryPlanner:
    """Decomposes task intent into structured sub-questions.

    L0: No decomposition (just the query itself).
    L1: 1-2 focused sub-questions.
    L2: Full decomposition with safety/history/prereq/blocker + external fallback.
    """

    def plan(self, query: str, tier: RetrievalTier) -> list[SubQuestion]:
        """Generate sub-questions appropriate for the tier."""
        if tier == RetrievalTier.L0_INDEX:
            return [SubQuestion(text=query, category="location", priority="normal")]

        questions: list[SubQuestion] = []

        # Safety check for destructive operations
        if _DESTRUCTIVE_PATTERNS.search(query):
            questions.append(SubQuestion(
                text=f"Safety concerns for: {query[:80]}",
                category="safety",
                priority="critical",
                source_hint=SourceType.KB_SKILL,
            ))

        # L1: convention check only
        questions.append(SubQuestion(
            text=f"Conventions and patterns for: {query[:80]}",
            category="convention",
            priority="normal",
            source_hint=SourceType.KB_SKILL,
        ))

        if tier == RetrievalTier.L2_FULL:
            # L2: add history, prerequisites, blockers
            if _COMPLEX_PATTERNS.search(query):
                questions.append(SubQuestion(
                    text=f"Past issues or lessons: {query[:80]}",
                    category="history",
                    priority="high",
                    source_hint=SourceType.RIVERBED,
                ))
            questions.append(SubQuestion(
                text=f"Prerequisites and dependencies: {query[:80]}",
                category="prerequisite",
                priority="high",
                source_hint=SourceType.RAG_VECTOR,
            ))
            questions.append(SubQuestion(
                text=f"Known blockers or pitfalls: {query[:80]}",
                category="blocker",
                priority="high",
                source_hint=SourceType.LEARNING,
            ))

        return questions


# ── Multi-Source Retriever ────────────────


class MultiSourceRetriever:
    """Queries KB Skills + RAG vector + Riverbed + learnings + web."""

    def __init__(
        self,
        project_dir: str = "",
        rag_index=None,
        riverbed_mem=None,
        learnings_path: str = "",
        kb_skills_dir: str = "",
        web_search_fn=None,
        shared_bus: SharedMemoryBus | None = None,
        bm25_index: BM25Index | None = None,
    ):
        self.project_dir = project_dir or os.environ.get("CLAUDE_PROJECT_DIR", ".")
        self._rag = rag_index
        self._riverbed = riverbed_mem
        self._learnings_path = learnings_path
        self._kb_skills_dir = kb_skills_dir or os.path.join(
            self.project_dir, ".claude", "skills"
        )
        self._web_search_fn = web_search_fn  # Pluggable: fn(query) -> list[dict]
        self._shared_bus = shared_bus
        self._bm25 = bm25_index

    def retrieve(
        self,
        question: SubQuestion,
        tier: RetrievalTier,
        max_per_source: int = 3,
    ) -> list[SourceResult]:
        """Retrieve from sources appropriate for the tier."""
        config = TIER_CONFIG[tier]
        results: list[SourceResult] = []

        # Determine sources based on category + tier limits
        preferred = CATEGORY_SOURCE_MAP.get(question.category, list(SourceType))
        if question.source_hint and question.source_hint not in preferred:
            preferred = [question.source_hint] + preferred

        # Limit source count by tier
        sources_to_query = preferred[:config["max_sources"]]

        for source_type in sources_to_query:
            try:
                source_results = self._query_source(
                    source_type, question.text, max_per_source, tier
                )
                results.extend(source_results)
            except Exception:
                continue

        # Also check shared bus if available (multi-agent knowledge)
        if self._shared_bus:
            try:
                shared = self._shared_bus.consume(question.text)
                results.extend(shared[:SHARED_BUS_MAX_INJECT])
            except Exception:
                pass

        results.sort(key=lambda r: r.score, reverse=True)
        return results

    def _query_source(
        self,
        source_type: SourceType,
        query: str,
        max_results: int,
        tier: RetrievalTier,
    ) -> list[SourceResult]:
        """Query a single knowledge source."""
        if source_type == SourceType.RAG_VECTOR:
            return self._query_rag(query, max_results, tier)
        elif source_type == SourceType.RIVERBED:
            return self._query_riverbed(query, max_results, tier)
        elif source_type == SourceType.KB_SKILL:
            return self._query_kb_skills(query, max_results, tier)
        elif source_type == SourceType.LEARNING:
            return self._query_learnings(query, max_results)
        elif source_type == SourceType.BM25:
            return self._query_bm25(query, max_results, tier)
        elif source_type == SourceType.WEB_SEARCH:
            return self._query_web(query, max_results)
        return []

    def _query_rag(
        self, query: str, max_results: int, tier: RetrievalTier
    ) -> list[SourceResult]:
        """Vector similarity search via RAGIndex."""
        if not self._rag:
            return []
        try:
            hits = self._rag.search(query, top_k=max_results, min_score=0.3)
            # L0: only return metadata (file + heading), no text
            if tier == RetrievalTier.L0_INDEX:
                return [
                    SourceResult(
                        source=SourceType.RAG_VECTOR,
                        text=f"{h.get('file', '')} § {h.get('heading', '')}",
                        score=h["score"],
                        file=h.get("file", ""),
                        heading=h.get("heading", ""),
                        timestamp=h.get("timestamp", 0.0),
                    )
                    for h in hits
                ]
            return [
                SourceResult(
                    source=SourceType.RAG_VECTOR,
                    text=h["text"][:500],
                    score=h["score"],
                    file=h.get("file", ""),
                    heading=h.get("heading", ""),
                    timestamp=h.get("timestamp", 0.0),
                )
                for h in hits
            ]
        except Exception:
            return []

    def _query_bm25(
        self, query: str, max_results: int, tier: RetrievalTier
    ) -> list[SourceResult]:
        """BM25 sparse retrieval — lexical term matching.

        Complements dense vector search: BM25 wins on exact keyword matches,
        dense wins on semantic similarity. Together = hybrid retrieval.
        """
        if not self._bm25 or not self._bm25.is_ready:
            return []

        # Strip planner prefix for cleaner keyword matching
        clean_query = query
        if ": " in clean_query:
            clean_query = clean_query.split(": ", 1)[1]

        results = self._bm25.query(clean_query, top_k=max_results)

        # L0: truncate text to metadata only
        if tier == RetrievalTier.L0_INDEX:
            for r in results:
                r.text = f"{r.file} § {r.heading}"

        return results

    def _query_riverbed(
        self, query: str, max_results: int, tier: RetrievalTier
    ) -> list[SourceResult]:
        """Depth-based recall via RiverbedMemory."""
        if not self._riverbed:
            return []
        try:
            config = TIER_CONFIG[tier]
            recalls = self._riverbed.recall(
                query,
                top_k=max_results,
                min_depth=0.05,
                max_hops=config["max_hops"],
            )
            if not recalls:
                return []
            max_priority = max(r.priority for r in recalls) or 1.0
            if tier == RetrievalTier.L0_INDEX:
                return [
                    SourceResult(
                        source=SourceType.RIVERBED,
                        text=r.text[:80],
                        score=min(1.0, r.priority / max_priority),
                        depth=r.depth,
                        emotional_charge=r.emotional_charge,
                        timestamp=getattr(r, "last_flow", 0.0),
                    )
                    for r in recalls
                ]
            return [
                SourceResult(
                    source=SourceType.RIVERBED,
                    text=r.text[:500],
                    score=min(1.0, r.priority / max_priority),
                    depth=r.depth,
                    emotional_charge=r.emotional_charge,
                    timestamp=getattr(r, "last_flow", 0.0),
                    metadata=r.metadata,
                )
                for r in recalls
            ]
        except Exception:
            return []

    def _query_kb_skills(
        self, query: str, max_results: int, tier: RetrievalTier
    ) -> list[SourceResult]:
        """Search KB Skill files by keyword matching."""
        if not os.path.isdir(self._kb_skills_dir):
            return []

        results: list[SourceResult] = []
        # Strip planner prefix (e.g. "Conventions and patterns for: X" → "X")
        clean_query = query
        if ": " in clean_query:
            clean_query = clean_query.split(": ", 1)[1]
        raw_words = re.sub(r"[^\w\s]", " ", clean_query.lower()).split()
        query_words = {w for w in raw_words if w not in _QUERY_STOPWORDS and len(w) > 1}

        try:
            for entry in os.listdir(self._kb_skills_dir):
                skill_dir = os.path.join(self._kb_skills_dir, entry)
                if not os.path.isdir(skill_dir) or not entry.startswith("kb_"):
                    continue
                skill_file = os.path.join(skill_dir, "SKILL.md")
                if not os.path.isfile(skill_file):
                    continue

                # L0: only check filename match, don't read content
                if tier == RetrievalTier.L0_INDEX:
                    name_words = set(entry.lower().replace("-", " ").replace("_", " ").split())
                    overlap = len(query_words & name_words)
                    if overlap > 0:
                        rel = os.path.relpath(skill_file, self.project_dir)
                        mtime = os.path.getmtime(skill_file)
                        results.append(SourceResult(
                            source=SourceType.KB_SKILL,
                            text=f"KB: {entry}",
                            score=min(1.0, overlap / max(len(query_words), 1)),
                            file=rel,
                            heading=entry,
                            timestamp=mtime,
                        ))
                    continue

                # L1+: read and search content
                try:
                    with open(skill_file, encoding="utf-8") as f:
                        content = f.read(8000)
                    mtime = os.path.getmtime(skill_file)
                except Exception:
                    continue

                content_lower = content.lower()
                matches = sum(1 for w in query_words if w in content_lower)
                if matches == 0:
                    continue

                score = min(1.0, (matches / max(len(query_words), 1)) * 1.2)
                best_para = _find_best_paragraph(content, query_words)
                rel = os.path.relpath(skill_file, self.project_dir)

                results.append(SourceResult(
                    source=SourceType.KB_SKILL,
                    text=best_para[:500],
                    score=score,
                    file=rel,
                    heading=entry,
                    timestamp=mtime,
                ))

                if len(results) >= max_results:
                    break
        except Exception:
            pass

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:max_results]

    def _query_learnings(self, query: str, max_results: int) -> list[SourceResult]:
        """Search correction learnings by keyword."""
        if not self._learnings_path or not os.path.isfile(self._learnings_path):
            return []

        try:
            with open(self._learnings_path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return []

        items = data.get("learnings", [])
        if not items:
            return []

        query_lower = query.lower()
        query_words = set(query_lower.split())
        results: list[SourceResult] = []

        for item in items:
            text = item.get("correction_text", "")
            context = item.get("context", "")
            combined = (text + " " + context).lower()

            matches = sum(1 for w in query_words if w in combined)
            if matches == 0:
                continue

            count = item.get("count", 1)
            score = min(1.0, (matches / max(len(query_words), 1)) * math.log(1 + count))

            results.append(SourceResult(
                source=SourceType.LEARNING,
                text=f"[{count}x] {text[:200]}",
                score=score,
                timestamp=item.get("last_seen", 0.0),
                metadata={"pattern_key": item.get("pattern_key", ""), "count": count},
            ))

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:max_results]

    def _query_web(self, query: str, max_results: int) -> list[SourceResult]:
        """External web search — the ocean beyond the fish pond.

        Hard-limited to MAX_WEB_RESULTS (Liu 2023: more docs ≠ better).
        Pluggable: set web_search_fn to a callable that takes query string
        and returns list[dict] with keys: text, url, title, score.

        Default: returns empty (web search requires explicit opt-in).
        """
        if not self._web_search_fn:
            return []

        # Hard ceiling: never exceed MAX_WEB_RESULTS regardless of caller
        effective_max = min(max_results, MAX_WEB_RESULTS)

        try:
            hits = self._web_search_fn(query)
            if not hits:
                return []

            results: list[SourceResult] = []
            for h in hits[:effective_max]:
                results.append(SourceResult(
                    source=SourceType.WEB_SEARCH,
                    text=h.get("text", "")[:500],
                    score=float(h.get("score", 0.5)),
                    file=h.get("url", ""),
                    heading=h.get("title", ""),
                    timestamp=time.time(),  # Web results are always "now"
                    freshness=1.0,
                    metadata={"url": h.get("url", "")},
                ))
            return results
        except Exception:
            return []


# ── Reranker (pluggable backend) ─────────


class Reranker:
    """Rerank candidates by relevance to query.

    Layered approach:
    1. If cross-encoder available (sentence-transformers) → semantic rerank
    2. Else → keyword-overlap fallback (Jaccard × original score)

    Cross-encoder auto-loads on first use (lazy init, ~80MB model).
    Model: cross-encoder/ms-marco-MiniLM-L-6-v2 (best speed/accuracy tradeoff).
    Disable with: Reranker(use_cross_encoder=False)
    """

    # Class-level model cache (shared across instances, load once)
    _shared_model = None
    _model_load_attempted = False

    def __init__(self, use_cross_encoder: bool = True):
        self._use_cross_encoder = use_cross_encoder
        self._backend = None  # Instance-level override via set_backend()

    def rerank(
        self,
        query: str,
        candidates: list[SourceResult],
        top_k: int = 5,
    ) -> list[SourceResult]:
        """Rerank candidates by relevance to query."""
        if not candidates:
            return []

        # Priority: explicit backend > auto cross-encoder > keyword fallback
        backend = self._backend or self._get_cross_encoder()
        if backend:
            return self._rerank_model_with(backend, query, candidates, top_k)

        return self._rerank_keyword(query, candidates, top_k)

    def _get_cross_encoder(self):
        """Lazy-load cross-encoder model (once per process)."""
        if not self._use_cross_encoder:
            return None
        if Reranker._model_load_attempted:
            return Reranker._shared_model
        Reranker._model_load_attempted = True
        try:
            from sentence_transformers import CrossEncoder

            Reranker._shared_model = CrossEncoder(
                "cross-encoder/ms-marco-MiniLM-L-6-v2",
                max_length=512,
            )
        except Exception:
            Reranker._shared_model = None
        return Reranker._shared_model

    def _rerank_keyword(
        self,
        query: str,
        candidates: list[SourceResult],
        top_k: int,
    ) -> list[SourceResult]:
        """Keyword-overlap reranker (zero-dep fallback)."""
        query_words = set(query.lower().split())
        if not query_words:
            return candidates[:top_k]

        scored: list[tuple[SourceResult, float]] = []
        for c in candidates:
            text_words = set(c.text.lower().split())
            # Jaccard-ish overlap weighted by original score
            overlap = len(query_words & text_words) / max(len(query_words | text_words), 1)
            combined = 0.4 * overlap + 0.6 * c.score  # Blend
            scored.append((c, combined))

        scored.sort(key=lambda x: x[1], reverse=True)
        # Update scores
        for c, new_score in scored[:top_k]:
            c.score = round(new_score, 4)
        return [c for c, _ in scored[:top_k]]

    def _rerank_model(
        self,
        query: str,
        candidates: list[SourceResult],
        top_k: int,
    ) -> list[SourceResult]:
        """Cross-encoder reranker via set_backend()."""
        return self._rerank_model_with(self._backend, query, candidates, top_k)

    def _rerank_model_with(
        self,
        model,
        query: str,
        candidates: list[SourceResult],
        top_k: int,
    ) -> list[SourceResult]:
        """Cross-encoder reranker (sentence-transformers or compatible)."""
        try:
            pairs = [(query, c.text[:512]) for c in candidates]
            scores = model.predict(pairs)
            for c, s in zip(candidates, scores):
                c.score = float(s)
            candidates.sort(key=lambda c: c.score, reverse=True)
            return candidates[:top_k]
        except Exception:
            return self._rerank_keyword(query, candidates, top_k)

    def set_backend(self, model):
        """Set a cross-encoder model for reranking."""
        self._backend = model


# ── Context Compressor (pluggable backend) ──


class ContextCompressor:
    """Compress retrieved context to minimize token usage.

    Default: extractive compression (keep highest-relevance sentences).
    Pluggable: LLMLingua-2 via set_backend().
    """

    def __init__(self):
        self._backend = None

    def compress(
        self,
        candidates: list[SourceResult],
        max_tokens: int = 150,
        query: str = "",
    ) -> list[SourceResult]:
        """Compress candidate texts to fit within token budget."""
        if not candidates:
            return []

        if self._backend:
            return self._compress_model(candidates, max_tokens, query)

        return self._compress_extractive(candidates, max_tokens)

    def _compress_extractive(
        self,
        candidates: list[SourceResult],
        max_tokens: int,
    ) -> list[SourceResult]:
        """Extractive compression: truncate to budget, keep best sentences."""
        char_budget = max_tokens * 4  # ~4 chars per token
        used = 0
        result: list[SourceResult] = []

        for c in candidates:
            remaining = char_budget - used
            if remaining <= 20:
                break

            if len(c.text) <= remaining:
                result.append(c)
                used += len(c.text)
            else:
                # Truncate at sentence boundary
                truncated = c.text[:remaining]
                last_period = max(
                    truncated.rfind(". "),
                    truncated.rfind("。"),
                    truncated.rfind("\n"),
                )
                if last_period > remaining // 3:
                    truncated = truncated[:last_period + 1]
                compressed = SourceResult(
                    source=c.source,
                    text=truncated,
                    score=c.score,
                    file=c.file,
                    heading=c.heading,
                    depth=c.depth,
                    timestamp=c.timestamp,
                    freshness=c.freshness,
                    metadata=c.metadata,
                )
                result.append(compressed)
                used += len(truncated)

        return result

    def _compress_model(
        self,
        candidates: list[SourceResult],
        max_tokens: int,
        query: str,
    ) -> list[SourceResult]:
        """LLMLingua-2 compression (requires llmlingua)."""
        try:
            combined = "\n---\n".join(c.text for c in candidates)
            compressed_text = self._backend.compress_prompt(
                [combined],
                instruction=query,
                target_token=max_tokens,
            )["compressed_prompt"]

            # Return as single result from best source
            best_source = candidates[0].source if candidates else SourceType.RAG_VECTOR
            return [SourceResult(
                source=best_source,
                text=compressed_text,
                score=candidates[0].score if candidates else 0.5,
                timestamp=candidates[0].timestamp if candidates else 0.0,
                freshness=candidates[0].freshness if candidates else 1.0,
            )]
        except Exception:
            return self._compress_extractive(candidates, max_tokens)

    def set_backend(self, model):
        """Set a LLMLingua model for compression."""
        self._backend = model


# ── CRAG Self-Corrector (Yan 2024) ───────


class CRAGCorrector:
    """Corrective RAG: evaluate retrieval relevance and self-correct.

    v3: INCORRECT now triggers web fallback before giving up (Innovation #10).
    """

    def __init__(self):
        self._evaluator = None

    def evaluate(
        self,
        query: str,
        candidates: list[SourceResult],
    ) -> CRAGAction:
        """Evaluate retrieval quality and decide corrective action."""
        if not candidates:
            return CRAGAction.INCORRECT

        if self._evaluator:
            return self._evaluate_model(query, candidates)

        return self._evaluate_heuristic(query, candidates)

    def _evaluate_heuristic(
        self,
        query: str,
        candidates: list[SourceResult],
    ) -> CRAGAction:
        """Heuristic CRAG evaluation based on scores and overlap."""
        if not candidates:
            return CRAGAction.INCORRECT

        avg_score = sum(c.score for c in candidates) / len(candidates)
        max_score = max(c.score for c in candidates)

        # Check keyword overlap between query and top result
        query_words = set(query.lower().split())
        top_words = set(candidates[0].text.lower().split())
        overlap = len(query_words & top_words) / max(len(query_words), 1)

        if max_score >= 0.7 and overlap >= 0.3:
            return CRAGAction.CORRECT
        elif avg_score >= 0.4 or overlap >= 0.2:
            return CRAGAction.AMBIGUOUS
        else:
            return CRAGAction.INCORRECT

    def _evaluate_model(
        self,
        query: str,
        candidates: list[SourceResult],
    ) -> CRAGAction:
        """Model-based CRAG evaluation."""
        try:
            relevance = self._evaluator(query, candidates[0].text)
            if relevance > 0.7:
                return CRAGAction.CORRECT
            elif relevance > 0.3:
                return CRAGAction.AMBIGUOUS
            return CRAGAction.INCORRECT
        except Exception:
            return self._evaluate_heuristic(query, candidates)

    def set_evaluator(self, evaluator_fn):
        """Set a model-based relevance evaluator function."""
        self._evaluator = evaluator_fn


# ── Confidence Gate ───────────────────────


class ConfidenceGate:
    """Cross-source confidence calibration with tier-based caps.

    v3: Now includes freshness factor in confidence calculation.
    Stale knowledge is penalized even if semantically relevant.
    """

    def score(
        self,
        question: str,
        candidates: list[SourceResult],
        tier: RetrievalTier = RetrievalTier.L1_SUMMARY,
        accept_threshold: float = CONFIDENCE_ACCEPT,
        caution_threshold: float = CONFIDENCE_CAUTION,
    ) -> ConfidenceVerdict:
        """Score confidence with tier-appropriate cap."""
        if not candidates:
            return ConfidenceVerdict(
                score=0.0, action="skip",
                reason="No results", tier=tier,
            )

        config = TIER_CONFIG[tier]
        cap = config["confidence_cap"]

        # L1: Source-weighted scores
        weighted = []
        for c in candidates:
            weight = SOURCE_WEIGHTS.get(c.source, 0.5)
            weighted.append(c.score * weight)

        base = sum(weighted) / len(weighted)

        # L2: Cross-source consistency bonus
        source_types = {c.source for c in candidates}
        consistency = 0.1 * min(len(source_types) - 1, 3) if len(source_types) >= 2 else 0.0

        # L3: Depth bonus (riverbed)
        max_depth = max((c.depth for c in candidates), default=0.0)
        depth_bonus = min(0.15, 0.05 * math.log(1 + max_depth)) if max_depth > 1.0 else 0.0

        # L3.5: Emotional charge bonus (riverbed — emotionally carved = more trusted)
        max_charge = max((abs(c.emotional_charge) for c in candidates), default=0.0)
        emotion_bonus = min(0.10, 0.10 * max_charge)  # Up to 10% boost for strong emotion

        # L4: Recurrence bonus (learnings)
        max_count = max((c.metadata.get("count", 0) for c in candidates), default=0)
        recurrence = min(0.1, 0.03 * max_count) if max_count >= 3 else 0.0

        # L5 (v3): Freshness factor — penalize stale knowledge
        avg_freshness = sum(c.freshness for c in candidates) / len(candidates)
        # Freshness modulates: 1.0 = no penalty, 0.1 = 90% penalty on bonuses
        freshness_factor = 0.5 + 0.5 * avg_freshness  # Range [0.55, 1.0]

        # Apply freshness to bonuses (not to base score — even stale facts matter)
        raw = base + (consistency + depth_bonus + emotion_bonus + recurrence) * freshness_factor
        final = min(cap, max(0.0, raw))

        if final >= accept_threshold:
            action = "accept"
        elif final >= caution_threshold:
            action = "use_with_caution"
        else:
            action = "skip"

        src_count = len(source_types)
        reason = (
            f"conf={final:.2f} ({src_count} src, depth={max_depth:.1f}, "
            f"fresh={avg_freshness:.2f}, cap={cap})"
        )

        return ConfidenceVerdict(
            score=round(final, 3), action=action,
            reason=reason, tier=tier,
        )


# ── Divergent Search Strategy ─────────────


@dataclass
class DivergentResult:
    """Result of a divergent (Map-Reduce) search.

    IMPORTANT: `merged` contains ALL deduplicated results (for reference/storage).
    Use `top_results` for LLM injection — it's capped at OPTIMAL_RESULTS_PER_QUERY
    to avoid Lost-in-the-Middle degradation (Liu 2023).
    """

    branches: list[RetrievalResult]  # Per-branch best results
    merged: list[RetrievalResult]  # Deduplicated union (full archive)
    coverage: float  # 0-1 estimated facet coverage
    gaps: list[str]  # Detected uncovered facets

    @property
    def top_results(self) -> list[RetrievalResult]:
        """LLM-safe subset: top N results by confidence (Liu 2023 safe zone).

        merged = full archive for storage/reference.
        top_results = what you feed the LLM (≤3 to avoid accuracy degradation).
        """
        ranked = sorted(self.merged, key=lambda r: r.confidence, reverse=True)
        return ranked[:OPTIMAL_RESULTS_PER_QUERY]


class DivergentSearch:
    """Map-Reduce search for exploratory/comprehensive queries.

    I am a perfectionist who builds retrieval systems, but I know when to stop.
    Divergent search is NOT "retrieve 50 docs at once" (that triggers Lost in the Middle).
    It IS "retrieve 3 precise docs per facet, across N facets, then merge."

    CBUA mapping:
        B2 Deep + ToT multi-branch → Phase 1 scatter (N branches × 3 results)
        B1 Structured three-layer  → Phase 2 gather (merge + deduplicate)
        B4 Metacognition          → Phase 3 gap-check ("what did I miss?")

    Strategy:
        1. Decompose broad query into N focused facets (max DIVERGENT_MAX_BRANCHES)
        2. Each facet → convergent retrieval (3 precise results per facet)
        3. Merge all results → deduplicate by content overlap
        4. Gap check: detect uncovered areas → optional re-query
        5. Return structured DivergentResult with coverage estimate

    This way, each individual LLM call only sees 3-5 results (safe zone),
    but the total search covers N×3 documents across all facets.
    """

    def __init__(self, engine: "STAREngine"):
        self._engine = engine

    def decompose_facets(self, broad_query: str) -> list[str]:
        """Break a broad query into focused facets for parallel retrieval.

        Uses keyword extraction + category hints.
        Override with LLM-based decomposition via set_decomposer().
        """
        # Default: heuristic decomposition by common research facets
        base = broad_query[:120]
        facets = [
            f"{base} — current state and major players",
            f"{base} — recent breakthroughs and trends",
            f"{base} — limitations and open problems",
        ]

        # Add domain-specific facets if query hints at them
        if re.search(r"(?:全部|所有|完整|comprehensive|all|every)", broad_query):
            facets.extend([
                f"{base} — historical evolution",
                f"{base} — commercial applications",
                f"{base} — comparison and benchmarks",
                f"{base} — future directions",
            ])

        return facets[:DIVERGENT_MAX_BRANCHES]

    def search(
        self,
        broad_query: str,
        facets: list[str] | None = None,
    ) -> DivergentResult:
        """Execute divergent Map-Reduce search.

        Phase 1 (Scatter): Each facet → convergent L2 retrieval
        Phase 2 (Gather): Merge + deduplicate
        Phase 3 (Gap Check): Detect coverage gaps

        Args:
            broad_query: The broad exploratory query.
            facets: Optional pre-decomposed facets. None = auto-decompose.

        Returns:
            DivergentResult with per-branch results, merged set, and gaps.
        """
        if facets is None:
            facets = self.decompose_facets(broad_query)

        # Phase 1: Scatter — each facet gets 3 precise results
        branches: list[RetrievalResult] = []
        for facet in facets:
            results = self._engine.retrieve(
                facet,
                tier=RetrievalTier.L2_FULL,
                max_results=DIVERGENT_RESULTS_PER_BRANCH,
            )
            branches.extend(results)

        # Phase 2: Gather — deduplicate by content overlap
        merged = self._deduplicate(branches)

        # Phase 3: Gap check — estimate coverage
        coverage = self._estimate_coverage(facets, merged)
        gaps = self._detect_gaps(facets, merged)

        return DivergentResult(
            branches=branches,
            merged=merged,
            coverage=coverage,
            gaps=gaps,
        )

    def search_with_gap_fill(
        self,
        broad_query: str,
        facets: list[str] | None = None,
        max_rounds: int = DIVERGENT_GAP_CHECK_ROUNDS,
    ) -> DivergentResult:
        """Divergent search with automatic gap-filling rounds.

        After initial scatter-gather, detects gaps and runs additional
        focused queries to fill them. Max iterations = max_rounds.
        """
        result = self.search(broad_query, facets)

        for _ in range(max_rounds):
            if result.coverage >= 0.8 or not result.gaps:
                break

            # Fill gaps with focused queries
            gap_results: list[RetrievalResult] = []
            for gap in result.gaps[:3]:  # Max 3 gaps per round
                gap_query = f"{broad_query} — {gap}"
                gap_hits = self._engine.retrieve(
                    gap_query,
                    tier=RetrievalTier.L2_FULL,
                    max_results=DIVERGENT_RESULTS_PER_BRANCH,
                )
                gap_results.extend(gap_hits)

            if not gap_results:
                break

            all_results = result.merged + gap_results
            merged = self._deduplicate(all_results)
            all_facets = (facets or []) + result.gaps[:3]
            coverage = self._estimate_coverage(all_facets, merged)
            gaps = self._detect_gaps(all_facets, merged)

            result = DivergentResult(
                branches=result.branches + gap_results,
                merged=merged,
                coverage=coverage,
                gaps=gaps,
            )

        return result

    @staticmethod
    def _deduplicate(
        results: list[RetrievalResult],
    ) -> list[RetrievalResult]:
        """Remove near-duplicate results by answer text overlap."""
        if not results:
            return []

        unique: list[RetrievalResult] = []
        seen_words: list[set[str]] = []

        for r in sorted(results, key=lambda x: x.confidence, reverse=True):
            words = set(r.answer.lower().split())
            if not words:
                continue

            is_dup = False
            for seen in seen_words:
                overlap = len(words & seen) / max(len(words | seen), 1)
                if overlap > 0.6:  # >60% word overlap = duplicate
                    is_dup = True
                    break

            if not is_dup:
                unique.append(r)
                seen_words.append(words)

        return unique

    @staticmethod
    def _estimate_coverage(
        facets: list[str],
        results: list[RetrievalResult],
    ) -> float:
        """Estimate what fraction of facets have at least one result."""
        if not facets:
            return 0.0

        result_text = " ".join(r.answer.lower() for r in results)
        covered = 0
        for facet in facets:
            facet_words = set(facet.lower().split())
            # At least 2 facet words appear in results → covered
            matches = sum(1 for w in facet_words if w in result_text)
            if matches >= min(2, len(facet_words)):
                covered += 1

        return covered / len(facets)

    @staticmethod
    def _detect_gaps(
        facets: list[str],
        results: list[RetrievalResult],
    ) -> list[str]:
        """Find facets with no matching results."""
        if not facets:
            return []

        result_text = " ".join(r.answer.lower() for r in results)
        gaps: list[str] = []
        for facet in facets:
            facet_words = set(facet.lower().split())
            matches = sum(1 for w in facet_words if w in result_text)
            if matches < min(2, len(facet_words)):
                # Extract the distinguishing part after " — "
                if " — " in facet:
                    gaps.append(facet.split(" — ", 1)[1])
                else:
                    gaps.append(facet[:80])

        return gaps


# ── STAR Engine (Main Entry Point) ───────────────────────


class STAREngine:
    """Stimulus-Triggered Agentic Retrieval — unified cognitive RAG.

    I am the retrieval brain. I match depth to difficulty (CBUA C0).
    I think before I fetch (three-layer: root cause → sweet spot → strategy).
    I verify before I trust (U: counterexample → boundary stress).
    I forget what hurts me (Adaptive Forgetting: noise is not neutral, it is toxic).

    v3.5 Pipeline (precision-first, research-hardened):
    1. Cache check (AI-native: perfect session recall)
    2. Route (CBUA C0 complexity → tier) → Plan (decompose sub-questions)
    3. Parallel retrieve from all sources
    4. Freshness scoring (Ebbinghaus temporal decay)
    5. Rerank BEFORE filtering (precision-first: best survive)
    6. CRAG self-correction → web fallback if INCORRECT (U adversarial gate)
    6.5. Noise ratio guard (Cuconasu 2024: >30% noise = harmful)
    7. Associative expansion + re-cap to max_sources
    8. Compress to token budget
    9. Confidence gate (source weight × consistency × depth × freshness)
    10. Cache result + publish to shared bus (multi-agent knowledge)
    """

    def __init__(
        self,
        project_dir: str = "",
        rag_index=None,
        riverbed_mem=None,
        learnings_path: str = "",
        kb_skills_dir: str = "",
        web_search_fn=None,
        cache_dir: str = "",
        session_id: str = "",
        shared: bool = False,
        use_cross_encoder: bool = True,
        profile: RetrievalProfile = RetrievalProfile.PRECISION,
    ):
        self.project_dir = project_dir or os.environ.get("CLAUDE_PROJECT_DIR", ".")
        self.profile = profile
        self._profile_cfg = PROFILE_CONFIG[profile]
        cache_dir = cache_dir or os.path.join(self.project_dir, ".concinno_cache")

        # Core components (v2)
        self._router = AdaptiveRouter()
        self._planner = QueryPlanner()
        self._reranker = Reranker(use_cross_encoder=use_cross_encoder)
        self._compressor = ContextCompressor()
        self._crag = CRAGCorrector()
        self._gate = ConfidenceGate()

        # v3 components
        self._freshness = FreshnessScorer()
        self._associations = AssociativeIndex()
        self._session_cache = SessionCache()
        self._forgetter = AdaptiveForgetter()
        self._shared_bus = SharedMemoryBus(
            cache_dir=cache_dir, session_id=session_id,
        )

        # BM25 sparse index (lazy-built from KB + learnings)
        self._bm25 = BM25Index()
        self._build_bm25_index(kb_skills_dir, learnings_path)

        # Retriever with shared bus + BM25
        self._retriever = MultiSourceRetriever(
            project_dir=self.project_dir,
            rag_index=rag_index,
            riverbed_mem=riverbed_mem,
            learnings_path=learnings_path,
            kb_skills_dir=kb_skills_dir,
            web_search_fn=web_search_fn,
            shared_bus=self._shared_bus if shared else None,
            bm25_index=self._bm25,
        )

    # ── Internal ───────────────────────────────────────────

    def _build_bm25_index(self, kb_skills_dir: str, learnings_path: str) -> None:
        """Collect KB skill files + learnings into BM25 corpus and build index."""
        docs: list[dict] = []
        skills_dir = kb_skills_dir or os.path.join(
            self.project_dir, ".claude", "skills"
        )

        # KB skill files
        if os.path.isdir(skills_dir):
            try:
                for entry in os.listdir(skills_dir):
                    skill_dir = os.path.join(skills_dir, entry)
                    if not os.path.isdir(skill_dir) or not entry.startswith("kb_"):
                        continue
                    skill_file = os.path.join(skill_dir, "SKILL.md")
                    if not os.path.isfile(skill_file):
                        continue
                    try:
                        with open(skill_file, encoding="utf-8") as f:
                            content = f.read(8000)
                        docs.append({
                            "text": content,
                            "file": os.path.relpath(skill_file, self.project_dir),
                            "heading": entry,
                            "timestamp": os.path.getmtime(skill_file),
                        })
                    except Exception:
                        continue
            except Exception:
                pass

        # Learnings
        if learnings_path and os.path.isfile(learnings_path):
            try:
                with open(learnings_path, encoding="utf-8") as f:
                    data = json.load(f)
                for item in data.get("learnings", []):
                    text = item.get("correction_text", "")
                    context = item.get("context", "")
                    if text:
                        docs.append({
                            "text": f"{text} {context}".strip(),
                            "file": learnings_path,
                            "heading": "learning",
                            "timestamp": item.get("timestamp", 0.0),
                        })
            except Exception:
                pass

        if docs:
            self._bm25.build(docs)

    # ── Public API ────────────────────────────────────────

    def retrieve(
        self,
        query: str,
        tier: RetrievalTier = RetrievalTier.AUTO,
        max_results: int = 0,
    ) -> list[RetrievalResult]:
        """Full STAR v3.5 pipeline with caching, freshness, association, and sharing.

        Args:
            query: Task intent or search query.
            tier: Retrieval depth. AUTO = let STAR decide.
            max_results: Max verified results. 0 = use profile default.

        Returns:
            List of RetrievalResult ordered by confidence.
        """
        pcfg = self._profile_cfg
        if max_results <= 0:
            max_results = pcfg["optimal_results"]

        # Step 0: Session cache check (AI-native: perfect recall)
        actual_tier = self._router.route(query, tier)
        cached = self._session_cache.get(query, actual_tier)
        if cached:
            return cached

        # Merge tier config with profile overrides
        config = dict(TIER_CONFIG[actual_tier])  # copy
        scale = pcfg["tier_source_scale"]
        if scale != 1.0:
            config["max_sources"] = max(
                1, int(config["max_sources"] * scale)
            )
        # Profile web policy override
        if pcfg["web_policy"] == "always" and actual_tier != RetrievalTier.L0_INDEX:
            config["use_web"] = True
        elif pcfg["web_policy"] == "supplement" and actual_tier == RetrievalTier.L1_SUMMARY:
            config["use_web"] = True

        # Step 1: Plan sub-questions
        sub_questions = self._planner.plan(query, actual_tier)

        # Step 2-8: Process each sub-question through full pipeline
        verified: list[RetrievalResult] = []

        for sq in sub_questions:
            result = self._process_question(sq, actual_tier, config)
            if result:
                verified.append(result)

        # Escalation: if L0 produced no results or all low-confidence,
        # auto-escalate to L1 (prevents router misclassification from
        # silently returning wrong/empty answers)
        if actual_tier == RetrievalTier.L0_INDEX and tier == RetrievalTier.AUTO:
            best_conf = max((r.confidence for r in verified), default=0.0)
            if best_conf < pcfg["confidence_caution"] or not verified:
                # L0 failed or uncertain — escalate to L1
                l1_config = TIER_CONFIG[RetrievalTier.L1_SUMMARY]
                l1_sqs = self._planner.plan(query, RetrievalTier.L1_SUMMARY)
                for sq in l1_sqs:
                    result = self._process_question(
                        sq, RetrievalTier.L1_SUMMARY, l1_config,
                    )
                    if result:
                        verified.append(result)

        verified.sort(key=lambda r: r.confidence, reverse=True)
        results = verified[:max_results]

        # Step 9: Cache results + publish to shared bus
        for r in results:
            self._session_cache.put(r)
        if results:
            self._shared_bus.publish(results)

        return results

    def quick_recall(
        self,
        stimulus: str,
        tier: RetrievalTier = RetrievalTier.L1_SUMMARY,
        max_results: int = 3,
    ) -> list[RetrievalResult]:
        """Fast path for hooks — skip planning, direct retrieval."""
        actual_tier = self._router.route(stimulus, tier)

        # Check session cache first
        cached = self._session_cache.get(stimulus, actual_tier)
        if cached:
            return cached

        config = TIER_CONFIG[actual_tier]
        sq = SubQuestion(text=stimulus, category="convention")
        result = self._process_question(sq, actual_tier, config)

        if result:
            self._session_cache.put(result)

        return [result] if result else []

    def share(self) -> int:
        """Explicitly publish all cached results to shared bus for sub-agents."""
        count = 0
        for result in self._session_cache._cache.values():
            count += self._shared_bus.publish([result])
        return count

    def divergent_search(
        self,
        broad_query: str,
        facets: list[str] | None = None,
        gap_fill: bool = True,
    ) -> DivergentResult:
        """Divergent (Map-Reduce) search for exploratory queries.

        When you need "all AI technologies" not "which file has config" —
        scatter across facets (3 precise per facet), then gather + gap-check.

        Args:
            broad_query: Broad exploratory query.
            facets: Pre-decomposed facets. None = auto-decompose.
            gap_fill: If True, run gap-check rounds to fill missing areas.

        Returns:
            DivergentResult with branches, merged results, coverage, and gaps.
        """
        ds = DivergentSearch(self)
        if gap_fill:
            return ds.search_with_gap_fill(broad_query, facets)
        return ds.search(broad_query, facets)

    def forget(self, target_size: int = 0) -> list[ForgetCandidate]:
        """Run adaptive forgetting on session cache (Adaptive Forgetting).

        Evaluates all cached source results and returns forget candidates.
        Does NOT auto-remove — caller decides what to act on.

        Args:
            target_size: Target pool size. 0 = use default max_pool.

        Returns:
            List of ForgetCandidates sorted by forget priority.
        """
        all_sources = list(self._session_cache.get_source_cache().values())
        return self._forgetter.evaluate(all_sources)

    def format_injection(
        self,
        results: list[RetrievalResult],
        max_tokens: int = 500,
    ) -> str:
        """Format results for hook additionalContext injection."""
        if not results:
            return ""

        pcfg = self._profile_cfg
        caution = pcfg["confidence_caution"]
        accepted = [r for r in results if r.confidence >= caution]
        if not accepted:
            return ""

        # Position-aware ranking (Liu 2023 Lost in the Middle):
        # Precision + Balanced: best at edges, weakest in middle.
        # Recall: pure score sort (more results, layout less critical).
        accepted.sort(key=lambda r: r.confidence, reverse=True)
        if pcfg["position_aware"] and len(accepted) >= 3:
            reordered = [accepted[0]]
            reordered.extend(accepted[2:])  # middle = weakest
            reordered.append(accepted[1])   # second-best at end
            accepted = reordered

        lines: list[str] = []
        avg_conf = sum(r.confidence for r in accepted) / len(accepted)
        tier_label = accepted[0].tier.value if accepted else "auto"
        lines.append(
            f"STAR/{tier_label} {len(accepted)} insight(s) ({avg_conf:.0%}):"
        )

        char_budget = max_tokens * 4
        used = len(lines[0])

        for r in accepted:
            icon = "+" if r.confidence >= CONFIDENCE_ACCEPT else "~"
            src_tags = ", ".join(sorted({s.source.value for s in r.sources}))
            crag_tag = (
                f" [{r.crag_action.value}]"
                if r.crag_action != CRAGAction.CORRECT else ""
            )
            line = f"  [{icon}{r.confidence:.0%}] {r.answer}{crag_tag}"
            if src_tags:
                line += f" ({src_tags})"

            if used + len(line) + 1 > char_budget:
                break
            lines.append(line)
            used += len(line) + 1

        return "\n".join(lines)

    def stats(self) -> dict:
        """Engine statistics."""
        has_rag = self._retriever._rag is not None
        has_riverbed = self._retriever._riverbed is not None
        has_learnings = bool(
            self._retriever._learnings_path
            and os.path.isfile(self._retriever._learnings_path)
        )
        has_kb = os.path.isdir(self._retriever._kb_skills_dir)
        has_web = self._retriever._web_search_fn is not None
        available = sum([has_rag, has_riverbed, has_learnings, has_kb, has_web])

        return {
            "version": "3.5",
            "sources_available": available,
            "has_rag": has_rag,
            "has_riverbed": has_riverbed,
            "has_learnings": has_learnings,
            "has_kb_skills": has_kb,
            "has_web_search": has_web,
            "tiers": ["L0_INDEX", "L1_SUMMARY", "L2_FULL"],
            "features": {
                "adaptive_routing": True,
                "reranker": True,
                "compression": True,
                "crag_correction": True,
                "confidence_calibration": True,
                "parent_child_chunks": has_rag,
                "temporal_freshness": True,
                "associative_expansion": True,
                "session_cache": True,
                "multi_agent_sharing": True,
                "web_fallback": has_web,
            },
            "session_cache": self._session_cache.stats(),
            "associations": self._associations.size(),
            "forgetter": self._forgetter.stats(),
        }

    # ── Internal Pipeline ─────────────────────────────────

    def _process_question(
        self,
        sq: SubQuestion,
        tier: RetrievalTier,
        config: dict,
    ) -> RetrievalResult | None:
        """Process a single sub-question through the full v3.5 pipeline.

        I retrieve with precision, not volume. Three gold pieces outweigh ten mixed ore.
        Every result must earn its place — noise is not neutral, it is toxic (Cuconasu 2024).

        Pipeline: Retrieve → Freshness → Rerank → CRAG → NoiseGuard → Associate → Compress → Gate
        """
        # Step 3: Retrieve
        candidates = self._retriever.retrieve(sq, tier)
        if not candidates:
            return None

        # Step 4: Freshness scoring (temporal weighting)
        self._freshness.apply_to_results(candidates)

        # Step 5: Rerank BEFORE filtering (precision-first)
        if config["use_reranker"]:
            candidates = self._reranker.rerank(
                sq.text, candidates, top_k=config["max_sources"],
            )

        # Step 6: CRAG self-correction (L2 only, profile-aware)
        crag_action = CRAGAction.CORRECT
        crag_policy = self._profile_cfg["crag_on_incorrect"]
        if config["use_crag"]:
            crag_action = self._crag.evaluate(sq.text, candidates)
            if crag_action == CRAGAction.INCORRECT:
                if crag_policy == "reject":
                    # Precision: mark rejected, try alternatives
                    for c in candidates:
                        self._forgetter.mark_rejected(AssociativeIndex._key(c))
                elif crag_policy == "downgrade":
                    # Recall: keep with halved scores
                    for c in candidates:
                        c.score *= 0.5
                    crag_action = CRAGAction.AMBIGUOUS
                else:
                    # Balanced: keep with mild penalty
                    for c in candidates:
                        c.score *= 0.7
                    crag_action = CRAGAction.AMBIGUOUS

                if crag_policy == "reject":
                    # Try alternative internal source first
                    alt_candidates = self._try_alternative_source(sq, tier)
                    if alt_candidates:
                        candidates = alt_candidates
                        crag_action = self._crag.evaluate(sq.text, candidates)
                    elif config.get("use_web"):
                        web_results = self._retriever._query_web(
                            sq.text, self._profile_cfg["max_web"],
                        )
                        if web_results:
                            candidates = web_results
                            crag_action = CRAGAction.AMBIGUOUS
                        else:
                            return None
                    else:
                        return None

        # Step 6.5: Noise ratio guard (profile-aware)
        pcfg = self._profile_cfg
        candidates = self._apply_noise_guard(
            candidates, ratio_max=pcfg["noise_ratio_max"],
        )

        # Step 7: Associative expansion (L2 only, spreading activation)
        # Post-noise-guard: expansion results also subject to source cap
        if config.get("use_association"):
            source_cache = self._session_cache.get_source_cache()
            associated = self._associations.expand(candidates, source_cache)
            if associated:
                candidates.extend(associated)
                candidates.sort(key=lambda c: c.score, reverse=True)
                # Re-apply source cap after expansion
                candidates = candidates[:config["max_sources"]]

        # Step 8: Compress (L1+)
        if config["use_compression"]:
            candidates = self._compressor.compress(
                candidates, config["max_tokens"], sq.text
            )

        # Step 9: Confidence gate (profile-aware thresholds)
        verdict = self._gate.score(
            sq.text, candidates, tier,
            accept_threshold=pcfg["confidence_accept"],
            caution_threshold=pcfg["confidence_caution"],
        )
        if verdict.action == "skip":
            # Recall mode: downgrade instead of skip
            if pcfg["crag_on_incorrect"] != "reject":
                verdict = ConfidenceVerdict(
                    score=verdict.score,
                    action="use_with_caution",
                    reason=f"kept by {self.profile.value} profile",
                    tier=tier,
                )
            else:
                return None

        # Record co-occurrence for future associative retrieval
        top_n = min(OPTIMAL_RESULTS_PER_QUERY, len(candidates))
        self._associations.record_cooccurrence(candidates[:top_n])

        answer = _synthesize(candidates[:OPTIMAL_RESULTS_PER_QUERY])
        tokens_est = len(answer) // 4  # Rough estimate

        return RetrievalResult(
            question=sq.text,
            answer=answer,
            confidence=verdict.score,
            tier=tier,
            sources=candidates[:OPTIMAL_RESULTS_PER_QUERY],
            reasoning=verdict.reason,
            crag_action=crag_action,
            tokens_used=tokens_est,
        )

    @staticmethod
    @staticmethod
    def _apply_noise_guard(
        candidates: list[SourceResult],
        ratio_max: float = NOISE_RATIO_MAX,
    ) -> list[SourceResult]:
        """Enforce noise ratio limit (Cuconasu 2024).

        Unverified sources (WEB_SEARCH) cannot exceed ratio_max
        of total results. Profile-aware: precision=30%, recall=60%, balanced=40%.
        This is a hard gate, not a soft warning — noise is toxic, not neutral.
        """
        if not candidates:
            return candidates

        web = [c for c in candidates if c.source == SourceType.WEB_SEARCH]
        if not web:
            return candidates

        internal = len(candidates) - len(web)
        # Strict enforcement: web / (internal + web) <= ratio_max
        # Solved: max_web = floor(internal * ratio / (1 - ratio))
        if internal <= 0:
            max_web = 0
        else:
            max_web = int(internal * ratio_max / (1.0 - ratio_max))

        if len(web) <= max_web:
            return candidates

        web.sort(key=lambda c: c.score, reverse=True)
        allowed_web = set(id(w) for w in web[:max_web])

        return [
            c for c in candidates
            if c.source != SourceType.WEB_SEARCH or id(c) in allowed_web
        ]

    def _try_alternative_source(
        self,
        sq: SubQuestion,
        tier: RetrievalTier,
    ) -> list[SourceResult]:
        """When CRAG says INCORRECT, try a different source."""
        all_sources = [s for s in SourceType if s != SourceType.WEB_SEARCH]
        preferred = CATEGORY_SOURCE_MAP.get(sq.category, [])
        alternatives = [s for s in all_sources if s not in preferred]

        for alt_source in alternatives:
            alt_sq = SubQuestion(
                text=sq.text,
                category=sq.category,
                priority=sq.priority,
                source_hint=alt_source,
            )
            results = self._retriever.retrieve(alt_sq, tier, max_per_source=3)
            if results and results[0].score >= 0.4:
                return results

        return []


# ── Helpers ──────────────────────────────────────────────


def _find_best_paragraph(content: str, query_words: set[str]) -> str:
    """Find the paragraph in content with the most query word matches."""
    paragraphs = re.split(r"\n\n+", content)
    if not paragraphs:
        return content[:500]

    best = ""
    best_score = 0

    for para in paragraphs:
        para_lower = para.lower()
        score = sum(1 for w in query_words if w in para_lower)
        if score > best_score:
            best_score = score
            best = para

    return best or paragraphs[0]


def _synthesize(candidates: list[SourceResult]) -> str:
    """Merge top candidates into a concise answer."""
    if not candidates:
        return ""
    if len(candidates) == 1:
        return candidates[0].text[:300]

    base = candidates[0].text[:200]
    seen_words = set(base.lower().split())

    for c in candidates[1:]:
        words = set(c.text.lower().split())
        new_words = words - seen_words
        if len(new_words) > 2:
            base += f" | {c.text[:100]}"
            seen_words |= new_words
        if len(base) > 400:
            break

    return base[:500]


# ── Factory ──────────────────────────────────────────────


def create_star_engine(
    project_dir: str = "",
    cache_dir: str = "",
    web_search_fn=None,
    session_id: str = "",
    shared: bool = False,
    profile: RetrievalProfile = RetrievalProfile.PRECISION,
) -> STAREngine:
    """Create a STAR engine with auto-detected sources.

    Args:
        project_dir: Project root directory.
        cache_dir: Cache directory for RAG/Riverbed/shared bus.
        web_search_fn: Optional web search callable(query) -> list[dict].
        session_id: Session ID for shared bus. Default: from CC_SESSION_ID env.
        shared: If True, read from shared bus (for sub-agents).

    Returns:
        Configured STAREngine instance.
    """
    project_dir = project_dir or os.environ.get("CLAUDE_PROJECT_DIR", ".")
    cache_dir = cache_dir or os.path.join(project_dir, ".concinno_cache")

    rag_index = None
    riverbed_mem = None

    try:
        from concinno.rag import RAGIndex
        rag_path = os.path.join(cache_dir, "rag")
        if os.path.isdir(rag_path):
            rag_index = RAGIndex(project_dir=project_dir, cache_dir=rag_path)
    except Exception:
        pass

    try:
        from concinno.riverbed import RiverbedMemory
        rb_path = os.path.join(cache_dir, "riverbed")
        riverbed_mem = RiverbedMemory(
            project_dir=project_dir, cache_dir=rb_path, rag_index=rag_index
        )
    except Exception:
        pass

    home = os.path.expanduser("~")
    learnings_path = os.path.join(home, ".claude", "cognitive", "learnings.json")
    if not os.path.isfile(learnings_path):
        learnings_path = ""

    return STAREngine(
        project_dir=project_dir,
        rag_index=rag_index,
        riverbed_mem=riverbed_mem,
        learnings_path=learnings_path,
        web_search_fn=web_search_fn,
        cache_dir=cache_dir,
        session_id=session_id,
        shared=shared,
        profile=profile,
    )


__all__ = [
    # Engine
    "STAREngine",
    "create_star_engine",
    # Components (pluggable)
    "AdaptiveRouter",
    "QueryPlanner",
    "MultiSourceRetriever",
    "Reranker",
    "ContextCompressor",
    "CRAGCorrector",
    "ConfidenceGate",
    # v3 components
    "FreshnessScorer",
    "AssociativeIndex",
    "SessionCache",
    "SharedMemoryBus",
    "AdaptiveForgetter",
    "ForgetCandidate",
    # Data structures
    "RetrievalTier",
    "SourceType",
    "CRAGAction",
    "SubQuestion",
    "SourceResult",
    "RetrievalResult",
    "ConfidenceVerdict",
    # v3.5 Divergent search
    "SearchMode",
    "DivergentSearch",
    "DivergentResult",
    # Research-backed constants (v3.5)
    "OPTIMAL_RESULTS_PER_QUERY",
    "MAX_WEB_RESULTS",
    "NOISE_RATIO_MAX",
    "TIER_CONFIG",
    # Profile system (v3.5)
    "RetrievalProfile",
    "PROFILE_CONFIG",
    # BM25 sparse retrieval
    "BM25Index",
    # Confluence RAG
    "ConfluenceRAG",
    "ConfluencePath",
    "ConfluencePoint",
]
