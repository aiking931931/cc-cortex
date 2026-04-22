"""Tests for :mod:`concinno.agent.confidence_fusion` (Tier 1 scope).

S5 verdict anchors:
* H2 — deterministic aggregator only; no Platt calibration.
* H5 — ``compute_post`` tolerates ``rubric_report=None`` (Tier 2 wires
  the actual rubric module).
* M2 — no Platt contamination in this module.
"""

from __future__ import annotations

import pytest

from concinno.agent.commander import TIER_BUDGETS, TierDecision
from concinno.agent.confidence_fusion import AlphaSignal


def _decision(alpha: float) -> TierDecision:
    """Build a minimal :class:`TierDecision` fixture with the given α_t."""
    return TierDecision(
        tier=0,
        alpha_t=alpha,
        budget=TIER_BUDGETS[0],
        reason="test",
    )


# ─────────────────────────── compute_pre ───────────────────────────


class TestComputePre:
    """Thin passthrough — returns ``decision.alpha_t`` unchanged."""

    def test_returns_decision_alpha(self) -> None:
        assert AlphaSignal.compute_pre(_decision(0.42)) == pytest.approx(0.42)

    def test_returns_zero_when_alpha_zero(self) -> None:
        assert AlphaSignal.compute_pre(_decision(0.0)) == 0.0

    def test_returns_one_when_alpha_one(self) -> None:
        assert AlphaSignal.compute_pre(_decision(1.0)) == 1.0

    def test_is_static(self) -> None:
        """No instance state needed — call as classmethod / static."""
        assert AlphaSignal.compute_pre(_decision(0.3)) == pytest.approx(0.3)


# ─────────────────────────── compute_post: agreement signal ───────────────────────────


class TestAgreementRate:
    def test_all_agree_returns_one(self) -> None:
        outputs = [
            {"role": "solver", "answer": "4"},
            {"role": "critic", "answer": "4"},
            {"role": "judge", "answer": "4"},
        ]
        assert AlphaSignal.compute_post(outputs) == pytest.approx(1.0)

    def test_all_disagree_returns_zero(self) -> None:
        outputs = [
            {"role": "solver", "answer": "apple"},
            {"role": "critic", "answer": "banana"},
            {"role": "judge", "answer": "cherry"},
        ]
        assert AlphaSignal.compute_post(outputs) == pytest.approx(0.0)

    def test_partial_overlap_between_zero_and_one(self) -> None:
        outputs = [
            {"role": "solver", "answer": "the red apple"},
            {"role": "critic", "answer": "the red banana"},
        ]
        result = AlphaSignal.compute_post(outputs)
        assert 0.0 < result < 1.0

    def test_case_insensitive(self) -> None:
        """Token sets lowercased before Jaccard."""
        outputs = [
            {"role": "solver", "answer": "Hello World"},
            {"role": "critic", "answer": "hello world"},
        ]
        assert AlphaSignal.compute_post(outputs) == pytest.approx(1.0)


# ─────────────────────────── compute_post: edge cases ───────────────────────────


class TestEdgeCases:
    def test_empty_list_returns_one(self) -> None:
        assert AlphaSignal.compute_post([]) == pytest.approx(1.0)

    def test_single_role_returns_one(self) -> None:
        outputs = [{"role": "solver", "answer": "4"}]
        assert AlphaSignal.compute_post(outputs) == pytest.approx(1.0)

    def test_all_empty_answers_returns_one(self) -> None:
        """All non-empty filtered → zero-pair case → return 1.0."""
        outputs = [
            {"role": "solver", "answer": ""},
            {"role": "critic", "answer": ""},
        ]
        assert AlphaSignal.compute_post(outputs) == pytest.approx(1.0)

    def test_one_empty_other_non_empty_returns_one(self) -> None:
        """One non-empty → <2 non-empty → degenerate → 1.0."""
        outputs = [
            {"role": "solver", "answer": "something"},
            {"role": "critic", "answer": ""},
        ]
        assert AlphaSignal.compute_post(outputs) == pytest.approx(1.0)

    def test_whitespace_only_answer_treated_as_empty(self) -> None:
        outputs = [
            {"role": "solver", "answer": "   "},
            {"role": "critic", "answer": "hello"},
        ]
        assert AlphaSignal.compute_post(outputs) == pytest.approx(1.0)

    def test_non_dict_entry_skipped(self) -> None:
        """Forward-compat: malformed entries don't blow up."""
        outputs = [
            {"role": "solver", "answer": "hello"},
            "not a dict",  # type: ignore[list-item]
            {"role": "critic", "answer": "hello"},
        ]
        assert AlphaSignal.compute_post(outputs) == pytest.approx(1.0)

    def test_missing_answer_key_treated_as_empty(self) -> None:
        outputs = [
            {"role": "solver", "answer": "x"},
            {"role": "critic"},  # no answer
        ]
        # Only solver contributes → <2 non-empty → 1.0
        assert AlphaSignal.compute_post(outputs) == pytest.approx(1.0)


