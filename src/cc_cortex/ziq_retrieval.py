"""cc_cortex.ziq_retrieval — EMA adaptive retrieval for Cognitive RAG.

@module ziq_retrieval
@responsibility Learn which knowledge sources (corrections, rules, skills,
    handoffs) are actually useful per-query, using EMA online learning.
    Replaces flat ChromaDB ranking with adaptive multi-source fusion.
    Also learns optimal routing breadth thresholds via FTRL.
@dependencies cc_cortex.core.state_store
@exports ZIQRetrieval, rerank_results, record_feedback

This is ZIQ-Find technology feeding back into CCC:
  - ZIQ-Score uses FTRL to learn retriever weights (search domain)
  - Here we use FTRL in two places:
      (a) knowledge-source weights (per source_type, see feedback())
      (b) routing breadth thresholds (low/high cutoff, see route_feedback())
  - PTME four-step mapping (synchronic / structural, NO outcome):
      P=source stability, T=query pattern change,
      M=structural pattern memory (co-occurrence / centroid),
      E=fusion decision
  - FTRL is the diachronic / causal layer and lives OUTSIDE PTME.
    Historical error "M = FTRL weights" was corrected 2026-04-13.
    See kb_ziq/split_architecture.md for the v6.1 PTME ⊥ FTRL layering.

v6.1 production status (2026-04-13):
  This module is still the merged-state router (one weight per
  source_type, threshold FTRL on breadth). It does NOT yet implement
  the Bayesian-fused B-v2 split architecture validated by the 4-pod
  benchmark (see benchmarks/ablation_arch_v2.py and
  benchmarks/results/pod_summary.json). Migration from merged to
  split is a separate task and is tracked in benchmark3 handoff.

Design (current, merged-state):
  RAG returns N results from ChromaDB. Each result has a source type
  (correction, rule, skill, handoff). FTRL learns per-source-type weights
  from implicit feedback (was the correction actually used? did the agent
  read the linked skill?).

  The FTRL weights adjust over time:
  - Source type that keeps getting used → weight goes UP
  - Source type that gets ignored → weight goes DOWN
  - New session resets nothing (weights persist cross-session)

  This is the ZIQ "教練面" (coach) for RAG — it doesn't change the
  retriever (ChromaDB embeddings), it changes which results to trust.

PTME SOP for RAG (current merged-state, not v6.1 split):
  P (勢): source_type hit rate stability (are corrections consistently useful?)
  T (張): query pattern shift (new domain → reset confidence)
  M (記): structural pattern memory — here stored as source_type hit counts
        (co-occurrence between query class and source outcome).
        NOT FTRL. FTRL in route_feedback() below is a separate
        threshold learner, not the M layer.
  E (決): final score = chromadb_score * source_weight[source_type]
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from cc_cortex.core.state_store import StateStore

_NS = "ziq_retrieval"

# Source types for RAG knowledge
SOURCE_TYPES = (
    "correction",  # Past mistakes / learnings
    "rule",        # L0/L1/L2 rules
    "skill",       # KB skills
    "handoff",     # Handoff summaries
    "memory",      # Memory files
)

# EMA learning rate (how fast weights adapt)
DEFAULT_LR = 0.15
# Weight bounds
WEIGHT_MIN = 0.1
WEIGHT_MAX = 5.0

# ── Adaptive threshold defaults (ablation-validated) ─────
# Grid search: 0.20/0.55 beats 0.30/0.60 by +1.17pp F1.5
# These are FTRL initial values; online learning adjusts them.
DEFAULT_LOW_THRESHOLD = 0.20
DEFAULT_HIGH_THRESHOLD = 0.55
THRESHOLD_LR = 0.05  # Slower than source weights (thresholds are global)
THRESHOLD_MIN = 0.05
THRESHOLD_MAX = 0.50  # low_threshold upper bound
THRESHOLD_HIGH_MIN = 0.30
THRESHOLD_HIGH_MAX = 0.90


@dataclass
class SourceState:
    """Per-source-type adaptive state."""

    weight: float = 1.0  # Current weight (EMA)
    hits: int = 0     # Total times this source appeared in results
    used: int = 0     # Total times result from this source was used


@dataclass
class ThresholdState:
    """FTRL-Proximal learned routing thresholds.

    Uses real FTRL-Proximal (McMahan 2013) with O(√T) regret:
      w_{t+1} = argmin_w [g_{1:t}·w + ½Σσ_s||w-w_s||² + λ₁||w||₁]

    For 2-param case, closed form per-coordinate:
      z_i += g_i - σ_i * w_i  (accumulate)
      w_i = -z_i / (λ₂ + Σσ) if |z_i| > λ₁ else 0

    We skip L1 (no sparsity needed for 2 params) →
      w_i = -z_i / (λ₂ + n_i)
    where n_i = cumulative sum of per-coordinate σ.
    """

    low: float = DEFAULT_LOW_THRESHOLD
    high: float = DEFAULT_HIGH_THRESHOLD
    n_updates: int = 0
    # FTRL-Proximal state (per-coordinate accumulators)
    z_low: float = 0.0   # cumulative adjusted gradient for low
    z_high: float = 0.0  # cumulative adjusted gradient for high
    n_low: float = 0.0   # cumulative σ for low (adaptive LR)
    n_high: float = 0.0  # cumulative σ for high


def _ema_update(
    state: SourceState,
    reward: float,
    lr: float = DEFAULT_LR,
) -> SourceState:
    """EMA weight update. Positive reward = source was useful.

    Args:
        state: Current state for one source type.
        reward: +1.0 if used, -0.1 if ignored (asymmetric).
        lr: Learning rate (0-1).

    Returns:
        Updated SourceState.
    """
    # EMA: weight = (1-lr)*weight + lr*target
    # target = weight * (1 + reward) — multiplicative update
    target = state.weight * (1.0 + reward)
    state.weight = (1.0 - lr) * state.weight + lr * target

    # Clamp
    state.weight = max(WEIGHT_MIN, min(WEIGHT_MAX, state.weight))

    return state


class ZIQRetrieval:
    """EMA adaptive retrieval layer for CCC RAG.

    Wraps RAG search results with learned source-type weights.

    Usage::

        ziq = ZIQRetrieval(cache_dir="/path/to/cache")

        # After RAG search returns results
        reranked = ziq.rerank(results)

        # After observing which results were actually used
        ziq.feedback(used_files=["corrections/fix_123.md"])
    """

    def __init__(self, cache_dir: str):
        self._store = StateStore(cache_dir)

    def _load_states(self) -> dict[str, SourceState]:
        """Load source states from persistent store."""
        raw = self._store.read(_NS, "source_states", default={})
        states = {}
        for stype in SOURCE_TYPES:
            data = raw.get(stype, {})
            states[stype] = SourceState(
                weight=data.get("weight", 1.0),
                hits=data.get("hits", 0),
                used=data.get("used", 0),
            )
        return states

    def _save_states(self, states: dict[str, SourceState]) -> None:
        """Persist source states."""
        raw = {}
        for stype, state in states.items():
            raw[stype] = {
                "weight": state.weight,
                "hits": state.hits,
                "used": state.used,
            }
        self._store.write(_NS, "source_states", raw)

    def _load_thresholds(self) -> ThresholdState:
        """Load routing thresholds + FTRL state from store."""
        raw = self._store.read(_NS, "thresholds", default={})
        return ThresholdState(
            low=raw.get("low", DEFAULT_LOW_THRESHOLD),
            high=raw.get("high", DEFAULT_HIGH_THRESHOLD),
            n_updates=raw.get("n_updates", 0),
            z_low=raw.get("z_low", 0.0),
            z_high=raw.get("z_high", 0.0),
            n_low=raw.get("n_low", 0.0),
            n_high=raw.get("n_high", 0.0),
        )

    def _save_thresholds(self, ts: ThresholdState) -> None:
        """Persist routing thresholds + FTRL state."""
        self._store.write(_NS, "thresholds", {
            "low": round(ts.low, 4),
            "high": round(ts.high, 4),
            "n_updates": ts.n_updates,
            "z_low": round(ts.z_low, 6),
            "z_high": round(ts.z_high, 6),
            "n_low": round(ts.n_low, 6),
            "n_high": round(ts.n_high, 6),
        })

    # ── P 勢(Macro): namespace hit distribution prior ──

    # P-layer slow EMA decay: every N updates, multiply all
    # hits by decay factor. Keeps P slow-moving (per ZIQ axiom 1)
    # while preventing ancient history from dominating.
    P_DECAY_INTERVAL = 100  # Apply decay every N feedback rounds
    P_DECAY_FACTOR = 0.95   # Retain 95%, forget 5%

    def _load_macro(self) -> dict[str, float]:
        """Load P-layer namespace hit counts (float for decay)."""
        default = {
            ns: 0.0 for ns in
            ["knowledge", "memory", "cognition",
             "skills", "context"]
        }
        raw = self._store.read(
            _NS, "macro_hits", default=default,
        )
        # Migrate int → float if needed
        return {k: float(v) for k, v in raw.items()}

    def _save_macro(self, hits: dict[str, float]) -> None:
        """Persist P-layer namespace hit counts."""
        self._store.write(
            _NS, "macro_hits",
            {k: round(v, 4) for k, v in hits.items()},
        )

    def _macro_prior(self) -> list[str]:
        """P 勢: namespaces ranked by historical hit rate.

        Slow-moving environment statistic (NOT FTRL — that's M's
        job per ZIQ axiom #1). Uses simple counting with
        periodic exponential decay to handle non-stationarity
        without becoming reactive.
        """
        hits = self._load_macro()
        total = sum(hits.values())
        if total < 10:
            return ["memory", "cognition"]
        ranked = sorted(
            hits.items(), key=lambda x: x[1], reverse=True,
        )
        return [ns for ns, _ in ranked]

    def _classify_source(self, file_path: str) -> str:
        """Classify a file path into source type."""
        fp = file_path.lower().replace("\\", "/")
        if "correction" in fp or "learning" in fp:
            return "correction"
        if "rule" in fp or "00-l0" in fp or "l1/" in fp:
            return "rule"
        if "skill" in fp or "kb_" in fp:
            return "skill"
        if "handoff" in fp or "交接" in fp:
            return "handoff"
        if "memory" in fp:
            return "memory"
        return "correction"

    @staticmethod
    def _source_to_namespace(source_type: str) -> str:
        """Map source type → routing namespace."""
        return {
            "correction": "memory",
            "memory": "memory",
            "handoff": "memory",
            "rule": "cognition",
            "skill": "skills",
        }.get(source_type, "memory")

    def compute_meso_scores(
        self,
        query: str,
        rag,
        top_k: int = 20,
    ) -> dict[str, float]:
        """T 張: per-namespace causal signal via RAG search.

        Runs full RAG search and aggregates max score by
        target namespace. Returns dict suitable for passing
        to route_query(meso_scores=...).

        This is the real T layer — uses causal signal from a
        third-party retriever, not keyword heuristics. Cost is
        one vector search (~10-50ms with ChromaDB).

        Args:
            query: The search query text.
            rag: A RAG instance with .search(query, top_k) method.
            top_k: How many results to fetch for aggregation.

        Returns:
            Dict mapping namespace → max score from results.
            Empty dict if RAG returns no results.
        """
        try:
            results = rag.search(query, top_k=top_k, min_score=0.0)
        except Exception:
            return {}

        if not results:
            return {}

        scores: dict[str, float] = {}
        for r in results:
            file_path = r.get("file", "")
            source_type = self._classify_source(file_path)
            ns = self._source_to_namespace(source_type)
            cur_score = float(r.get("score", 0.0))
            if ns not in scores or cur_score > scores[ns]:
                scores[ns] = cur_score
        return scores

    def get_weights(self) -> dict[str, float]:
        """Get current source-type weights (for debugging/display)."""
        states = self._load_states()
        return {stype: round(state.weight, 3) for stype, state in states.items()}

    def rerank(self, results: list[dict]) -> list[dict]:
        """Rerank RAG results using FTRL-learned source weights.

        Args:
            results: List of RAG result dicts with keys: text, file, score.

        Returns:
            Same results with adjusted scores, re-sorted.
        """
        if not results:
            return results

        # Shallow-copy each dict to avoid mutating caller's data
        results = [dict(r) for r in results]

        states = self._load_states()

        # Apply FTRL weights to scores
        for r in results:
            source_type = self._classify_source(r.get("file", ""))
            weight = states[source_type].weight
            original_score = r.get("score", 0.5)
            r["ziq_score"] = round(original_score * weight, 4)
            r["source_type"] = source_type
            # Track hit
            states[source_type].hits += 1

        self._save_states(states)

        # R1 fix: store this round's results for causal feedback
        self._last_rerank_results = list(results)

        # Sort by ZIQ-adjusted score (descending)
        results.sort(key=lambda x: x.get("ziq_score", 0), reverse=True)
        return results

    def feedback(self, used_files: list[str]) -> None:
        """Record which results were actually used (implicit feedback).

        Call this when the agent reads a file that was in RAG results.
        Positive gradient for used source types, negative for unused.

        Args:
            used_files: File paths that were actually read/used.
        """
        states = self._load_states()

        used_types = set()
        for fp in used_files:
            stype = self._classify_source(fp)
            used_types.add(stype)
            states[stype].used += 1

        # R1 fix: only penalize source types that appeared THIS round
        # (not all historically active types)
        this_round = {
            self._classify_source(r.get("file", ""))
            for r in self._last_rerank_results
        } if hasattr(self, "_last_rerank_results") else set()

        for stype in SOURCE_TYPES:
            if stype not in this_round and stype not in used_types:
                continue  # Skip types not involved this round
            reward = 0.3 if stype in used_types else -0.1
            states[stype] = _ema_update(states[stype], reward)
            # Weight decay every 20 feedback rounds: regress toward 1.0
            total = states[stype].hits
            if total > 0 and total % 20 == 0:
                states[stype].weight = (
                    0.95 * states[stype].weight + 0.05 * 1.0
                )

        self._save_states(states)

    def route_query(
        self,
        query: str,
        confidence: float,
        meso_scores: dict[str, float] | None = None,
    ) -> list[str]:
        """ZIQ PTME routing: P(prior) + T(meso) + M(FTRL) + E(decide).

        P 勢: namespace hit distribution prior (learned from history)
        T 張: reranker confidence (off by default, enable via meso_scores)
        M 記: FTRL-Proximal threshold learning
        E 決: threshold comparison → route + freeze

        Keyword matching was ablation-killed (Δ=+5.48pp when removed).

        Args:
            query: The search query text.
            confidence: ZIQ alpha_t uncertainty (0=certain, 1=unknown).
            meso_scores: Optional T-layer causal signal. Dict mapping
                namespace → confidence score. None = T disabled.

        Returns:
            List of namespace keys to search.
        """
        all_ns = [
            "knowledge", "memory", "cognition", "skills", "context",
        ]
        ts = self._load_thresholds()
        prior = self._macro_prior()  # P 勢

        self._last_route_confidence = confidence
        self._last_route_thresholds = (ts.low, ts.high)

        # E 決: high uncertainty → search all
        if confidence > ts.high:
            self._last_route_breadth = "all"
            return all_ns

        # E 決: low uncertainty → narrow (1 namespace)
        if confidence < ts.low:
            if meso_scores:
                # T 張 ON: causal signal picks best
                best = max(meso_scores, key=meso_scores.get)
                if best in all_ns:
                    self._last_route_breadth = "narrow"
                    self._last_route_result = [best]
                    return [best]
            # T OFF → P 勢: use historical prior
            best = prior[0] if prior else "memory"
            self._last_route_breadth = "narrow"
            self._last_route_result = [best]
            return [best]

        # E 決: medium uncertainty → 2-3 namespaces
        if meso_scores:
            # T 張 ON: top 2-3 by causal score
            ranked = sorted(
                meso_scores.items(),
                key=lambda x: x[1],
                reverse=True,
            )
            selected = [
                ns for ns, _ in ranked[:3] if ns in all_ns
            ]
            if len(selected) < 2:
                selected = prior[:2]
        else:
            # T OFF → P 勢: top 2 by historical prior
            selected = prior[:2]

        self._last_route_breadth = "medium"
        self._last_route_result = selected
        return selected

    def route_feedback(
        self,
        correct_namespaces: list[str],
    ) -> ThresholdState:
        """FTRL-Proximal threshold update (McMahan 2013).

        Scope: policy-layer threshold FTRL (NOT v6.1 split causal
        FTRL). See module docstring "v6.1 production status".

        Real FTRL with O(√T) regret bound, not ad-hoc EMA.
        ZIQ framework uses FTRL as the online learning engine.

        Per-coordinate update (simplified, no L1 for 2 params):
          σ_t = (√(n_{t}) - √(n_{t-1})) / α
          z_t += g_t - σ_t * w_t
          w_{t+1} = -z_t / (λ₂ + √(n_t) / α)

        where g_t = gradient from routing outcome,
        n_t = cumulative squared gradient.

        Also includes:
        - Medium feedback (red-team #3 fix #1)
        - Band-width regularization (fix #2)

        Args:
            correct_namespaces: Namespace(s) that actually had
                the useful result.

        Returns:
            Updated ThresholdState (also persisted).
        """
        ts = self._load_thresholds()
        breadth = getattr(self, "_last_route_breadth", "medium")
        routed = getattr(self, "_last_route_result", [])

        # FTRL hyperparams
        alpha = THRESHOLD_LR  # Learning rate scaling
        lambda2 = 0.01        # L2 regularization (stability)

        # ── Compute gradient g for (low, high) ──
        g_low = 0.0
        g_high = 0.0

        if breadth == "narrow":
            hit = any(ns in routed for ns in correct_namespaces)
            if not hit:
                # Miss → gradient pushes low DOWN (broaden)
                g_low = 1.0
            else:
                # Hit → gradient pushes low UP (tighten), weaker
                g_low = -0.2

        elif breadth == "medium":
            hit = any(ns in routed for ns in correct_namespaces)
            n_correct = len(correct_namespaces)
            if not hit:
                # Medium missed → push high DOWN (go broad sooner)
                g_high = 1.0
            elif n_correct == 1 and len(routed) >= 3:
                # Too broad for medium → push low UP
                g_low = -0.3

        elif breadth == "all":
            useful_frac = len(correct_namespaces) / 5.0
            if useful_frac < 0.4:
                # Waste → push high UP (less broad)
                g_high = -1.0
            else:
                # Justified → push high DOWN (keep broad)
                g_high = 0.3

        # ── FTRL-Proximal per-coordinate update ──
        for param in ("low", "high"):
            g = g_low if param == "low" else g_high
            if g == 0.0:
                continue

            # Get current accumulators
            z = ts.z_low if param == "low" else ts.z_high
            n = ts.n_low if param == "low" else ts.n_high
            w = ts.low if param == "low" else ts.high

            # Update n (cumulative squared gradient)
            n_new = n + g * g

            # Compute σ (per-coordinate LR adjustment)
            sigma = (math.sqrt(n_new) - math.sqrt(n)) / alpha

            # Update z (adjusted cumulative gradient)
            z_new = z + g - sigma * w

            # Compute new weight
            # w = -z / (λ₂ + √n / α)
            denom = lambda2 + math.sqrt(n_new) / alpha
            w_new = -z_new / denom

            # Clamp to valid range
            if param == "low":
                w_new = max(THRESHOLD_MIN, min(THRESHOLD_MAX, w_new))
                ts.low = w_new
                ts.z_low = z_new
                ts.n_low = n_new
            else:
                w_new = max(THRESHOLD_HIGH_MIN, min(THRESHOLD_HIGH_MAX, w_new))
                ts.high = w_new
                ts.z_high = z_new
                ts.n_high = n_new

        # Band-width regularization (ZIQ layer on top of FTRL)
        band = ts.high - ts.low
        default_band = (
            DEFAULT_HIGH_THRESHOLD - DEFAULT_LOW_THRESHOLD
        )
        if band < default_band * 0.5:
            restore = 0.02
            ts.low = max(THRESHOLD_MIN, ts.low - restore)
            ts.high = min(
                THRESHOLD_HIGH_MAX, ts.high + restore,
            )

        # Hard margin safety net
        if ts.low >= ts.high - 0.10:
            ts.low = ts.high - 0.10

        ts.n_updates += 1
        self._save_thresholds(ts)

        # P 勢: slow accumulator with periodic decay
        macro = self._load_macro()
        # Apply decay every N updates (slow-moving non-stationarity)
        if (ts.n_updates % self.P_DECAY_INTERVAL) == 0:
            for ns in macro:
                macro[ns] *= self.P_DECAY_FACTOR
        # Accumulate this round's hits
        for ns in correct_namespaces:
            if ns in macro:
                macro[ns] += 1.0
        self._save_macro(macro)

        return ts

    def get_thresholds(self) -> ThresholdState:
        """Get current adaptive routing thresholds."""
        return self._load_thresholds()

    def stats(self) -> dict:
        """Return stats for debugging/telemetry."""
        states = self._load_states()
        ts = self._load_thresholds()
        source_stats = {
            stype: {
                "weight": round(s.weight, 3),
                "hits": s.hits,
                "used": s.used,
                "use_rate": round(s.used / max(s.hits, 1), 2),
            }
            for stype, s in states.items()
        }
        source_stats["_thresholds"] = {
            "low": round(ts.low, 4),
            "high": round(ts.high, 4),
            "n_updates": ts.n_updates,
        }
        return source_stats


def rerank_results(
    cache_dir: str, results: list[dict],
) -> list[dict]:
    """Convenience: rerank RAG results with ZIQ adaptive weights."""
    return ZIQRetrieval(cache_dir).rerank(results)


def record_feedback(
    cache_dir: str, used_files: list[str],
) -> None:
    """Convenience: record feedback for ZIQ weight learning."""
    ZIQRetrieval(cache_dir).feedback(used_files)
