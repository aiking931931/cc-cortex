"""Tests for concinno.cognitive.tot_branch_explorer module."""

from __future__ import annotations

from concinno.cognitive.router import ComplexityDomain
from concinno.cognitive.tot_branch_explorer import (
    CONVERGENCE_PCT,
    MAX_BRANCHES,
    BranchPlan,
    format_plan,
    plan_branches,
)

# ── plan_branches: per-complexity baseline ───────────────


def test_simple_returns_single_branch():
    p = plan_branches(
        complexity=ComplexityDomain.SIMPLE,
        budget_consumed_pct=0.0,
    )
    assert p.recommended_branches == 1
    assert p.should_converge is False
    assert "below C3" in p.reason


def test_complicated_returns_single_branch():
    p = plan_branches(
        complexity=ComplexityDomain.COMPLICATED,
        budget_consumed_pct=0.0,
    )
    assert p.recommended_branches == 1
    assert p.should_converge is False


def test_complex_returns_three_branches_under_budget():
    p = plan_branches(
        complexity=ComplexityDomain.COMPLEX,
        budget_consumed_pct=0.1,
    )
    assert p.recommended_branches == 3
    assert p.should_converge is False
    assert "complex" in p.reason


def test_chaotic_returns_three_branches_under_budget():
    p = plan_branches(
        complexity=ComplexityDomain.CHAOTIC,
        budget_consumed_pct=0.0,
    )
    assert p.recommended_branches == 3
    assert p.should_converge is False


# ── plan_branches: convergence threshold ─────────────────


def test_convergence_forced_at_threshold():
    p = plan_branches(
        complexity=ComplexityDomain.COMPLEX,
        budget_consumed_pct=0.5,
    )
    assert p.should_converge is True
    assert p.recommended_branches == 1


def test_convergence_above_threshold():
    p = plan_branches(
        complexity=ComplexityDomain.CHAOTIC,
        budget_consumed_pct=0.85,
    )
    assert p.should_converge is True
    assert p.recommended_branches == 1


def test_no_convergence_just_below_threshold():
    p = plan_branches(
        complexity=ComplexityDomain.COMPLEX,
        budget_consumed_pct=0.49,
    )
    assert p.should_converge is False
    assert p.recommended_branches == 3


# ── plan_branches: clamp + tunable params ────────────────


def test_negative_budget_clamped_to_zero():
    p = plan_branches(
        complexity=ComplexityDomain.COMPLEX,
        budget_consumed_pct=-0.5,
    )
    # Negative clamped → not converging
    assert p.should_converge is False


def test_above_one_budget_clamped():
    p = plan_branches(
        complexity=ComplexityDomain.COMPLEX,
        budget_consumed_pct=1.5,
    )
    assert p.should_converge is True


def test_max_branches_cap_enforced():
    p = plan_branches(
        complexity=ComplexityDomain.COMPLEX,
        budget_consumed_pct=0.1,
        max_branches=2,
    )
    assert p.recommended_branches == 2


def test_max_branches_floor_clamps_at_one():
    # max_branches=0 should clamp to 1.
    p = plan_branches(
        complexity=ComplexityDomain.COMPLEX,
        budget_consumed_pct=0.1,
        max_branches=0,
    )
    assert p.recommended_branches >= 1


def test_max_branches_ceiling_clamps_at_five():
    # max_branches=99 should clamp to 5; baseline 3 is still the cap-result.
    p = plan_branches(
        complexity=ComplexityDomain.CHAOTIC,
        budget_consumed_pct=0.0,
        max_branches=99,
    )
    # baseline=3, cap=5 → 3
    assert p.recommended_branches == 3


def test_convergence_pct_clamped_low():
    # Below 0.3 → clamped to 0.3.
    p = plan_branches(
        complexity=ComplexityDomain.COMPLEX,
        budget_consumed_pct=0.31,
        convergence_pct=0.0,
    )
    assert p.should_converge is True


def test_convergence_pct_clamped_high():
    # Above 0.7 → clamped to 0.7.
    p = plan_branches(
        complexity=ComplexityDomain.COMPLEX,
        budget_consumed_pct=0.71,
        convergence_pct=1.5,
    )
    assert p.should_converge is True


# ── format_plan rendering ────────────────────────────────


def test_format_plan_branching():
    p = BranchPlan(
        recommended_branches=3,
        should_converge=False,
        reason="explore 3 parallel branches",
    )
    out = format_plan(p)
    assert "B2 ToT" in out
    assert "3" in out


def test_format_plan_convergence():
    p = BranchPlan(
        recommended_branches=1,
        should_converge=True,
        reason="budget 60% threshold hit",
    )
    out = format_plan(p)
    assert "converge" in out


# ── module-level constants ───────────────────────────────


def test_constants_match_spec():
    # CBUA spec: max 3 branches, converge at 50%.
    assert MAX_BRANCHES == 3
    assert CONVERGENCE_PCT == 0.5