# ─────────────────────────── compute_post: rubric report ───────────────────────────


class TestRubricReport:
    """S5 H5: ``rubric_report=None`` defaults ``rubric_pass_rate`` to 1.0."""

    def test_none_rubric_defaults_pass_rate_one(self) -> None:
        outputs = [
            {"role": "solver", "answer": "4"},
            {"role": "critic", "answer": "4"},
        ]
        assert (
            AlphaSignal.compute_post(outputs, rubric_report=None)
            == pytest.approx(1.0)
        )

    def test_rubric_pass_rate_half(self) -> None:
        outputs = [
            {"role": "solver", "answer": "4"},
            {"role": "critic", "answer": "4"},
        ]
        report = {"pass_rate": 0.5}
        assert (
            AlphaSignal.compute_post(outputs, rubric_report=report)
            == pytest.approx(0.5)
        )

    def test_rubric_pass_rate_zero(self) -> None:
        outputs = [
            {"role": "solver", "answer": "4"},
            {"role": "critic", "answer": "4"},
        ]
        report = {"pass_rate": 0.0}
        assert (
            AlphaSignal.compute_post(outputs, rubric_report=report)
            == pytest.approx(0.0)
        )

    def test_rubric_pass_rate_clamped_above_one(self) -> None:
        outputs = [
            {"role": "solver", "answer": "4"},
            {"role": "critic", "answer": "4"},
        ]
        report = {"pass_rate": 1.5}  # malformed — clamp
        assert (
            AlphaSignal.compute_post(outputs, rubric_report=report)
            == pytest.approx(1.0)
        )

    def test_rubric_pass_rate_clamped_below_zero(self) -> None:
        outputs = [
            {"role": "solver", "answer": "4"},
            {"role": "critic", "answer": "4"},
        ]
        report = {"pass_rate": -0.5}
        assert (
            AlphaSignal.compute_post(outputs, rubric_report=report)
            == pytest.approx(0.0)
        )

    def test_malformed_rubric_pass_rate_defaults_one(self) -> None:
        outputs = [
            {"role": "solver", "answer": "4"},
            {"role": "critic", "answer": "4"},
        ]
        report = {"pass_rate": "high"}  # non-numeric
        assert (
            AlphaSignal.compute_post(outputs, rubric_report=report)
            == pytest.approx(1.0)
        )

    def test_rubric_missing_pass_rate_key_defaults_one(self) -> None:
        outputs = [
            {"role": "solver", "answer": "4"},
            {"role": "critic", "answer": "4"},
        ]
        report: dict[str, object] = {}
        assert (
            AlphaSignal.compute_post(outputs, rubric_report=report)
            == pytest.approx(1.0)
        )

    def test_non_dict_rubric_report_defaults_one(self) -> None:
        outputs = [
            {"role": "solver", "answer": "4"},
            {"role": "critic", "answer": "4"},
        ]
        assert (
            AlphaSignal.compute_post(outputs, rubric_report="bad")  # type: ignore[arg-type]
            == pytest.approx(1.0)
        )


# ─────────────────────────── Symmetry / composition ───────────────────────────


class TestComposition:
    def test_full_agreement_half_rubric_product(self) -> None:
        outputs = [
            {"role": "solver", "answer": "the answer is 4"},
            {"role": "critic", "answer": "the answer is 4"},
        ]
        # agreement=1.0, rubric=0.5 → product=0.5
        assert AlphaSignal.compute_post(
            outputs, rubric_report={"pass_rate": 0.5},
        ) == pytest.approx(0.5)

    def test_zero_agreement_times_anything_is_zero(self) -> None:
        outputs = [
            {"role": "solver", "answer": "apple"},
            {"role": "critic", "answer": "banana"},
        ]
        assert AlphaSignal.compute_post(
            outputs, rubric_report={"pass_rate": 1.0},
        ) == pytest.approx(0.0)


# ─────────────────────────── Forbidden imports regression ───────────────────────────


class TestForbiddenImports:
    """S5 H2 / M2 — no Platt / sklearn / scipy in this module."""

    @staticmethod
    def _import_lines() -> list[str]:
        import inspect

        import concinno.agent.confidence_fusion as m
        src = inspect.getsource(m)
        return [
            line.strip().lower()
            for line in src.splitlines()
            if line.lstrip().startswith(("import ", "from "))
        ]

    def test_no_sklearn(self) -> None:
        for line in self._import_lines():
            assert "sklearn" not in line

    def test_no_scipy(self) -> None:
        for line in self._import_lines():
            assert "scipy" not in line

    def test_no_platt(self) -> None:
        for line in self._import_lines():
            assert "platt" not in line

    def test_no_logprob(self) -> None:
        for line in self._import_lines():
            assert "logprob" not in line

    def test_no_numpy(self) -> None:
        for line in self._import_lines():
            assert "numpy" not in line
