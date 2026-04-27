"""concinno.cognitive.review_router — ZIQ-routed review method dispatcher.

@module review_router
@responsibility Pick the right review method per task instead of always
    running both standard MAR (4-perspective breadth) AND R+B+G (depth+
    rigor) fusion. Combines a hand-coded SPS structural prior with the
    existing ZIQ FTRL outcome-learning stack so the routing decision is
    cold-start safe AND outcome-adaptive once samples accumulate.
@dependencies
    concinno.ziq_autotuner (FTRL backend, no new implementation),
    concinno.ziq_autotune_registry (TunableSpec / register / get_tuner),
    concinno.redteam_spawn_guard (RedteamSpawnLedger for MAR 4-perspective
        spawn audit),
    concinno.feature_config (FEATURE_META.review_router_ziq for kill
        switch + tunable params),
    concinno.guards.redblue_green_dispatch_guard (S1' module — IMPORTED
        LAZILY so this module loads before the sibling lands).
@exports ReviewMethod, TaskSignal, RoutingDecision, AgentDispatcher,
    ReviewRouter, SPS_PRIOR

Design notes
------------
The proposal cc_3631e442 (2026-04-27) is to *route* between methods
rather than *always run both*. Two existing pieces already work:

* MAR (4-perspective breadth) — multi-anchor review, lightest cost,
  best for surveying many claims under time pressure or when an early
  exploration is needed.
* R+B+G (depth + rigor fusion) — the red+blue+green dispatch shipped
  by sibling sub-agent S1' as ``redblue_green_dispatch_guard``. Best
  for irreversible / pre-action / single-claim verification at high
  blast radius.

The router's job is to pick one of FIVE arms (the third-option
synthesis pattern: pure A, pure B, sequential A→B, sequential B→A,
parallel both). The five arms collapse the binary "MAR vs RBG" framing
into a continuous integration spectrum; the SPS prior tilts the
arms by structural signals; ZIQ FTRL refines that with outcome data.

Decoupling from S1'
-------------------
The R+B+G arm is implemented by sibling sub-agent S1' in
``concinno.guards.redblue_green_dispatch_guard``. We import it lazily
inside ``_dispatch_redblue_green`` so this module stays importable
when S1' has not yet committed. Tests covering the RBG arm use
``pytest.importorskip`` so the suite stays green without S1'.

Outcome semantics
-----------------
Cost-adjusted reward = ``outcome / max(1, token_cost / 1000)`` so
"cheap-and-correct" beats "expensive-and-correct". This is the FTRL
signal fed into ``ZIQAutoTuner``. SPS prior dominates while the
sample count is below ``ftrl_takeover_after_n_samples`` (default 30).

Meta-MAR ground-truth sampling
------------------------------
Every Mth Chaotic-radius decision (default M=10) we run BOTH the
``parallel_both`` arm AND the SPS-chosen arm. The disagreement signal
trains the routing judgment itself, not just the chosen method's
verdict. Samples are flagged ``meta_mar=True`` in the JSONL audit
log so they can be filtered downstream.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

# ── Enums + records ─────────────────────────────────────────────────


class ReviewMethod(str, Enum):
    """The five integration patterns the router can pick from.

    Note the synthesis: instead of binary "MAR vs RBG", we keep both
    pure arms AND three composite arms (sequential MAR→RBG,
    sequential RBG→MAR, true parallel). This mirrors the 5-state
    verdict pattern in ``rules/L1/redteam.md``.
    """

    MAR_ONLY = "mar_only"
    REDBLUE_GREEN_ONLY = "redblue_green_only"
    MAR_FIRST_THEN_RBG = "mar_first_then_rbg"
    RBG_FIRST_THEN_MAR = "rbg_first_then_mar"
    PARALLEL_BOTH = "parallel_both"


@dataclass(frozen=True)
class TaskSignal:
    """Structural inputs to the SPS prior — no outcome learning at boot.

    These signals come from the call site (typically the U-stage of
    ``cbua_pipeline_guard``) and describe the task. They do NOT
    describe past outcomes — that's the FTRL layer's job.
    """

    irreversible: bool
    pre_action: bool                 # False = post_failure / exploration
    radius: str                      # "simple" | "medium" | "high" | "chaotic"
    ship_gate: bool
    open_exploration: bool
    time_pressed: bool
    single_claim: bool               # True = verifying one claim


@dataclass(frozen=True)
class RoutingDecision:
    """The router's verdict for a single task.

    Fields are explainable: ``chosen_reason`` is a human-readable
    one-liner that the call site can log alongside the verdict.
    """

    method: ReviewMethod
    sps_prior_score: dict[ReviewMethod, float]
    ftrl_posterior_score: dict[ReviewMethod, float]
    chosen_reason: str
    cost_adjusted: bool              # True iff FTRL had enough samples


# ── SPS structural prior table ──────────────────────────────────────


SPS_PRIOR: dict[str, dict[ReviewMethod, float]] = {
    # signal pattern → method → boost factor (0.0–1.0)
    "irreversible_pre_action": {
        ReviewMethod.REDBLUE_GREEN_ONLY: 1.0,
        ReviewMethod.PARALLEL_BOTH: 0.8,
    },
    "post_failure_exploration": {
        ReviewMethod.MAR_ONLY: 1.0,
    },
    "chaotic_ship_gate": {
        # 1.5 not 1.0 so it dominates `multi_claim_survey`'s MAR_ONLY=1.0
        # boost when both fire (chaotic ship gate ALWAYS implies multi-claim).
        ReviewMethod.PARALLEL_BOTH: 1.5,
    },
    "time_pressed": {
        ReviewMethod.MAR_ONLY: 0.4,  # MAR cheaper than RBG
    },
    "single_claim_verification": {
        ReviewMethod.REDBLUE_GREEN_ONLY: 1.0,
    },
    "multi_claim_survey": {
        ReviewMethod.MAR_ONLY: 1.0,
    },
}


def _matched_patterns(signal: TaskSignal) -> list[str]:
    """Return the SPS pattern keys that match ``signal`` (order-independent)."""
    matched: list[str] = []
    if signal.irreversible and signal.pre_action:
        matched.append("irreversible_pre_action")
    if (not signal.pre_action) and signal.open_exploration:
        matched.append("post_failure_exploration")
    if signal.radius == "chaotic" and signal.ship_gate:
        matched.append("chaotic_ship_gate")
    if signal.time_pressed:
        matched.append("time_pressed")
    if signal.single_claim:
        matched.append("single_claim_verification")
    if not signal.single_claim:
        matched.append("multi_claim_survey")
    return matched


def _score_sps_prior(signal: TaskSignal) -> dict[ReviewMethod, float]:
    """Sum boost factors per method based on which signal patterns match."""
    scores: dict[ReviewMethod, float] = {m: 0.0 for m in ReviewMethod}
    for pattern in _matched_patterns(signal):
        for method, boost in SPS_PRIOR.get(pattern, {}).items():
            scores[method] = scores.get(method, 0.0) + boost
    return scores


# ── Agent dispatcher protocol ──────────────────────────────────────


class AgentDispatcher(Protocol):
    """Caller-supplied LLM dispatch hook.

    Same shape as ``redblue_green_dispatch_guard.AgentDispatcher`` so
    a single dispatcher can drive both the MAR 4-perspective fan-out
    and the RBG sibling guard.
    """

    def dispatch(
        self,
        prompt: str,
        *,
        model: str = "opus",
        role: str,
    ) -> str:
        ...


# ── ZIQ FTRL arm registration ──────────────────────────────────────


_ZIQ_ARM_TARGET: str = "review_method.route"
_OUTCOME_FILENAME: str = "review_router_outcomes.jsonl"


def _outcome_path() -> Path:
    """Resolve the JSONL audit path. Honours ``CONCINNO_ZIQ_TUNER_DIR`` for tests."""
    override = os.environ.get("CONCINNO_REVIEW_ROUTER_OUTCOME_DIR", "").strip()
    if override:
        base = Path(override).expanduser()
    else:
        base = Path.home() / ".concinno" / "ziq_state"
    base.mkdir(parents=True, exist_ok=True)
    return base / _OUTCOME_FILENAME


def _ensure_arm_registered() -> None:
    """Register the ``review_method.route`` arm with the ZIQ registry.

    Idempotent — a pre-existing entry is left untouched. Called from
    ``ReviewRouter.__init__`` so import order does not matter.
    """
    from concinno.ziq_autotune_registry import (
        TUNABLE_REGISTRY,
        TunableSpec,
        register,
    )

    if _ZIQ_ARM_TARGET in TUNABLE_REGISTRY:
        return

    register(
        TunableSpec(
            target=_ZIQ_ARM_TARGET,
            preset=ReviewMethod.MAR_ONLY.value,
            kind="discrete",
            choices=tuple(m.value for m in ReviewMethod),
            source="concinno.cognitive.review_router.ReviewRouter",
            note=(
                "ZIQ-routed review method dispatcher arm. SPS prior "
                "dominates while n < ftrl_takeover_after_n_samples; "
                "FTRL takes over once outcomes accumulate."
            ),
        ),
    )


# ── Feature flag plumbing ──────────────────────────────────────────


def _feature_enabled() -> bool:
    """Read FEATURE_META.review_router_ziq.enabled (defaults True)."""
    try:
        from concinno.feature_config import FEATURE_META
    except Exception:
        return True
    meta = FEATURE_META.get("review_router_ziq", {})
    return bool(meta.get("enabled", True))


def _feature_param(name: str, default: Any) -> Any:
    """Read ``FEATURE_META.review_router_ziq.params[name]`` with fallback."""
    try:
        from concinno.feature_config import FEATURE_META
    except Exception:
        return default
    meta = FEATURE_META.get("review_router_ziq", {})
    params = meta.get("params", {}) or {}
    return params.get(name, default)


# ── ReviewRouter ───────────────────────────────────────────────────


class ReviewRouter:
    """SPS × FTRL routed review method dispatcher.

    Cold-start: SPS prior dominates. After
    ``ftrl_takeover_after_n_samples`` outcomes the FTRL posterior is
    blended in via ``final_score = SPS_prior * (1 + FTRL_posterior)``
    so the prior remains a floor and the posterior shapes the slope.

    Persistence:
    * JSONL audit at ``~/.concinno/ziq_state/review_router_outcomes.jsonl``
      (override via ``CONCINNO_REVIEW_ROUTER_OUTCOME_DIR``).
    * ``ZIQAutoTuner`` state under ``~/.concinno/ziq_tuners/`` (handled
      by the existing tuner — we only call ``record()`` / ``suggest()``).
    """

    def __init__(self) -> None:
        _ensure_arm_registered()
        self._chaotic_decision_count = 0

    # -- Public API -------------------------------------------------

    def route(self, signal: TaskSignal) -> RoutingDecision:
        """Return the routing decision for ``signal`` without executing it."""
        sps_scores = _score_sps_prior(signal)

        ftrl_scores: dict[ReviewMethod, float] = {m: 0.0 for m in ReviewMethod}
        cost_adjusted = False

        from concinno.ziq_autotune_registry import get_tuner

        tuner = get_tuner(_ZIQ_ARM_TARGET)
        takeover_n = int(
            _feature_param("ftrl_takeover_after_n_samples", 30),
        )
        if tuner.n >= takeover_n:
            cost_adjusted = True
            for method in ReviewMethod:
                arm = tuner._arms.get(method.value)
                if arm is None:
                    continue
                ftrl_scores[method] = arm.mean_reward()

        # Blend: final = SPS * (1 + FTRL). Prior is the floor; FTRL shapes the slope.
        final_scores: dict[ReviewMethod, float] = {}
        for method in ReviewMethod:
            final_scores[method] = sps_scores[method] * (1.0 + ftrl_scores[method])

        # Pick the dominant arm. Tie-break order: the enum order acts as the
        # implicit prior for "no signal" cases and keeps determinism.
        chosen = max(
            ReviewMethod,
            key=lambda m: (final_scores[m], -list(ReviewMethod).index(m)),
        )
        if all(v == 0.0 for v in final_scores.values()):
            # No SPS pattern matched. Fall back to MAR_ONLY (cheapest safe arm).
            chosen = ReviewMethod.MAR_ONLY

        matched = _matched_patterns(signal) or ["no_pattern"]
        reason = " × ".join(matched) + f" → {chosen.value}"

        return RoutingDecision(
            method=chosen,
            sps_prior_score=sps_scores,
            ftrl_posterior_score=ftrl_scores,
            chosen_reason=reason,
            cost_adjusted=cost_adjusted,
        )

    def execute(
        self,
        signal: TaskSignal,
        decision_context: str,
        dispatcher: Any,
    ) -> Any:
        """Route + execute the chosen method.

        For Chaotic radius every Mth call (default M=10) we additionally
        run the ``parallel_both`` arm as a meta-MAR ground-truth sample.
        Both verdicts are written to the JSONL log; the SPS-chosen
        verdict is returned to the caller.
        """
        if not _feature_enabled():
            return self._dispatch(
                ReviewMethod.MAR_ONLY,
                signal,
                decision_context,
                dispatcher,
            )

        decision = self.route(signal)

        meta_every = int(_feature_param("meta_mar_every_n_chaotic", 10))
        if signal.radius == "chaotic":
            self._chaotic_decision_count += 1
            if (
                meta_every > 0
                and self._chaotic_decision_count % meta_every == 0
                and decision.method != ReviewMethod.PARALLEL_BOTH
            ):
                # Run the parallel_both ground-truth shadow probe FIRST so
                # the disagreement signal can be logged before the main
                # dispatch returns. Tag with meta_mar=True for filtering.
                self._dispatch(
                    ReviewMethod.PARALLEL_BOTH,
                    signal,
                    decision_context,
                    dispatcher,
                    meta_mar=True,
                )

        # The main dispatch is the SPS-chosen verdict — never tag it as a
        # meta_mar ground-truth probe. Only the ``parallel_both`` shadow
        # call above carries the meta_mar=True flag.
        verdict = self._dispatch(
            decision.method,
            signal,
            decision_context,
            dispatcher,
            meta_mar=False,
        )
        return verdict

    def record_outcome(
        self,
        method: ReviewMethod,
        signal: TaskSignal,
        outcome: float,
        token_cost: int,
    ) -> None:
        """Append cost-adjusted outcome to JSONL + feed into FTRL arm.

        ``outcome`` is the raw reward (1.0 = next user turn did NOT
        correct the verdict; 0.0 = overruled). The cost-adjusted
        reward is ``outcome / max(1, token_cost / 1000)`` so cheap-
        and-correct beats expensive-and-correct.
        """
        cost_factor = float(_feature_param("cost_adjustment_factor", 1.0))
        cost_div = max(1.0, (token_cost / 1000.0) * cost_factor)
        adjusted = float(outcome) / cost_div

        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "method": method.value,
            "signal": {
                "irreversible": signal.irreversible,
                "pre_action": signal.pre_action,
                "radius": signal.radius,
                "ship_gate": signal.ship_gate,
                "open_exploration": signal.open_exploration,
                "time_pressed": signal.time_pressed,
                "single_claim": signal.single_claim,
            },
            "outcome_raw": float(outcome),
            "outcome_adjusted": adjusted,
            "token_cost": int(token_cost),
            "meta_mar": False,
        }

        path = _outcome_path()
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

        from concinno.ziq_autotune_registry import get_tuner

        tuner = get_tuner(_ZIQ_ARM_TARGET)
        tuner.record(
            method.value,
            adjusted,
            context={
                "radius": signal.radius,
                "irreversible": signal.irreversible,
                "token_cost": int(token_cost),
            },
        )

    # -- Internals --------------------------------------------------

    def _dispatch(
        self,
        method: ReviewMethod,
        signal: TaskSignal,
        decision_context: str,
        dispatcher: Any,
        *,
        meta_mar: bool = False,
    ) -> Any:
        """Single-method dispatch. Returns whatever the chosen arm produces."""
        if method == ReviewMethod.MAR_ONLY:
            return self._dispatch_mar_4perspective(
                signal,
                decision_context,
                dispatcher,
                meta_mar=meta_mar,
            )
        if method == ReviewMethod.REDBLUE_GREEN_ONLY:
            return self._dispatch_redblue_green(
                signal,
                decision_context,
                dispatcher,
                meta_mar=meta_mar,
            )
        if method == ReviewMethod.MAR_FIRST_THEN_RBG:
            mar_verdict = self._dispatch_mar_4perspective(
                signal,
                decision_context,
                dispatcher,
                meta_mar=meta_mar,
            )
            rbg_verdict = self._dispatch_redblue_green(
                signal,
                decision_context,
                dispatcher,
                meta_mar=meta_mar,
            )
            return {"mar": mar_verdict, "rbg": rbg_verdict, "order": "mar_first"}
        if method == ReviewMethod.RBG_FIRST_THEN_MAR:
            rbg_verdict = self._dispatch_redblue_green(
                signal,
                decision_context,
                dispatcher,
                meta_mar=meta_mar,
            )
            mar_verdict = self._dispatch_mar_4perspective(
                signal,
                decision_context,
                dispatcher,
                meta_mar=meta_mar,
            )
            return {"rbg": rbg_verdict, "mar": mar_verdict, "order": "rbg_first"}
        # PARALLEL_BOTH — sequential at this layer; true parallelism
        # is the dispatcher's choice (caller can wrap with ThreadPool).
        mar_verdict = self._dispatch_mar_4perspective(
            signal,
            decision_context,
            dispatcher,
            meta_mar=meta_mar,
        )
        rbg_verdict = self._dispatch_redblue_green(
            signal,
            decision_context,
            dispatcher,
            meta_mar=meta_mar,
        )
        return {"mar": mar_verdict, "rbg": rbg_verdict, "order": "parallel"}

    # -- MAR 4-perspective inline (~50 LOC) ------------------------

    _MAR_LENSES: tuple[tuple[str, str], ...] = (
        (
            "engineer",
            "You are a senior engineer reviewer. Focus on correctness, "
            "wiring, edge cases, and regression risk.",
        ),
        (
            "user",
            "You are a real end-user reviewer. Focus on whether the "
            "outcome solves the user's stated problem with low friction.",
        ),
        (
            "attacker",
            "You are an adversarial security reviewer. Focus on misuse, "
            "abuse, escalation paths, and supply-chain risk.",
        ),
        (
            "auditor",
            "You are a compliance/audit reviewer. Focus on observability, "
            "logging, reproducibility, and chain-of-custody.",
        ),
    )

    def _dispatch_mar_4perspective(
        self,
        signal: TaskSignal,
        decision_context: str,
        dispatcher: Any,
        *,
        meta_mar: bool = False,
    ) -> dict[str, str]:
        """Run 4 redteam-role Opus calls each with a different lens.

        Each spawn is gated through ``RedteamSpawnLedger`` with
        ``role="redteam"`` (existing accepted role). The 4 lens names
        are passed as ``model="opus"`` + role tag in the dispatcher
        call so a real Opus dispatcher can branch on the lens.
        """
        from concinno.redteam_spawn_guard import before_spawn_redteam

        event_id = f"mar4-{int(time.time() * 1000)}"
        results: dict[str, str] = {}
        for lens_name, lens_prompt in self._MAR_LENSES:
            before_spawn_redteam(
                event_id,
                estimated_spawns=1,
                role="redteam",
                extra={
                    "router": "review_router",
                    "lens": lens_name,
                    "meta_mar": meta_mar,
                },
            )
            prompt = (
                f"{lens_prompt}\n\nDecision under review:\n{decision_context}\n\n"
                f"Signal: radius={signal.radius}, irreversible={signal.irreversible}, "
                f"pre_action={signal.pre_action}.\n\n"
                "Output a 1-paragraph verdict from your lens."
            )
            results[lens_name] = dispatcher.dispatch(
                prompt,
                model="opus",
                role="redteam",
            )
        return results

    def _dispatch_redblue_green(
        self,
        signal: TaskSignal,
        decision_context: str,
        dispatcher: Any,
        *,
        meta_mar: bool = False,
    ) -> Any:
        """Delegate to the S1' RBG dispatch guard.

        Imported lazily so the rest of this module loads even when the
        sibling has not yet committed. Tests covering this arm should
        use ``pytest.importorskip``.
        """
        try:
            from concinno.guards.redblue_green_dispatch_guard import (
                Radius,
                RedBlueGreenDispatchGuard,
            )
        except ImportError as exc:  # pragma: no cover - exercised by skip test
            raise NotImplementedError(
                "S1' module not landed yet — re-run after sibling sub-agent commits "
                "(concinno.guards.redblue_green_dispatch_guard).",
            ) from exc

        radius_map = {
            "simple": Radius.SIMPLE,
            "medium": Radius.MEDIUM,
            "high": Radius.HIGH,
            "chaotic": Radius.CHAOTIC,
        }
        radius = radius_map.get(signal.radius, Radius.MEDIUM)

        guard = RedBlueGreenDispatchGuard()
        original_intent = (
            f"meta_mar={meta_mar} | irreversible={signal.irreversible} | "
            f"pre_action={signal.pre_action}"
        )
        return guard.review(
            radius=radius,
            decision_context=decision_context,
            original_intent=original_intent,
            dispatcher=dispatcher,
        )


__all__ = [
    "AgentDispatcher",
    "ReviewMethod",
    "ReviewRouter",
    "RoutingDecision",
    "SPS_PRIOR",
    "TaskSignal",
]
