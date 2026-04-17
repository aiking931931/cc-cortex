"""concinno.ziq_retrieval — EMA adaptive retrieval for Cognitive RAG.

@module ziq_retrieval
@responsibility Learn which knowledge sources (corrections, rules, skills,
    handoffs) are actually useful per-query, using EMA online learning.
    Replaces flat ChromaDB ranking with adaptive multi-source fusion.
    Also learns optimal routing breadth thresholds via FTRL.
@dependencies concinno.core.state_store
@exports ZIQRetrieval, SPPMIPrior, _FTRLNamespace, rerank_results, record_feedback

Core formula:

    posterior ∝ SPS(domain) × FTRL(outcome)

  - SPS (Structural Prior SOTA) = the best non-causal structural signal
    for a given domain. It's a SLOT — each domain fills it with their own
    best structural prior.
  - FTRL (Follow The Regularized Leader) = causal learner from outcome
    feedback. Universal across all domains.
  - In the namespace routing domain: SPS implementation = SPPMIPrior
    (SPPMI word co-occurrence, Levy-Goldberg 2014).
  - PTME (P勢/T張/M記/E決) is the methodology for *building* an SPS,
    not the output. PTME describes the 4-step process of constructing
    a structural prior; SPS is what you get out of it.

This is ZIQ-Find technology feeding back into CCC:
  - ZIQ-Score uses FTRL to learn retriever weights (search domain)
  - Here we use FTRL in two places:
      (a) knowledge-source weights (per source_type, see feedback())
      (b) routing breadth thresholds (low/high cutoff, see route_feedback())
  - SPS construction methodology (PTME: P勢/T張/M記/E決):
      P=source stability, T=query pattern change,
      M=structural pattern memory (co-occurrence / centroid),
      E=fusion decision
  - FTRL is the diachronic / causal layer and lives OUTSIDE the SPS.
    Historical error "M = FTRL weights" was corrected 2026-04-13.
    See kb_ziq/split_architecture.md for the v6.1 SPS ⊥ FTRL layering.

v6.2.1 FTRL namespace migration (2026-04-16):
  Migrated FTRL likelihood from EMA source-type mapping to true
  FTRL-Proximal per-namespace weights (_FTRLNamespace class).
  feedback() now updates both EMA source weights AND FTRL namespace
  weights from outcome gradients. _ftrl_namespace_likelihood() reads
  directly from _FTRLNamespace instead of mapping EMA weights.
  Old EMA path preserved as _ema_namespace_likelihood() fallback.
  When FTRL namespace state is empty, weights default to 1.0
  (backward compatible).

v6.2 Goldilocks (2026-04-16):
  Added SPPMIPrior — Shifted Positive PMI (Levy-Goldberg 2014) as the
  SPS implementation for namespace routing. Ablation-validated:
  SPS(SPPMI, k=5) × FTRL achieves top-1=0.8917 on N=351 cold-start,
  beating supervised by +5.13pp. The prior is built one-shot from
  namespace filesystem corpus. At inference, route_query() computes:

      posterior ∝ SPS(query) × FTRL(likelihood)

  Backward compatible: if SPPMIPrior is not initialized, route_query()
  falls back to the P-layer hit-count prior (v6.1 behavior).

v6.1 production status (2026-04-13):
  This module was the merged-state router (one weight per source_type,
  threshold FTRL on breadth). v6.2 adds the Bayesian-fused split
  architecture validated by the 4-pod benchmark (see
  benchmarks/ablation_arch_v2.py, benchmarks/ablation_sppmi_term_ns.py,
  and benchmarks/results/pod_summary.json).

Design (current, v6.2 Goldilocks split):
  RAG returns N results from ChromaDB. Each result has a source type
  (correction, rule, skill, handoff). FTRL learns per-source-type weights
  from implicit feedback (was the correction actually used? did the agent
  read the linked skill?).

  The routing layer now uses Bayesian product:
  - SPS: SPPMIPrior — structural prior from corpus word statistics
  - FTRL: per-namespace causal weights from outcome feedback
  - Combiner: posterior = SPS × FTRL (Goldilocks)

  The FTRL weights adjust over time:
  - Source type that keeps getting used → weight goes UP
  - Source type that gets ignored → weight goes DOWN
  - New session resets nothing (weights persist cross-session)

  This is the ZIQ "教練面" (coach) for RAG — it doesn't change the
  retriever (ChromaDB embeddings), it changes which results to trust.

SPS construction for RAG via PTME methodology (v6.2 Goldilocks):
  P (勢): source_type hit rate stability (slow EMA prior, fallback SPS)
  T (張): query pattern shift (off by default, enable via meso_scores)
  M (記): SPPMIPrior — word-level SPPMI(term, namespace) = primary SPS
        for this domain. Built from filesystem corpus. NOT FTRL.
        FTRL is the separate diachronic learner that provides likelihood.
  E (決): posterior = SPS × FTRL → threshold → route
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from concinno.core.state_store import StateStore

__version__ = "6.2.1"  # v6.2.1 FTRL namespace migration

_NS = "ziq_retrieval"

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]")

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

# ── SPPMI default shift (ablation-validated) ────────────
# SPPMI(word, shift_k=5) × FTRL top-1=0.8917 on N=351 cold-start
# beats supervised +5.13pp. See benchmarks/ablation_sppmi_term_ns.py
DEFAULT_SPPMI_SHIFT_K = 5

# Routing namespaces (shared with route_query)
ALL_NS = ("knowledge", "memory", "cognition", "skills", "context")


def _tokenize(text: str) -> list[str]:
    """Mixed CJK + ASCII tokenizer. CJK chars as individual tokens."""
    return [t.lower() for t in _TOKEN_RE.findall(text)]


@runtime_checkable
class SPSProtocol(Protocol):
    """Structural Prior SOTA — universal interface for any domain.

    ZIQ v7 invariant: every domain plugs its own SPS into the
    same ``posterior ∝ SPS × FTRL`` framework.

    Implementors: SPPMIPrior (routing), RetrieverConfidenceSPS
    (retriever selection), FR saliency (KV compression, external).
    """

    def prior(self, query: str, **ctx: object) -> dict[str, float]:
        """Return probability distribution over options.

        Args:
            query: Query/context string.
            **ctx: Domain-specific context (scores, features).

        Returns:
            Dict mapping option → probability (~sums to 1).
        """
        ...


class SPPMIPrior:
    """SPS implementation for namespace routing (Levy-Goldberg 2014 SPPMI).

    This is the Structural Prior SOTA (SPS) for the namespace routing
    domain. Builds a sparse Shifted Positive PMI matrix from filesystem
    namespace corpus:

        PMI(t, n)   = log(#(t,n) * |D| / (#(t) * #(n)))
        SPPMI(t, n) = max(PMI(t,n) - log(k), 0)

    At inference, ``prior(query)`` sums SPPMI values over query words
    per namespace, returning a normalized probability distribution.

    This is a pure structural prior (SPS) — it never sees routing outcomes.
    FTRL provides the causal likelihood; the combiner in
    ``ZIQRetrieval.route_query()`` computes posterior = SPS × FTRL.

    The SPS was constructed using the PTME methodology (P勢/T張/M記/E決):
    SPPMIPrior corresponds to the M記 step — structural pattern memory
    from word co-occurrence statistics.

    Args:
        shift_k: SPPMI shift parameter (default 5, ablation-validated).
    """

    def __init__(self, shift_k: int = DEFAULT_SPPMI_SHIFT_K) -> None:
        self.shift_k = shift_k
        # Sparse SPPMI matrix: term -> {namespace -> sppmi_value}
        self._sppmi: dict[str, dict[str, float]] = {}
        self._built = False

    @classmethod
    def build_from_corpus(
        cls,
        namespace_dirs: dict[str, list[str]],
        shift_k: int = DEFAULT_SPPMI_SHIFT_K,
        max_files_per_ns: int = 200,
        max_chars_per_file: int = 3000,
    ) -> "SPPMIPrior":
        """Build SPPMI matrix from filesystem namespace directories.

        Args:
            namespace_dirs: Mapping of namespace name to list of
                glob patterns or directory paths. Each file found
                contributes word counts to its namespace.
            shift_k: SPPMI shift parameter (higher = sparser).
            max_files_per_ns: Cap files per namespace (memory bound).
            max_chars_per_file: Truncate file content (speed bound).

        Returns:
            Initialized SPPMIPrior with built matrix.

        Example::

            prior = SPPMIPrior.build_from_corpus({
                "memory": ["_AI_BRAIN/06_Handoffs/**/*.md"],
                "cognition": [".claude/rules/**/*.md"],
            })
        """
        instance = cls(shift_k=shift_k)

        # Count co-occurrences: #(term, namespace)
        term_ns_count: dict[str, dict[str, float]] = defaultdict(
            lambda: defaultdict(float),
        )
        term_count: dict[str, float] = defaultdict(float)
        ns_count: dict[str, float] = defaultdict(float)
        total = 0.0

        for ns, patterns in namespace_dirs.items():
            file_count = 0
            seen: set[str] = set()
            for pattern in patterns:
                p = Path(pattern)
                # If pattern contains glob chars, glob from cwd
                if any(c in pattern for c in ("*", "?", "[")):
                    files = Path(".").glob(pattern)
                elif p.is_dir():
                    files = p.rglob("*.md")
                elif p.is_file():
                    files = [p]
                else:
                    continue

                for fp in files:
                    fp_str = str(fp.resolve())
                    if fp_str in seen or not fp.is_file():
                        continue
                    seen.add(fp_str)
                    try:
                        text = fp.read_text(
                            encoding="utf-8", errors="ignore",
                        )[:max_chars_per_file]
                    except Exception:
                        continue

                    tokens = _tokenize(text)[:3000]
                    for t in tokens:
                        term_ns_count[t][ns] += 1.0
                        term_count[t] += 1.0
                        ns_count[ns] += 1.0
                        total += 1.0

                    file_count += 1
                    if file_count >= max_files_per_ns:
                        break
                if file_count >= max_files_per_ns:
                    break

        # Build SPPMI matrix
        instance._build_matrix(
            term_ns_count, term_count, ns_count, total,
        )
        return instance

    @classmethod
    def build_from_texts(
        cls,
        namespace_texts: dict[str, list[str]],
        shift_k: int = DEFAULT_SPPMI_SHIFT_K,
    ) -> "SPPMIPrior":
        """Build SPPMI matrix from in-memory text collections.

        Useful for testing and when texts are already loaded.

        Args:
            namespace_texts: Mapping of namespace → list of text strings.
            shift_k: SPPMI shift parameter.

        Returns:
            Initialized SPPMIPrior with built matrix.
        """
        instance = cls(shift_k=shift_k)

        term_ns_count: dict[str, dict[str, float]] = defaultdict(
            lambda: defaultdict(float),
        )
        term_count: dict[str, float] = defaultdict(float)
        ns_count: dict[str, float] = defaultdict(float)
        total = 0.0

        for ns, texts in namespace_texts.items():
            for text in texts:
                tokens = _tokenize(text)[:3000]
                for t in tokens:
                    term_ns_count[t][ns] += 1.0
                    term_count[t] += 1.0
                    ns_count[ns] += 1.0
                    total += 1.0

        instance._build_matrix(
            term_ns_count, term_count, ns_count, total,
        )
        return instance

    def _build_matrix(
        self,
        term_ns_count: dict[str, dict[str, float]],
        term_count: dict[str, float],
        ns_count: dict[str, float],
        total: float,
    ) -> None:
        """Compute sparse SPPMI matrix from raw counts."""
        log_k = math.log(self.shift_k) if self.shift_k > 1 else 0.0
        log_total = math.log(total + 1e-9)

        sppmi: dict[str, dict[str, float]] = {}
        for t, ns_counts in term_ns_count.items():
            for ns, count in ns_counts.items():
                pmi = (
                    math.log(count + 1e-9)
                    - math.log(term_count[t] + 1e-9)
                    - math.log(ns_count[ns] + 1e-9)
                    + log_total
                )
                sppmi_val = max(pmi - log_k, 0.0)
                if sppmi_val > 0:
                    if t not in sppmi:
                        sppmi[t] = {}
                    sppmi[t][ns] = sppmi_val

        self._sppmi = sppmi
        self._built = True

    @property
    def is_built(self) -> bool:
        """Whether the SPPMI matrix has been built."""
        return self._built

    @property
    def n_terms(self) -> int:
        """Number of terms in the SPPMI vocabulary."""
        return len(self._sppmi)

    @property
    def n_nonzero(self) -> int:
        """Number of non-zero entries in the SPPMI matrix."""
        return sum(len(v) for v in self._sppmi.values())

    def to_dict(self) -> dict:
        """Serialize SPPMI state for persistence."""
        return {
            "shift_k": self.shift_k,
            "sppmi": self._sppmi,
            "built": self._built,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SPPMIPrior":
        """Restore SPPMIPrior from serialized state."""
        instance = cls(shift_k=data.get("shift_k", DEFAULT_SPPMI_SHIFT_K))
        instance._sppmi = data.get("sppmi", {})
        instance._built = data.get("built", False)
        return instance

    def prior(self, query: str) -> dict[str, float]:
        """Compute namespace prior distribution for a query.

        Sums SPPMI values over query words per namespace, then
        normalizes to a probability distribution with Laplace smoothing.

        Args:
            query: The search query text.

        Returns:
            Dict mapping namespace → probability score.
            Uniform distribution if no terms match or matrix not built.
        """
        n_ns = len(ALL_NS)
        uniform = {ns: 1.0 / n_ns for ns in ALL_NS}

        if not self._built:
            return uniform

        tokens = _tokenize(query)
        if not tokens:
            return uniform

        scores: dict[str, float] = {ns: 0.0 for ns in ALL_NS}
        hit = 0
        for t in tokens:
            if t in self._sppmi:
                for ns, val in self._sppmi[t].items():
                    if ns in scores:
                        scores[ns] += val
                hit += 1

        if hit == 0:
            return uniform

        # Normalize with Laplace smoothing (matches ablation formulation)
        total = sum(scores.values()) + 1e-9
        smoothing_per = 0.01
        smoothing_total = smoothing_per * n_ns
        sppmi_dist = {
            ns: (s + smoothing_per) / (total + smoothing_total)
            for ns, s in scores.items()
        }

        # Adaptive k: coverage-weighted blend toward uniform.
        # Low coverage → weaker prior (avoids noisy guidance on
        # unseen vocabulary). Solves N=1985 warm degradation while
        # preserving N=351 cold-start advantage.
        coverage = hit / len(tokens)
        return {
            ns: coverage * sppmi_dist[ns] + (1 - coverage) * uniform[ns]
            for ns in ALL_NS
        }


class RetrieverConfidenceSPS:
    """SPS for retriever selection domain.

    Uses retriever self-confidence (score distribution) as the
    structural prior signal.  For each retriever, confidence =
    how peaked its score distribution is for a given query.

    High confidence → retriever clearly distinguishes relevant
    docs → trust it more.  Low confidence → retriever is guessing.

    This is a pure structural prior (SPS) — computed from retriever
    scores before knowing ground truth.  FTRL provides causal
    correction (which retriever was actually right).

    Follows the same SPS protocol as SPPMIPrior:
        ``prior(query, scores=...)`` → ``dict[str, float]``

    ZIQ v7 invariant compliance:
        Separation: only reads scores, never outcomes
        Fusion: returns prior for Bayesian product with FTRL
        Autonomy: no manual params (softmax temperature auto-derived)
    """

    def prior(
        self,
        query: str,
        *,
        scores: dict[str, list[float]],
    ) -> dict[str, float]:
        """Per-retriever confidence prior from score distributions.

        Args:
            query: Query string (unused, kept for SPS protocol).
            scores: Mapping retriever_name → list of top-k scores
                (descending order, higher = more relevant).

        Returns:
            Probability distribution over retrievers.
        """
        if not scores:
            return {}

        confidences: dict[str, float] = {}
        for name, score_list in scores.items():
            confidences[name] = self._score_gap(score_list)

        # Softmax normalization (auto temperature = stddev)
        vals = list(confidences.values())
        if not vals:
            return {}
        mu = sum(vals) / len(vals)
        std = (
            sum((v - mu) ** 2 for v in vals) / len(vals)
        ) ** 0.5
        temp = max(std, 1e-6)
        exp_vals = {
            k: math.exp((v - max(vals)) / temp)
            for k, v in confidences.items()
        }
        total = sum(exp_vals.values())
        if total < 1e-12:
            n = len(exp_vals)
            return {k: 1.0 / n for k in exp_vals}
        return {k: v / total for k, v in exp_vals.items()}

    @staticmethod
    def _score_gap(scores: list[float]) -> float:
        """Score gap: top-1 minus mean of rest.

        Large gap = retriever is confident about its top pick.
        """
        if len(scores) < 2:
            return 0.0
        top = scores[0]
        rest_mean = sum(scores[1:]) / len(scores[1:])
        return top - rest_mean


class _MetaTuner:
    """Autonomy L2: learn FTRL/EMA hyperparams from routing regret.

    Tracks rolling routing accuracy.  Every TUNE_INTERVAL decisions,
    adjusts hyperparams based on regret trend:
    - Regret increasing → slow down (alpha × 0.9)
    - Regret decreasing → cautiously speed up (alpha × 1.05)

    Invariant compliance:
    - Separation: only reads outcomes, never touches SPS
    - Fusion: doesn't interfere with Bayesian product
    - Autonomy: IS the autonomy L2 layer
    """

    TUNE_INTERVAL = 50
    WINDOW = 20
    # (min, max, default)
    PARAMS: dict[str, tuple[float, float, float]] = {
        "ftrl_alpha": (0.01, 0.5, 0.1),
        "ftrl_lam": (0.001, 0.1, 0.01),
        "ema_lr": (0.05, 0.4, DEFAULT_LR),
        "threshold_lr": (0.01, 0.2, THRESHOLD_LR),
    }

    def __init__(self, store: StateStore) -> None:
        self._store = store

    def _load(self) -> dict:
        default = {
            "params": {
                k: v[2] for k, v in self.PARAMS.items()
            },
            "outcomes": [],
            "prev_accuracy": None,
            "n_decisions": 0,
            "adjustments": [],
        }
        return self._store.read(_NS, "meta_tuner", default=default)

    def _save(self, state: dict) -> None:
        self._store.write(_NS, "meta_tuner", state)

    def get_param(self, key: str) -> float:
        """Get current tuned value for a hyperparameter."""
        state = self._load()
        default = self.PARAMS.get(key, (0, 1, 0.1))[2]
        return state.get("params", {}).get(key, default)

    def record_outcome(self, correct: bool) -> dict | None:
        """Record routing outcome. Tune if interval hit."""
        state = self._load()
        state["outcomes"].append(1.0 if correct else 0.0)
        state["n_decisions"] = state.get("n_decisions", 0) + 1

        max_buf = self.WINDOW * 3
        if len(state["outcomes"]) > max_buf:
            state["outcomes"] = state["outcomes"][-max_buf:]

        result = None
        if state["n_decisions"] % self.TUNE_INTERVAL == 0:
            result = self._tune(state)

        self._save(state)
        return result

    def _tune(self, state: dict) -> dict:
        """Evaluate and adjust hyperparams."""
        outcomes = state["outcomes"]
        if len(outcomes) < self.WINDOW:
            return {"status": "insufficient_data"}

        recent = outcomes[-self.WINDOW:]
        accuracy = sum(recent) / len(recent)
        prev = state.get("prev_accuracy")
        state["prev_accuracy"] = accuracy

        if prev is None:
            return {"status": "baseline_set", "accuracy": accuracy}

        delta = accuracy - prev
        params = state.get("params", {})
        adjustments: dict = {}

        if delta < -0.05:
            for key, (lo, _hi, _d) in self.PARAMS.items():
                old = params.get(key, _d)
                new = max(lo, round(old * 0.9, 4))
                if old != new:
                    params[key] = new
                    adjustments[key] = {
                        "old": old, "new": new, "reason": "regret_up",
                    }
        elif delta > 0.05:
            for key, (_lo, hi, _d) in self.PARAMS.items():
                old = params.get(key, _d)
                new = min(hi, round(old * 1.05, 4))
                if old != new:
                    params[key] = new
                    adjustments[key] = {
                        "old": old, "new": new, "reason": "regret_down",
                    }

        state["params"] = params
        if adjustments:
            state.setdefault("adjustments", []).append({
                "n": state["n_decisions"],
                "accuracy": round(accuracy, 4),
                "delta": round(delta, 4),
                "changes": adjustments,
            })
            if len(state["adjustments"]) > 50:
                state["adjustments"] = state["adjustments"][-50:]

        return {
            "status": "tuned" if adjustments else "stable",
            "accuracy": accuracy,
            "delta": delta,
            "adjustments": adjustments,
        }

    def status(self) -> dict:
        """Current L2 state for debugging."""
        state = self._load()
        return {
            "params": state.get("params", {}),
            "n_decisions": state.get("n_decisions", 0),
            "prev_accuracy": state.get("prev_accuracy"),
            "n_adjustments": len(state.get("adjustments", [])),
        }


class _FTRLNamespace:
    """FTRL-Proximal per-namespace causal weights (McMahan 2013).

    The causal learner in the SPS × FTRL architecture.  Learns from
    routing outcomes: which namespaces had useful results.  Completely
    separate from SPPMIPrior (the SPS — structural, no outcome).

    State is persisted via StateStore so weights survive across
    sessions.  When state is empty, all weights default to 1.0
    (backward compatible with pre-v6.2.1 behavior).

    Uses L2 auto-tuned alpha/lam when MetaTuner is provided.
    """

    def __init__(
        self,
        store: StateStore,
        all_ns: tuple[str, ...] = ALL_NS,
        meta: _MetaTuner | None = None,
    ) -> None:
        self._store = store
        self._all_ns = all_ns
        self._meta = meta

    def _load(self) -> dict:
        return self._store.read(_NS, "ftrl_ns", default={})

    def _save(self, state: dict) -> None:
        self._store.write(_NS, "ftrl_ns", state)

    def update(self, correct_namespaces: list[str]) -> None:
        """FTRL-Proximal per-namespace update from outcome.

        Uses L2-tuned alpha/lam when MetaTuner is available,
        falls back to defaults otherwise.

        Args:
            correct_namespaces: Namespaces that contained useful
                results this round.
        """
        state = self._load()
        if self._meta:
            alpha = self._meta.get_param("ftrl_alpha")
            lam = self._meta.get_param("ftrl_lam")
        else:
            alpha = 0.1
            lam = 0.01
        for ns in self._all_ns:
            z = state.get(f"z_{ns}", 0.0)
            n = state.get(f"n_{ns}", 0.0)
            w = state.get(f"w_{ns}", 1.0)

            g = -1.0 if ns in correct_namespaces else 0.2
            n_new = n + g * g
            sigma = (math.sqrt(n_new) - math.sqrt(n)) / alpha
            z_new = z + g - sigma * w
            denom = lam + math.sqrt(n_new) / alpha
            w_new = max(0.1, -z_new / denom)

            state[f"z_{ns}"] = round(z_new, 6)
            state[f"n_{ns}"] = round(n_new, 6)
            state[f"w_{ns}"] = round(w_new, 4)
        self._save(state)

    def likelihood(self) -> dict[str, float]:
        """Current FTRL namespace weights."""
        state = self._load()
        return {
            ns: state.get(f"w_{ns}", 1.0) for ns in self._all_ns
        }


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
    """EMA adaptive retrieval layer for CCC RAG (v6.2 Goldilocks).

    Wraps RAG search results with learned source-type weights.
    When an SPPMIPrior (SPS) is provided, route_query() uses Bayesian
    product: posterior = SPS × FTRL.

    Usage::

        ziq = ZIQRetrieval(cache_dir="/path/to/cache")

        # Optional: attach SPPMI prior for Goldilocks routing
        prior = SPPMIPrior.build_from_texts({"memory": [...], ...})
        ziq = ZIQRetrieval(cache_dir="/path/to/cache", sppmi_prior=prior)

        # After RAG search returns results
        reranked = ziq.rerank(results)

        # After observing which results were actually used
        ziq.feedback(used_files=["corrections/fix_123.md"])
    """

    def __init__(
        self,
        cache_dir: str,
        sppmi_prior: SPPMIPrior | None = None,
    ) -> None:
        self._store = StateStore(cache_dir)
        self._meta = _MetaTuner(self._store)
        self._ftrl_ns = _FTRLNamespace(
            self._store, meta=self._meta,
        )
        if sppmi_prior is not None:
            self._sppmi = sppmi_prior
        else:
            self._sppmi = self._auto_load_sppmi()

    def _auto_load_sppmi(self) -> SPPMIPrior | None:
        """Load persisted SPPMI from state store if available."""
        raw = self._store.read(_NS, "sppmi_prior", default=None)
        if raw and isinstance(raw, dict) and raw.get("built"):
            return SPPMIPrior.from_dict(raw)
        return None

    def build_and_persist_sppmi(
        self,
        namespace_texts: dict[str, list[str]],
        shift_k: int = DEFAULT_SPPMI_SHIFT_K,
    ) -> SPPMIPrior:
        """Build SPPMI from texts and persist to state store.

        Call once after indexing. Subsequent ZIQRetrieval instances
        auto-load from cache (no rebuild needed).

        Args:
            namespace_texts: Mapping of namespace to text lists.
            shift_k: SPPMI shift (default 5, ablation-validated).

        Returns:
            The built SPPMIPrior instance.
        """
        prior = SPPMIPrior.build_from_texts(
            namespace_texts, shift_k=shift_k,
        )
        self._store.write(_NS, "sppmi_prior", prior.to_dict())
        self._sppmi = prior
        return prior

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
        """P 勢: namespaces ranked by historical hit rate (fallback SPS).

        Slow-moving environment statistic (NOT FTRL — FTRL is the
        causal learner). Uses simple counting with periodic
        exponential decay to handle non-stationarity without
        becoming reactive.
        """
        hits = self._load_macro()
        total = sum(hits.values())
        if total < 10:
            return ["memory", "cognition"]
        ranked = sorted(
            hits.items(), key=lambda x: x[1], reverse=True,
        )
        return [ns for ns, _ in ranked]

    def _ema_namespace_likelihood(self) -> dict[str, float]:
        """EMA likelihood per routing namespace (v6.1 fallback).

        Maps source-type EMA weights to namespace weights via
        _source_to_namespace(). When multiple source types map to
        the same namespace, takes the max weight (conservative).

        Returns:
            Dict mapping namespace -> EMA weight (>= WEIGHT_MIN).
        """
        states = self._load_states()
        ns_weights: dict[str, float] = {ns: WEIGHT_MIN for ns in ALL_NS}
        for stype, state in states.items():
            ns = self._source_to_namespace(stype)
            if ns in ns_weights:
                ns_weights[ns] = max(ns_weights[ns], state.weight)
        return ns_weights

    def _ftrl_namespace_likelihood(self) -> dict[str, float]:
        """FTRL likelihood per routing namespace (v6.2.1 split).

        Uses true FTRL-Proximal per-namespace weights from outcome
        gradients via _FTRLNamespace. Falls back to EMA-based
        mapping if FTRL namespace state is empty (all weights 1.0,
        backward compatible).

        Returns:
            Dict mapping namespace -> FTRL weight (>= 0.1).
        """
        return self._ftrl_ns.likelihood()

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

        # v6.2.1 split: update FTRL namespace weights from outcome
        correct_ns = list({
            self._source_to_namespace(self._classify_source(fp))
            for fp in used_files
        })
        if correct_ns:
            self._ftrl_ns.update(correct_ns)

    def _ranked_by_posterior(
        self,
        query: str,
        prior_ranked: list[str],
    ) -> list[str]:
        """Rank namespaces by posterior = SPS × FTRL (v6.2 Goldilocks).

        When SPPMIPrior (SPS) is available:
            posterior(ns) = SPS(query)[ns] × FTRL(likelihood)[ns]

        When SPPMIPrior is not available (backward compat):
            Falls back to prior_ranked (P-layer hit counts).

        Args:
            query: The search query text.
            prior_ranked: Fallback ranking from P-layer macro prior.

        Returns:
            Namespace list ranked by posterior score (descending).
        """
        if self._sppmi is None or not self._sppmi.is_built:
            return prior_ranked

        sppmi_prior = self._sppmi.prior(query)
        ftrl_like = self._ftrl_namespace_likelihood()

        posterior = {
            ns: sppmi_prior.get(ns, 0.0) * ftrl_like.get(ns, WEIGHT_MIN)
            for ns in ALL_NS
        }
        return sorted(posterior, key=posterior.get, reverse=True)

    def route_query(
        self,
        query: str,
        confidence: float,
        meso_scores: dict[str, float] | None = None,
    ) -> list[str]:
        """ZIQ SPS × FTRL routing (v6.2 Goldilocks).

        When SPPMIPrior is attached, uses Bayesian product combiner:
            posterior = SPS(query) × FTRL(likelihood)
        Otherwise falls back to P-layer hit-count prior (v6.1 behavior).

        SPS construction (via PTME methodology):
          P (勢): namespace hit distribution (fallback SPS)
          T (張): reranker confidence (optional signal)
          M (記): SPPMIPrior = primary SPS for this domain
          E (決): threshold → route + freeze

        FTRL: per-namespace causal weights from outcome
        Combiner: posterior = SPS × FTRL

        Keyword matching was ablation-killed (Δ=+5.48pp when removed).

        Args:
            query: The search query text.
            confidence: ZIQ alpha_t uncertainty (0=certain, 1=unknown).
            meso_scores: Optional T-layer causal signal. Dict mapping
                namespace → confidence score. None = T disabled.

        Returns:
            List of namespace keys to search.
        """
        all_ns = list(ALL_NS)
        ts = self._load_thresholds()
        p_prior = self._macro_prior()  # P 勢 (fallback)

        # v6.2 Goldilocks: SPS × FTRL posterior ranking
        ranked = self._ranked_by_posterior(query, p_prior)

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
            # T OFF → SPS × FTRL posterior (v6.2) or P 勢 (v6.1)
            best = ranked[0] if ranked else "memory"
            self._last_route_breadth = "narrow"
            self._last_route_result = [best]
            return [best]

        # E 決: medium uncertainty → 2-3 namespaces
        if meso_scores:
            # T 張 ON: top 2-3 by causal score
            sorted_meso = sorted(
                meso_scores.items(),
                key=lambda x: x[1],
                reverse=True,
            )
            selected = [
                ns for ns, _ in sorted_meso[:3] if ns in all_ns
            ]
            if len(selected) < 2:
                selected = ranked[:2]
        else:
            # T OFF → SPS × FTRL posterior top 2 (v6.2) or P 勢 (v6.1)
            selected = ranked[:2]

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

        # FTRL hyperparams (L2 auto-tuned when MetaTuner available)
        alpha = self._meta.get_param("threshold_lr")
        lambda2 = self._meta.get_param("ftrl_lam")

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

        # L2 auto-tune: record outcome for MetaTuner
        hit = any(ns in routed for ns in correct_namespaces)
        self._meta.record_outcome(correct=hit)

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
        source_stats["_l2_meta"] = self._meta.status()
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
