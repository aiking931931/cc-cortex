"""Tests for concinno.tools.builtin.dspy_optimizer.

All tests mock the LM — zero API calls, zero credit burn.
"""

from __future__ import annotations

import pytest

# ── Helpers ──────────────────────────────────────────────────────────────────

class _MockExample:
    """Minimal stand-in for dspy.Example in metric tests."""
    def __init__(self, gold_answer: str, **kwargs):
        self.gold_answer = gold_answer
        for k, v in kwargs.items():
            setattr(self, k, v)


class _MockPrediction:
    """Minimal stand-in for dspy.Prediction in metric tests."""
    def __init__(self, final_answer: str):
        self.final_answer = final_answer


# ── Import smoke ──────────────────────────────────────────────────────────────

class TestImport:
    def test_importable_from_tools_builtin(self):
        from concinno.tools.builtin.dspy_optimizer import DspyOptimizer  # noqa: F401

    def test_all_public_exports_importable(self):
        from concinno.tools.builtin.dspy_optimizer import (  # noqa: F401
            CriticModule,
            DspyOptimizer,
            JudgeModule,
            build_critic_examples,
            build_judge_examples,
            gaia_exact_match,
            normalize_answer,
        )

    def test_importable_via_package_all(self):
        import concinno.tools.builtin.dspy_optimizer as mod
        for name in mod.__all__:
            assert hasattr(mod, name), f"__all__ exports {name!r} but it's missing"


# ── normalize_answer ──────────────────────────────────────────────────────────

class TestNormalizeAnswer:
    def test_empty_string(self):
        from concinno.tools.builtin.dspy_optimizer import normalize_answer
        assert normalize_answer("") == ""

    def test_strips_trailing_whitespace(self):
        from concinno.tools.builtin.dspy_optimizer import normalize_answer
        assert normalize_answer("  Paris  ") == "paris"

    def test_lowercases(self):
        from concinno.tools.builtin.dspy_optimizer import normalize_answer
        assert normalize_answer("PARIS") == "paris"

    def test_strips_trailing_punctuation(self):
        from concinno.tools.builtin.dspy_optimizer import normalize_answer
        assert normalize_answer("Paris.") == "paris"
        assert normalize_answer("Paris!") == "paris"
        assert normalize_answer("Paris,") == "paris"

    def test_collapses_internal_whitespace(self):
        from concinno.tools.builtin.dspy_optimizer import normalize_answer
        assert normalize_answer("New  York") == "new york"

    def test_strips_trailing_float_zero(self):
        from concinno.tools.builtin.dspy_optimizer import normalize_answer
        assert normalize_answer("42.0") == "42"
        assert normalize_answer("3.00") == "3"
        assert normalize_answer("-7.0") == "-7"

    def test_leaves_non_whole_float_unchanged(self):
        from concinno.tools.builtin.dspy_optimizer import normalize_answer
        assert normalize_answer("3.14") == "3.14"

    def test_unicode_normalization(self):
        from concinno.tools.builtin.dspy_optimizer import normalize_answer
        # Full-width digit → ASCII digit via NFKC
        assert normalize_answer("４２") == "42"


# ── gaia_exact_match ──────────────────────────────────────────────────────────

class TestGaiaExactMatch:
    def test_correct_prediction_returns_one(self):
        from concinno.tools.builtin.dspy_optimizer import gaia_exact_match
        ex = _MockExample(gold_answer="paris")
        pred = _MockPrediction("Paris")
        assert gaia_exact_match(ex, pred) == 1.0

    def test_incorrect_prediction_returns_zero(self):
        from concinno.tools.builtin.dspy_optimizer import gaia_exact_match
        ex = _MockExample(gold_answer="paris")
        pred = _MockPrediction("london")
        assert gaia_exact_match(ex, pred) == 0.0

    def test_normalized_numeric_match(self):
        from concinno.tools.builtin.dspy_optimizer import gaia_exact_match
        ex = _MockExample(gold_answer="42")
        pred = _MockPrediction("42.0")
        assert gaia_exact_match(ex, pred) == 1.0

    def test_missing_gold_returns_zero(self):
        from concinno.tools.builtin.dspy_optimizer import gaia_exact_match

        class NoGold:
            pass

        pred = _MockPrediction("paris")
        assert gaia_exact_match(NoGold(), pred) == 0.0

    def test_returns_float(self):
        from concinno.tools.builtin.dspy_optimizer import gaia_exact_match
        ex = _MockExample(gold_answer="42")
        pred = _MockPrediction("42")
        result = gaia_exact_match(ex, pred)
        assert isinstance(result, float)

    def test_trace_param_accepted(self):
        from concinno.tools.builtin.dspy_optimizer import gaia_exact_match
        ex = _MockExample(gold_answer="42")
        pred = _MockPrediction("42")
        # trace=None is the default; should not raise
        assert gaia_exact_match(ex, pred, trace=None) == 1.0


# ── build_*_examples ─────────────────────────────────────────────────────────

