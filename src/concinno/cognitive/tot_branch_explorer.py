"""concinno.cognitive.tot_branch_explorer — Tree-of-Thought branch planning.

@module cognitive.tot_branch_explorer
@responsibility Close the C3 ToT/AGoT MISSING gap from
    ``cbua_ux.CbuaCode.B2_TOT`` / ``B2_AGOT`` (enum-only). Provide a pure
    decision helper that, given the current task complexity and how much
    of the reasoning token budget has been consumed, recommends a branch
    count and signals when convergence is required. Consumers
    (e.g. ``redteam_spawn_guard``, ``gaia_meta_router``, future Sancio
    parallel dispatchers) call into this layer to inform their own
    ``Agent`` tool fan-out decisions.
@dependencies concinno.cognitive.router (ComplexityDomain enum)
@exports BranchPlan, plan_branches, MAX_BRANCHES, CONVERGENCE_PCT

Why this is a pure helper, not a guard
--------------------------------------
The CBUA spec ``認知行為統一架構.md`` line 165-180 describes ToT as
"分支 → 評估 → 修剪" with "最多 3 分支" and "token 消耗 > 50% → 強制收斂".
Whether to actually spawn parallel ``Agent`` calls is a *consumer* decision
that depends on Claude Code's L1 platform ceiling (parent agent loses
control after spawn) and on whether the consumer is itself running inside
a sub-agent. This module stays pure so the consumer can adapt to local
constraints without circular guard-pipeline coupling.
"""

from __future__ import annotations

from dataclasses import dataclass

from concinno.cognitive.router import ComplexityDomain

# Default ZIQ-tunable params (mirrored in ziq_autotune_registry.py).
MAX_BRANCHES = 3
CONVERGENCE_PCT = 0.5

# Per-complexity baseline branch suggestion.
# Simple/Complicated → 1 branch (no fan-out). Complex → up to MAX_BRANCHES.
# Chaotic → MAX_BRANCHES (always fan out, then converge fast).
_BASELINE: dict[ComplexityDomain, int] = {
    ComplexityDomain.SIMPLE: 1,
    ComplexityDomain.COMPLICATED: 1,
    ComplexityDomain.COMPLEX: 3,
    ComplexityDomain.CHAOTIC: 3,
}


@dataclass(frozen=True)
class BranchPlan:
    """Outcome of a branch-planning decision.

    Attributes:
        recommended_branches: How many parallel branches the consumer
            should consider dispatching. ``1`` means do not fan out.
        should_converge: True when the budget threshold has been crossed
            and no further branches should be opened — collapse to the
            best current branch.
        reason: One-line human-readable rationale for tracing.
    """

    recommended_branches: int
    should_converge: bool
    reason: str


