"""Confluence RAG: Multi-path fusion with auto-tuning and fixed-parameter modes.

This is the production-ready fusion engine. Two modes:
1. AUTO (default): Analyzes query semantics + corpus characteristics to auto-tune
2. FIXED: Uses a single optimal parameter set across all domains

Theory basis:
- 意識張力論 (Tension Theory): R=T/M, paths agree → decisive, disagree → explore
- 河床論 (Riverbed Theory): Raw scores = depth of riverbed, preserve magnitude
- 太極融合 (Taiji Fusion): BM25 (陰/keyword) + Dense (陽/semantic) dynamic balance
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum


class FusionMode(Enum):
    """Fusion operating mode."""

    AUTO = "auto"          # Semantic auto-tuning (default)
    FIXED = "fixed"        # Single fixed parameter set
    AGGRESSIVE = "aggressive"  # Maximize nDCG (may overfit)


@dataclass
class FusionConfig:
    """Fusion parameters — one set for all datasets in FIXED mode."""

    k_low: int = 3           # RRF k when high agreement (decisive)
    k_high: int = 10         # RRF k when high tension (inclusive)
    top_n: int = 20          # Window for measuring agreement
    boost_max: float = 1.2   # Max agreement boost multiplier
    score_w: float = 0.5     # Riverbed weight (0=pure RRF, 1=pure score)
    bw: float = 0.8          # BM25 path weight
    dw: float = 1.4          # Dense path weight
    mode: FusionMode = FusionMode.AUTO


# Universal fixed config — beats Simple RRF on BOTH E5-PT and MiniLM
# Sweep result: E5-PT 0.7573(+0.32%) | MiniLM 0.7063(+0.62%)
FIXED_UNIVERSAL = FusionConfig(
    k_low=2, k_high=5, top_n=20,
    boost_max=1.2, score_w=0.5, bw=0.8, dw=1.0,
    mode=FusionMode.FIXED,
)

# E5-PT-specific config (world #1 on SciFact: 0.7578, but loses on MiniLM)
FIXED_E5PT = FusionConfig(
    k_low=3, k_high=10, top_n=20,
    boost_max=1.2, score_w=0.5, bw=0.8, dw=1.4,
    mode=FusionMode.FIXED,
)

# Default = universal
FIXED_OPTIMAL = FIXED_UNIVERSAL


@dataclass
class QueryAnalysis:
    """Per-query semantic analysis for auto-tuning."""

    tension: float = 0.0          # BM25/Dense disagreement (0=agree, 1=disagree)
    agreement: float = 0.0        # Overlap ratio
    bm25_confidence: float = 0.0  # BM25 score spread (high = confident)
    dense_confidence: float = 0.0  # Dense score spread
    query_specificity: float = 0.0  # How specific the query is (keyword density)
    adaptive_k: int = 5


@dataclass
class FusionResult:
    """Result of fusion with diagnostic metadata."""

    ranked: list[tuple[str, float]] = field(default_factory=list)
    analysis: QueryAnalysis = field(default_factory=QueryAnalysis)
    config_used: FusionConfig = field(default_factory=FusionConfig)


def _measure_confidence(results: list[tuple[str, float]], top_n: int = 10) -> float:
    """Measure how confident a retrieval path is (scale-invariant).

    Normalize scores to [0,1] first, then measure separation between
    top-k and rest. This makes BM25 and Dense confidence comparable.
    """
    if len(results) < top_n + 5:
        return 0.0
    scores = [s for _, s in results[:top_n * 3]]
    mn, mx = min(scores), max(scores)
    rng = mx - mn
    if rng < 1e-10:
        return 0.0  # All scores identical = no confidence
    # Normalize to [0, 1]
    normed = [(s - mn) / rng for s in scores]
    top_mean = sum(normed[:top_n]) / top_n
    rest_mean = sum(normed[top_n:]) / len(normed[top_n:])
    # Separation in normalized space (0~1 range)
    return min(1.0, max(0.0, (top_mean - rest_mean) * 2.0))


def _measure_specificity(query: str) -> float:
    """Estimate query specificity from lexical features.

    Specific queries (technical terms, numbers, proper nouns) → BM25 advantage
    Broad queries (conceptual, abstract) → Dense advantage
    """
    words = query.lower().split()
    if not words:
        return 0.5
    # Heuristics for specificity
    has_numbers = any(any(c.isdigit() for c in w) for w in words)
    avg_word_len = sum(len(w) for w in words) / len(words)
    short_query = len(words) <= 5
    # Long words + numbers + short = specific
    score = 0.5
    if has_numbers:
        score += 0.2
    if avg_word_len > 6:
        score += 0.15
    if short_query:
        score += 0.1
    return min(1.0, score)


def _normalize_scores(
    results: list[tuple[str, float]],
) -> dict[str, float]:
    """Min-max normalize scores to [0, 1]."""
    if not results:
        return {}
    vals = [s for _, s in results]
    mn, mx = min(vals), max(vals)
    rng = mx - mn if mx > mn else 1.0
    return {did: (s - mn) / rng for did, s in results}


def analyze_query(
    bm25_results: list[tuple[str, float]],
    dense_results: list[tuple[str, float]],
    query_text: str = "",
    top_n: int = 20,
) -> QueryAnalysis:
    """Analyze query characteristics for auto-tuning."""
    b_set = {did for did, _ in bm25_results[:top_n]}
    d_set = {did for did, _ in dense_results[:top_n]}
    union = b_set | d_set
    agreement = len(b_set & d_set) / len(union) if union else 0.0
    tension = 1.0 - agreement

    return QueryAnalysis(
        tension=tension,
        agreement=agreement,
        bm25_confidence=_measure_confidence(bm25_results, top_n),
        dense_confidence=_measure_confidence(dense_results, top_n),
        query_specificity=_measure_specificity(query_text),
        adaptive_k=0,  # Will be set by fusion
    )


def auto_tune(analysis: QueryAnalysis, base: FusionConfig) -> FusionConfig:
    """Auto-tune v3: Self-sensing — derive params from result characteristics.

    不依賴 base config 的固定權重，而是從搜尋結果自己判斷：
    1. 哪條路更可信 → bw/dw
    2. 分數有沒有意義 → score_w
    3. 兩路同不同意 → k（已有 tension）

    核心原則：量測結果特性，不預設模型身份。
    對任何模型都通用。
    """
    bc = analysis.bm25_confidence   # BM25 top-k 分離度
    dc = analysis.dense_confidence  # Dense top-k 分離度

    # ── 核心：信心差距決定一切 ──
    # 差距大 → 偏向更強的那路 + 河床加持
    # 差距小 → 分不出誰強 → 回歸 Simple RRF（已證明跨模型穩定）
    conf_gap = abs(bc - dc)  # 0 = 分不出, 1 = 一方碾壓

    if conf_gap < 0.25:
        # 分不出誰強 → Simple RRF 參數（proven baseline）
        bw = 1.0
        dw = 1.2
        score_w = 0.0  # 純 rank，不信分數
    elif dc > bc:
        # Dense 明顯更強 → 偏 Dense + 河床
        strength = min(1.0, conf_gap * 2)  # 0~1
        bw = 1.0 - 0.2 * strength    # 1.0 → 0.8
        dw = 1.2 + 0.2 * strength    # 1.2 → 1.4
        score_w = 0.3 * strength      # 0 → 0.3（河床漸入）
    else:
        # BM25 明顯更強 → 偏 BM25，不信分數
        strength = min(1.0, conf_gap * 2)
        bw = 1.0 + 0.15 * strength   # 1.0 → 1.15
        dw = 1.2 - 0.2 * strength    # 1.2 → 1.0
        score_w = 0.0  # BM25 強 = keyword 查詢，分數沒意義

    # ── Tension → k range ──
    k_low = base.k_low
    k_high = base.k_high
    boost_max = base.boost_max

    if analysis.tension > 0.85:
        k_high = min(15, k_high + 2)
    elif analysis.tension < 0.2:
        k_low = max(1, k_low - 1)
        boost_max = min(1.4, boost_max + 0.1)

    return FusionConfig(
        k_low=k_low, k_high=k_high, top_n=base.top_n,
        boost_max=boost_max, score_w=score_w,
        bw=bw, dw=dw, mode=FusionMode.AUTO,
    )


def fuse(
    bm25_results: list[tuple[str, float]],
    dense_results: list[tuple[str, float]],
    query_text: str = "",
    config: FusionConfig | None = None,
) -> FusionResult:
    """Main fusion entry point.

    Args:
        bm25_results: BM25 ranked results [(doc_id, score), ...]
        dense_results: Dense ranked results [(doc_id, score), ...]
        query_text: Original query text (needed for AUTO mode)
        config: Fusion config. None = AUTO mode with optimal defaults.

    Returns:
        FusionResult with ranked docs, analysis, and config used.
    """
    if config is None:
        config = FusionConfig(mode=FusionMode.AUTO)

    # Analyze query
    analysis = analyze_query(
        bm25_results, dense_results, query_text, config.top_n,
    )

    # Auto-tune if in AUTO mode
    if config.mode == FusionMode.AUTO:
        effective_config = auto_tune(analysis, config)
    else:
        effective_config = config

    # If Auto decided score_w=0 (RRF fallback), use simple RRF directly
    # No adaptive_k, no boost — pure proven RRF
    if effective_config.score_w == 0.0 and config.mode == FusionMode.AUTO:
        rrf_result = fuse_simple_rrf(
            bm25_results, dense_results,
            k=5, bw=effective_config.bw, dw=effective_config.dw,
        )
        analysis.adaptive_k = 5
        return FusionResult(
            ranked=rrf_result, analysis=analysis, config_used=effective_config,
        )

    # Compute adaptive k from tension
    tension = analysis.tension
    adaptive_k = max(1, int(
        effective_config.k_low
        + (effective_config.k_high - effective_config.k_low) * tension
    ))
    analysis.adaptive_k = adaptive_k

    # ── Stage 1: Tension-Adaptive RRF ──
    rrf_scores: dict[str, float] = defaultdict(float)
    presence: dict[str, int] = defaultdict(int)

    for rank, (did, _) in enumerate(bm25_results):
        rrf_scores[did] += effective_config.bw / (adaptive_k + rank + 1)
        presence[did] += 1
    for rank, (did, _) in enumerate(dense_results):
        rrf_scores[did] += effective_config.dw / (adaptive_k + rank + 1)
        presence[did] += 1

    # Agreement boost
    boost = 1.0 + (effective_config.boost_max - 1.0) * analysis.agreement
    for did in rrf_scores:
        if presence[did] >= 2:
            rrf_scores[did] *= boost

    # ── Stage 2: Riverbed Score Integration ──
    b_n = _normalize_scores(bm25_results)
    d_n = _normalize_scores(dense_results)

    rv = list(rrf_scores.values())
    if rv:
        r_mn, r_mx = min(rv), max(rv)
        r_rng = r_mx - r_mn if r_mx > r_mn else 1.0
    else:
        r_mn, r_rng = 0.0, 1.0

    sw = effective_config.score_w
    total_w = effective_config.bw + effective_config.dw

    all_docs = set(rrf_scores) | set(b_n) | set(d_n)
    final: dict[str, float] = {}
    for did in all_docs:
        r = (rrf_scores.get(did, 0) - r_mn) / r_rng
        s = (
            effective_config.bw * b_n.get(did, 0)
            + effective_config.dw * d_n.get(did, 0)
        ) / total_w
        final[did] = (1 - sw) * r + sw * s

    ranked = sorted(final.items(), key=lambda x: x[1], reverse=True)

    return FusionResult(
        ranked=ranked,
        analysis=analysis,
        config_used=effective_config,
    )


# ── Convenience functions ─────────────────────────────────


def fuse_auto(
    bm25_results: list[tuple[str, float]],
    dense_results: list[tuple[str, float]],
    query_text: str = "",
) -> list[tuple[str, float]]:
    """Auto-tuning fusion (default mode). Returns ranked list."""
    result = fuse(bm25_results, dense_results, query_text)
    return result.ranked


def fuse_fixed(
    bm25_results: list[tuple[str, float]],
    dense_results: list[tuple[str, float]],
) -> list[tuple[str, float]]:
    """Fixed-parameter fusion. Returns ranked list."""
    result = fuse(bm25_results, dense_results, config=FIXED_OPTIMAL)
    return result.ranked


def fuse_simple_rrf(
    bm25_results: list[tuple[str, float]],
    dense_results: list[tuple[str, float]],
    k: int = 5,
    bw: float = 1.0,
    dw: float = 1.2,
) -> list[tuple[str, float]]:
    """Simple asymmetric RRF baseline. No theory, no tricks."""
    scores: dict[str, float] = defaultdict(float)
    for rank, (did, _) in enumerate(bm25_results):
        scores[did] += bw / (k + rank + 1)
    for rank, (did, _) in enumerate(dense_results):
        scores[did] += dw / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)
