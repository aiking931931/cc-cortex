"""Confidence fusion — pre/post α_t signal aggregator (Tier 1 scope).

@module concinno.agent.confidence_fusion
@responsibility Expose two deterministic functions that produce an α_t
    signal *before* the multi-role dispatch (so Commander can route) and
    *after* the roles have spoken (so a future supervisor rubric can
    score agreement). Tier 1 deliberately ships only the thin signal
    aggregator — no Platt calibration, no sklearn import, no logprob
    lookup. Those paths were all rejected by the S5 verdict (F1 / H2 /
    M2).

S5 verdict anchors (see
``_AI_BRAIN/05_Planning/mas-tier-overhaul-commander-verdict-2026-04-22.md``):

* **H2 ACCEPT** — ``confidence_fusion`` is a function-level signal
  aggregator. No probabilistic calibration. The
  ``SANCIO_MAS_USE_LOGPROBS`` flag stays as a future hook at the
  adapter layer, not here.

* **M2 ACCEPT** — No Platt contamination. If Tier 2+ wants Platt, that
  lands in a separate ``calibration.py`` module with its own held-out
  set budget.

* **H5 ACCEPT** — ``compute_post`` accepts a ``rubric_report`` parameter
  typed ``dict | None``. When ``None`` (Tier 1 ships without the rubric
  module), ``rubric_pass_rate`` defaults to ``1.0`` so post-α_t
  degrades to "role agreement rate × 1 = role agreement rate". Tier 2
  lands the rubric wiring.

Why this lives in Concinno (MEMORY #52 切點):
    Confidence fusion is generic across benchmarks — GAIA, HAL,
    OSWorld all need "how do the role outputs agree post-hoc". Keeping
    the aggregator in the library avoids every runner re-implementing
    string-overlap heuristics.

@dependencies stdlib only.
@exports AlphaSignal
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from concinno.agent.commander import TierDecision


class AlphaSignal:
    """Deterministic α_t aggregator.

    Two class methods, no instance state:

    * :meth:`compute_pre` — thin wrapper around an already-computed
      :class:`concinno.agent.commander.TierDecision`. Returns
      ``decision.alpha_t`` directly. Exists as a module boundary so
      Tier 2+ can drop in a richer pre-α_t without every consumer
      updating its import (e.g. a "query_length_feature" layer).

    * :meth:`compute_post` — combines role agreement with (optional)
      rubric pass rate. Tier 1 ships without a rubric module so the
      default ``rubric_report=None`` case degenerates cleanly.
    """

    @staticmethod
    def compute_pre(commander_decision: TierDecision) -> float:
        """Return the pre-role α_t as computed by the Commander.

        This is intentionally a trivial passthrough. The point of the
        method (vs accessing ``decision.alpha_t`` directly at call
        sites) is to establish the extension point — Tier 2 can swap
        in a weighted aggregation without touching consumer code.
        """
        return float(commander_decision.alpha_t)

    @staticmethod
    def compute_post(
        role_outputs: list[dict[str, Any]],
        rubric_report: dict[str, Any] | None = None,
    ) -> float:
        """Post-role α_t = ``role_agreement_rate × rubric_pass_rate``.

        Parameters
        ----------
        role_outputs : list[dict]
            One dict per role. Expected keys: ``"role"`` (str) and
            ``"answer"`` (str). Extra keys ignored for forward-compat
            with ``MASResult.per_role`` records (which also carry
            ``raw_len`` / ``skipped``).
        rubric_report : dict | None
            When ``None`` (Tier 1 default) → ``rubric_pass_rate=1.0``.
            When a dict, expected key ``"pass_rate"`` ∈ [0, 1]. Missing
            or malformed → treated as ``1.0`` so bad rubric output
            doesn't silently zero the post-α_t.

        Returns
        -------
        float
            Post-role α_t ∈ [0, 1]. Empty / single-role inputs return
            ``1.0`` (degenerate case — nothing to disagree about).
        """
        agreement = _role_agreement_rate(role_outputs)
        pass_rate = _rubric_pass_rate(rubric_report)
        return _clamp(agreement * pass_rate, 0.0, 1.0)


# ─────────────────────────── Helpers ───────────────────────────


def _role_agreement_rate(role_outputs: list[dict[str, Any]]) -> float:
    """Pairwise string overlap across non-empty role answers.

    "Overlap" here is a simple token-set Jaccard — intentionally
    heuristic so we don't import nltk / sklearn for a Tier 1 signal.
    The method returns the *average* pairwise Jaccard across all
    2-tuples of non-empty answers. With one non-empty answer (or zero)
    there's nothing to disagree about → returns ``1.0``.
    """
    if not role_outputs:
        return 1.0

    answers = [
        str(r.get("answer", "") or "").strip()
        for r in role_outputs
        if isinstance(r, dict)
    ]
    non_empty = [a for a in answers if a]

    if len(non_empty) < 2:
        return 1.0

    # Token sets — lower-cased whitespace split. Good enough for agreement
    # signals on GAIA-style final-answer strings (typically short).
    token_sets: list[frozenset[str]] = [
        frozenset(a.lower().split()) for a in non_empty
    ]

    pair_scores: list[float] = []
    for i in range(len(token_sets)):
        for j in range(i + 1, len(token_sets)):
            a = token_sets[i]
            b = token_sets[j]
            if not a and not b:
                pair_scores.append(1.0)
                continue
            union = a | b
            if not union:
                pair_scores.append(1.0)
                continue
            pair_scores.append(len(a & b) / len(union))

    return sum(pair_scores) / len(pair_scores) if pair_scores else 1.0


def _rubric_pass_rate(rubric_report: dict[str, Any] | None) -> float:
    """Extract rubric pass rate, defaulting to 1.0 when unavailable.

    Tier 1 does NOT ship the rubric module (that's Tier 2 scope per
    S5 H3 / H5). When the report is ``None`` or malformed we return
    ``1.0`` so post-α_t degrades to pure role agreement rather than
    silently collapsing to 0.
    """
    if rubric_report is None:
        return 1.0
    if not isinstance(rubric_report, dict):
        return 1.0
    raw = rubric_report.get("pass_rate", 1.0)
    try:
        return _clamp(float(raw), 0.0, 1.0)
    except (TypeError, ValueError):
        return 1.0


def _clamp(x: float, lo: float, hi: float) -> float:
    """Clamp ``x`` to ``[lo, hi]``."""
    return max(lo, min(hi, x))


__all__ = ["AlphaSignal"]