class TestBuildExamples:
    def test_build_critic_examples_length(self):
        from concinno.tools.builtin.dspy_optimizer import build_critic_examples
        records = [
            {"question": "Q1", "solver_answer": "A1", "gold": "a1"},
            {"question": "Q2", "solver_answer": "A2", "gold": "a2"},
        ]
        examples = build_critic_examples(records)
        assert len(examples) == 2

    def test_build_critic_examples_fields(self):
        from concinno.tools.builtin.dspy_optimizer import build_critic_examples
        records = [{"question": "Q?", "solver_answer": "A", "gold": "g"}]
        ex = build_critic_examples(records)[0]
        assert ex.question == "Q?"
        assert ex.solver_answer == "A"
        assert ex.gold_answer == "g"

    def test_build_critic_examples_optional_trace(self):
        from concinno.tools.builtin.dspy_optimizer import build_critic_examples
        records = [{"question": "Q?", "gold": "g"}]  # no solver_answer or trace
        ex = build_critic_examples(records)[0]
        assert ex.solver_answer == ""
        assert ex.solver_trace_summary == ""

    def test_build_judge_examples_fields(self):
        from concinno.tools.builtin.dspy_optimizer import build_judge_examples
        records = [{"question": "Q?", "response_1": "R1", "response_2": "R2", "gold": "r1"}]
        ex = build_judge_examples(records)[0]
        assert ex.question == "Q?"
        assert ex.response_1 == "R1"
        assert ex.response_2 == "R2"
        assert ex.gold_answer == "r1"


# ── CriticModule / JudgeModule (mock LM) ─────────────────────────────────────

class TestCriticModule:
    def test_instantiation(self):
        import dspy
        from dspy.utils import DummyLM

        from concinno.tools.builtin.dspy_optimizer import CriticModule
        dspy.configure(lm=DummyLM([{"final_answer": "paris", "reasoning": "test"}]))
        m = CriticModule()
        assert m is not None

    def test_is_dspy_module(self):
        import dspy
        from dspy.utils import DummyLM

        from concinno.tools.builtin.dspy_optimizer import CriticModule
        dspy.configure(lm=DummyLM([{"final_answer": "paris", "reasoning": "test"}]))
        m = CriticModule()
        assert isinstance(m, dspy.Module)

    def test_forward_returns_prediction(self):
        import dspy
        from dspy.utils import DummyLM

        from concinno.tools.builtin.dspy_optimizer import CriticModule
        dspy.configure(lm=DummyLM([{"final_answer": "paris", "reasoning": "capital"}]))
        m = CriticModule()
        result = m(question="Capital of France?", solver_answer="Paris",
                   solver_trace_summary="")
        assert result.final_answer == "paris"


class TestJudgeModule:
    def test_instantiation(self):
        import dspy
        from dspy.utils import DummyLM

        from concinno.tools.builtin.dspy_optimizer import JudgeModule
        dspy.configure(lm=DummyLM([{"final_answer": "42", "reasoning": "test"}]))
        m = JudgeModule()
        assert m is not None

    def test_is_dspy_module(self):
        import dspy
        from dspy.utils import DummyLM

        from concinno.tools.builtin.dspy_optimizer import JudgeModule
        dspy.configure(lm=DummyLM([{"final_answer": "42", "reasoning": "test"}]))
        m = JudgeModule()
        assert isinstance(m, dspy.Module)

    def test_forward_returns_prediction(self):
        import dspy
        from dspy.utils import DummyLM

        from concinno.tools.builtin.dspy_optimizer import JudgeModule
        dspy.configure(lm=DummyLM([{"final_answer": "42", "reasoning": "correct"}]))
        m = JudgeModule()
        result = m(question="6×7?", response_1="42", response_2="48")
        assert result.final_answer == "42"


# ── DspyOptimizer feature flag ────────────────────────────────────────────────

class TestDspyOptimizerFeatureFlag:
    def test_feature_off_returns_original_module(self):
        """When feature is disabled, optimize_prompt returns original module unchanged."""
        import dspy
        from dspy.utils import DummyLM

        from concinno.tools.builtin.dspy_optimizer import (
            CriticModule,
            DspyOptimizer,
            build_critic_examples,
            gaia_exact_match,
        )
        dspy.configure(lm=DummyLM([{"final_answer": "paris", "reasoning": "test"}]))

        optimizer = DspyOptimizer()
        # _feature_enabled() will return False because dspy_prompt_optimization
        # is default-off in FEATURE_META, and get_config() in test env returns defaults.
        module = CriticModule()
        examples = build_critic_examples([
            {"question": "Q?", "solver_answer": "A", "gold": "a"},
        ])
        result = optimizer.optimize_prompt(module, examples, gaia_exact_match)
        # Feature off → original module returned
        assert result is module

    def test_empty_training_raises_when_feature_on(self, monkeypatch):
        """Empty training_examples raises ValueError when feature is enabled."""
        import dspy
        from dspy.utils import DummyLM

        from concinno.tools.builtin.dspy_optimizer import (
            CriticModule,
            DspyOptimizer,
            gaia_exact_match,
        )
        dspy.configure(lm=DummyLM([{"final_answer": "paris", "reasoning": "test"}]))

        optimizer = DspyOptimizer()
        # Patch feature check to True
        monkeypatch.setattr(optimizer, "_feature_enabled", lambda: True)

        module = CriticModule()
        with pytest.raises(ValueError, match="training_examples must not be empty"):
            optimizer.optimize_prompt(module, [], gaia_exact_match)

    def test_non_module_raises_type_error_when_feature_on(self, monkeypatch):
        """Non-dspy.Module argument raises TypeError when feature is enabled."""
        from concinno.tools.builtin.dspy_optimizer import DspyOptimizer, gaia_exact_match

        optimizer = DspyOptimizer()
        monkeypatch.setattr(optimizer, "_feature_enabled", lambda: True)

        with pytest.raises(TypeError, match="dspy.Module subclass"):
            optimizer.optimize_prompt(
                "not a module",  # wrong type
                [object()],
                gaia_exact_match,
            )