def plan_branches(
    *,
    complexity: ComplexityDomain,
    budget_consumed_pct: float,
    max_branches: int = MAX_BRANCHES,
    convergence_pct: float = CONVERGENCE_PCT,
) -> BranchPlan:
    """Compute a ``BranchPlan`` for the current C3 deep-exploration step.

    Args:
        complexity: Current Cynefin domain (from
            ``concinno.cognitive.router.classify_complexity``).
        budget_consumed_pct: Fraction of the reasoning token budget
            already consumed in [0.0, 1.0]. Out-of-range values are
            clamped (negative → 0.0, >1.0 → 1.0).
        max_branches: Hard cap on branches (ZIQ-tunable, vmin 1, vmax 5).
            Spec sets default = 3.
        convergence_pct: Threshold above which the planner forces
            convergence (ZIQ-tunable, vmin 0.3, vmax 0.7). Spec sets
            default = 0.5.

    Returns:
        ``BranchPlan`` with ``recommended_branches``, ``should_converge``,
        and a tracing ``reason``.
    """
    # Defensive normalisation.
    pct = budget_consumed_pct
    if pct != pct:  # NaN check
        pct = 0.0
    pct = max(0.0, min(1.0, float(pct)))
    cap = max(1, min(5, int(max_branches)))
    threshold = max(0.3, min(0.7, float(convergence_pct)))

    baseline = _BASELINE.get(complexity, 1)

    # Convergence forced by budget — overrides baseline regardless of domain.
    if pct >= threshold:
        plan = BranchPlan(
            recommended_branches=1,
            should_converge=True,
            reason=(
                f"budget {pct:.0%} >= convergence threshold {threshold:.0%} "
                f"(complexity={complexity.value}); collapse to best branch"
            ),
        )
        _emit_tot_outcomes(
            cap=cap,
            threshold=threshold,
            pct=pct,
            branches=1,
            converged=True,
            complexity=complexity,
        )
        return plan

    # Simple / Complicated never benefit from parallel branches — even with
    # plenty of budget the cost-benefit (spawn overhead vs reasoning gain)
    # is negative. The router already routes these to C1/C2, not C3.
    if baseline == 1:
        plan = BranchPlan(
            recommended_branches=1,
            should_converge=False,
            reason=(
                f"complexity={complexity.value} below C3 threshold; "
                "single-track reasoning"
            ),
        )
        _emit_tot_outcomes(
            cap=cap,
            threshold=threshold,
            pct=pct,
            branches=1,
            converged=False,
            complexity=complexity,
        )
        return plan

    # C3 territory: scale baseline against cap.
    branches = min(baseline, cap)
    plan = BranchPlan(
        recommended_branches=branches,
        should_converge=False,
        reason=(
            f"complexity={complexity.value} at budget {pct:.0%}; "
            f"explore {branches} parallel branches (cap={cap})"
        ),
    )
    _emit_tot_outcomes(
        cap=cap,
        threshold=threshold,
        pct=pct,
        branches=branches,
        converged=False,
        complexity=complexity,
    )
    return plan


def _emit_tot_outcomes(
    *,
    cap: int,
    threshold: float,
    pct: float,
    branches: int,
    converged: bool,
    complexity: ComplexityDomain,
) -> None:
    """ZIQ outcome wire for ToT tunables (4.4.0 — sub-agent K wave-2).

    Two emits per ``plan_branches`` call:
      * ``tot.max_branches`` — iteration outcome. Used = recommended
        branch count; succeeded=True when we recommended >=1 branch
        without exhausting the cap (recommended <= cap).
      * ``tot.convergence_pct`` — continuous outcome. Reward grows
        with how well the threshold separated "explore" from
        "converge" given the observed budget pct.
    """
    try:
        from concinno.ziq_emit_helpers import (
            emit_continuous_outcome,
            emit_iteration_outcome,
        )

        emit_iteration_outcome(
            "tot.max_branches",
            value=int(cap),
            iterations_used=int(branches),
            succeeded=(branches >= 1),
            source="concinno.cognitive.tot_branch_explorer.plan_branches",
            metadata={
                "complexity": complexity.value,
                "converged": converged,
            },
        )

        # Convergence-quality reward: when converged at high pct or
        # exploring at low pct, the threshold did its job → reward 1.
        # When pct is right at the threshold (boundary call) reward
        # drops because the decision was marginal.
        boundary_dist = abs(pct - threshold)
        reward = max(0.0, min(1.0, boundary_dist / max(threshold, 0.1)))
        emit_continuous_outcome(
            "tot.convergence_pct",
            value=float(threshold),
            reward=reward,
            source="concinno.cognitive.tot_branch_explorer.plan_branches",
            metadata={
                "budget_consumed_pct": pct,
                "converged": converged,
                "complexity": complexity.value,
            },
        )
    except Exception:
        pass


def format_plan(plan: BranchPlan) -> str:
    """Render a one-line summary suitable for advisory injection.

    Returns:
        A string like
        ``"B2 ToT: 3 branches (complexity=complex at budget 20%; explore ...)"``
        or, on convergence,
        ``"B2 ToT: converge (budget 60% >= convergence threshold 50%; ...)"``.
    """
    if plan.should_converge:
        head = "B2 ToT: converge"
    else:
        head = f"B2 ToT: {plan.recommended_branches} branch(es)"
    return f"{head} ({plan.reason})"


__all__ = [
    "MAX_BRANCHES",
    "CONVERGENCE_PCT",
    "BranchPlan",
    "plan_branches",
    "format_plan",
]
