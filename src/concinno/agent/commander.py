"""Commander — tier router for MAS 3-tier overhaul (Tier 1 scope).

@module concinno.agent.commander
@responsibility Map an incoming question + operational context to a
    tier decision (``0``=SAS, ``1``=3-role MAS with asymmetry, ``2``=MAS +
    rubric judge toolkit, ``3``=MAS + parallel branch DAG). The commander
    is **deterministic** by design — Tier 1 explicitly rejects
    probabilistic calibration (Platt / logprobs / peakedness) because the
    red-team's F1 finding proved those signals do not exist on the code
    paths consumers call today.

S5 verdict anchors (see
``_AI_BRAIN/05_Planning/mas-tier-overhaul-commander-verdict-2026-04-22.md``):

* **F1 ACCEPT** — ``alpha_t`` uses only
  :func:`concinno.c0_router.C0Router.classify_with_hysteresis` +
  attached-file count. No peakedness, no Platt, no sklearn, no logprobs.
* **F3 ACCEPT** — No IMPLIRET-derived thresholds. The complexity
  categorical → α_t mapping + α_t → tier thresholds are warm-start
  heuristics. A ``thresholds_frozen_until_n_outcomes`` flag advertises
  the FTRL sunset clause (Tier 2 lands the actual on-line update).
* **M3 ACCEPT** — Adapter-level env flag (see ``agent_api.py``) defaults
  ``SANCIO_MAS_COMMANDER=0``. This module does not read env itself.
* **藍 C2 ACCEPT** — ``TierDecision.thresholds_frozen_until_n_outcomes``
  exists as a data field so consumers know when auto-tune activates
  (the actual FTRL wiring is Tier 2 scope and is NOT implemented here).

Why this lives in Concinno (MEMORY #52 切點):
    Tier routing is benchmark-agnostic — GAIA, HAL, OSWorld, any agent
    consumer can ask "given question + context, which multi-role
    escalation should I run?" The answer stays the same regardless of
    which runner is driving.

@dependencies stdlib only; reuses :class:`concinno.c0_router.C0Router`.
@exports Commander, TierDecision, TierBudget, TIER_BUDGETS, FallbackChain
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from concinno.c0_router import C0Result, C0Router


# ─────────────────────────── α_t mapping ───────────────────────────
# Warm-start heuristic mapping (c0_router 4-class categorical → α_t).
# Boundaries between tiers are picked so the default c0_router class
# lands mid-tier: Simple→tier0, Complicated→tier1, Complex→tier2,
# Chaotic→tier3. File-count boost lowers α_t because attached files
# reduce ambiguity (the solver has concrete material).
# S5 F3: thresholds marked "warm-start only" — Tier 2 ships FTRL.

COMPLEXITY_PRIOR: dict[str, float] = {
    "simple": 0.20,
    "complicated": 0.50,
    "complex": 0.75,
    "chaotic": 0.90,
}
"""c0_router 4-class → α_t categorical prior.

Rationale for these anchors (matches verdict ACCEPT F1):

* Simple → 0.20 well below the 0.35 tier-1 boundary → routes to SAS.
* Complicated → 0.50 mid-tier-1.
* Complex → 0.75 mid-tier-2.
* Chaotic → 0.90 above the 0.85 tier-3 boundary.

These are hand-set until the FTRL sunset clause lifts at
``n_outcomes >= 60`` (see ``thresholds_frozen_until_n_outcomes``).
"""

QUESTION_FILE_BOOST_PER_FILE: float = 0.1
"""Reduction applied to α_t per attached file.

Attached files concretise ambiguity: a question with 2 file attachments
is closer to "follow the reference" than "free research". Clamped at
:data:`QUESTION_FILE_BOOST_MAX` so a 10-file request doesn't flip
chaotic → SAS.
"""

QUESTION_FILE_BOOST_MAX: float = 0.2
"""Upper bound on how much ``attached_file_count`` can pull α_t down."""

# ─────────────────────────── Tier thresholds ───────────────────────────
# α_t → tier mapping. Kept as module constants (not enum) so ablation
# runs that want to sweep the boundary can monkeypatch cleanly.
# S5 F3 ACCEPT: values are warm-start heuristics, auto-tune kicks in at
# N≥60 cumulative outcomes (Tier 2).

TIER_0_MAX_ALPHA: float = 0.35
TIER_1_MAX_ALPHA: float = 0.60
TIER_2_MAX_ALPHA: float = 0.85


# ─────────────────────────── Budget struct ───────────────────────────


@dataclass(frozen=True)
class TierBudget:
    """Per-tier token / iteration ceiling envelope.

    Values are guidance for the adapter — consumers can clamp harder
    at the call site (``max_iterations`` / ``thinking_budget`` on the
    request body). This dataclass is frozen so a decision object can
    be shared across async tasks without aliasing surprises.
    """

    max_tokens: int
    """Soft token ceiling the adapter SHOULD honour for this tier."""

    max_iterations: int
    """Soft iteration ceiling for the solver role."""

    per_role_timeout_s: int
    """Per-role wall-clock budget."""


TIER_BUDGETS: dict[int, TierBudget] = {
    0: TierBudget(max_tokens=4_096, max_iterations=8, per_role_timeout_s=300),
    1: TierBudget(max_tokens=8_192, max_iterations=12, per_role_timeout_s=600),
    2: TierBudget(max_tokens=16_384, max_iterations=16, per_role_timeout_s=900),
    3: TierBudget(max_tokens=32_768, max_iterations=20, per_role_timeout_s=1_200),
}
"""Warm-start envelope table. S5 L4 ACCEPT: token envelope reported as
~2× at Tier 1, ~3× at Tier 2, ~4-5× at Tier 3 vs SAS baseline."""


# ─────────────────────────── TierDecision struct ───────────────────────────


@dataclass(frozen=True)
class TierDecision:
    """Commander output: which tier + why + budget + fallback.

    Frozen so the decision can cross async boundaries. The
    ``fallback_chain`` lists tiers the caller should try in order if
    the primary tier dispatch raises a retryable error. Default behaviour:
    ``[current_tier, max(current_tier - 1, 0)]`` — one step down the
    ladder before giving up and returning a SAS answer.
    """

    tier: int
    """``0`` SAS / ``1`` MAS+asymmetry / ``2`` MAS+rubric / ``3`` MAS+DAG."""

    alpha_t: float
    """Computed α_t ∈ [0, 1]. Deterministic given (c0 class, file count)."""

    budget: TierBudget
    """Budget envelope the adapter SHOULD respect for this tier."""

    reason: str
    """Human-readable routing rationale (for telemetry / debug)."""

    fallback_chain: list[int] = field(default_factory=list)
    """Ordered tier IDs to try if the primary tier errors. Frozen."""

    thresholds_frozen_until_n_outcomes: int = 60
    """FTRL sunset clause (S5 藍 C2). Once the commander has observed
    this many tier-execution outcomes the auto-tune path would activate.
    Tier 1 only exposes the flag; Tier 2 ships the actual wiring."""

    signals: dict[str, Any] = field(default_factory=dict)
    """Opaque signal dump for post-hoc analysis. Keys stable across
    versions: ``c0_class`` / ``attached_file_count`` / ``complexity_prior``."""


# ─────────────────────────── Commander ───────────────────────────


class Commander:
    """Route a question to the appropriate MAS tier.

    The commander does exactly three things:

    1. Ask :class:`concinno.c0_router.C0Router` for the question's
       complexity class (via its ``classify_with_hysteresis`` path so
       the session-level ratchet applies, or ``classify`` if the caller
       has no session).
    2. Compute a deterministic α_t from the class + attached-file count.
    3. Map α_t → tier → budget via module constants.

    It does NOT:

    * run an LLM probe
    * read Anthropic logprobs
    * compute peakedness / Platt calibration
    * consult any non-deterministic oracle

    Those are Tier 2+ concerns.
    """

    def __init__(
        self,
        router: C0Router | None = None,
    ) -> None:
        """Bind a :class:`C0Router` instance (or construct a default).

        Tests can inject a mock router to exercise each complexity
        class deterministically. In production the default-constructed
        router is pure heuristic (no LLM calls).
        """
        if router is None:
            # Import locally so module import stays cheap for callers
            # who only want the dataclasses (e.g. ``TierDecision``
            # pickling in a worker).
            from concinno.c0_router import C0Router as _C0Router
            router = _C0Router()
        self._router = router

    # ── Helpers ──

    @staticmethod
    def _complexity_prior(c0_class: str) -> float:
        """Return the α_t prior for a c0_router class, case-insensitive.

        Unknown classes fall through to ``complicated`` (0.50) — the
        mid-tier default is safer than routing an unclassified prompt
        to SAS (which a zero prior would do).
        """
        key = (c0_class or "").strip().lower()
        return COMPLEXITY_PRIOR.get(key, COMPLEXITY_PRIOR["complicated"])

    @staticmethod
    def _file_boost(attached_file_count: int) -> float:
        """Non-negative boost applied to α_t for attached files.

        Clamped at :data:`QUESTION_FILE_BOOST_MAX`. Negative inputs are
        treated as ``0`` (bad clients shouldn't be able to pull α_t up
        past the raw prior).
        """
        count = max(0, int(attached_file_count))
        boost = count * QUESTION_FILE_BOOST_PER_FILE
        return min(boost, QUESTION_FILE_BOOST_MAX)

    @staticmethod
    def _alpha_to_tier(alpha_t: float) -> int:
        """Map α_t ∈ [0, 1] to tier ∈ {0, 1, 2, 3}.

        Half-open intervals so the boundary values fall to the higher
        tier (consistent with "when in doubt, escalate").
        """
        if alpha_t < TIER_0_MAX_ALPHA:
            return 0
        if alpha_t < TIER_1_MAX_ALPHA:
            return 1
        if alpha_t < TIER_2_MAX_ALPHA:
            return 2
        return 3

    @staticmethod
    def _fallback_for(tier: int) -> list[int]:
        """Build a default fallback chain for ``tier``.

        The chain always includes ``tier`` itself as the primary attempt,
        followed by ``tier-1`` (capped at ``0``), and finally ``0`` so
        every failure eventually degrades to SAS rather than looping.
        Duplicates are collapsed while preserving order.
        """
        candidates = [tier, max(tier - 1, 0), 0]
        seen: set[int] = set()
        out: list[int] = []
        for t in candidates:
            if t not in seen:
                seen.add(t)
                out.append(t)
        return out

    # ── Route ──

    def route(
        self,
        question: str,
        ctx: dict[str, Any] | None = None,
    ) -> TierDecision:
        """Classify ``question`` + ``ctx`` into a :class:`TierDecision`.

        ``ctx`` recognised keys (all optional):

        * ``attached_file_count`` — int, default 0. Files attached to the
          turn; caps at :data:`QUESTION_FILE_BOOST_MAX` via :meth:`_file_boost`.
        * ``tool_history`` — list[str], forwarded to c0_router.
        * ``file_paths`` — list[str], forwarded to c0_router.
        * ``context_tokens`` — int, forwarded to c0_router.
        * ``cache_dir`` + ``session_id`` — when BOTH present, invoke
          ``classify_with_hysteresis`` so the session ratchet applies.
          When either is missing, fall back to pure ``classify``.

        The return value is always a :class:`TierDecision`; the commander
        never raises on unknown ctx keys (forward-compat — consumers can
        extend ``ctx`` without breaking older commander builds).
        """
        ctx = ctx or {}
        attached_file_count = int(ctx.get("attached_file_count", 0) or 0)
        tool_history = ctx.get("tool_history")
        file_paths = ctx.get("file_paths")
        context_tokens = ctx.get("context_tokens")
        cache_dir = ctx.get("cache_dir")
        session_id = ctx.get("session_id")

        # Prefer the hysteresis path when the caller has wired up a
        # session-level state store — this keeps the commander honest
        # against self-downgrade attacks (c0_router docstring).
        c0_result: C0Result
        if cache_dir and session_id:
            c0_result = self._router.classify_with_hysteresis(
                task_prompt=question,
                cache_dir=str(cache_dir),
                session_id=str(session_id),
                tool_history=list(tool_history) if tool_history else None,
                file_paths=list(file_paths) if file_paths else None,
                context_tokens=int(context_tokens) if context_tokens is not None else None,
            )
        else:
            c0_result = self._router.classify(
                task_prompt=question,
                tool_history=list(tool_history) if tool_history else None,
                file_paths=list(file_paths) if file_paths else None,
                context_tokens=int(context_tokens) if context_tokens is not None else None,
            )

        c0_class = str(c0_result.complexity).lower()
        prior = self._complexity_prior(c0_class)
        file_boost = self._file_boost(attached_file_count)

        # α_t = prior - boost, clamped to [0, 1].
        alpha_t = max(0.0, min(1.0, prior - file_boost))
        tier = self._alpha_to_tier(alpha_t)
        budget = TIER_BUDGETS[tier]

        reason_parts = [
            f"c0={c0_class}",
            f"prior={prior:.2f}",
        ]
        if file_boost > 0:
            reason_parts.append(
                f"files={attached_file_count} (boost -{file_boost:.2f})"
            )
        reason_parts.append(f"alpha_t={alpha_t:.3f}")
        reason_parts.append(f"-> tier {tier}")
        reason = "; ".join(reason_parts)

        signals = {
            "c0_class": c0_class,
            "attached_file_count": attached_file_count,
            "complexity_prior": prior,
            "file_boost": file_boost,
            "tier_thresholds": {
                "tier_0_max": TIER_0_MAX_ALPHA,
                "tier_1_max": TIER_1_MAX_ALPHA,
                "tier_2_max": TIER_2_MAX_ALPHA,
            },
        }

        return TierDecision(
            tier=tier,
            alpha_t=alpha_t,
            budget=budget,
            reason=reason,
            fallback_chain=self._fallback_for(tier),
            signals=signals,
        )


__all__ = [
    "COMPLEXITY_PRIOR",
    "QUESTION_FILE_BOOST_MAX",
    "QUESTION_FILE_BOOST_PER_FILE",
    "TIER_0_MAX_ALPHA",
    "TIER_1_MAX_ALPHA",
    "TIER_2_MAX_ALPHA",
    "TIER_BUDGETS",
    "Commander",
    "TierBudget",
    "TierDecision",
]
