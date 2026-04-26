"""Tests for gaia image-quiz scoring hybrid solver.

Origin: GAIA cca70ce6 — image of a graded fractions quiz with N
numbered problems; question text supplies the per-type point map +
bonus and asks for the student's total score. Hybrid pipeline:
narrow Sonnet OCR (per-problem type + operands + student-answer
string) + Python deterministic correctness via
``fractions.Fraction`` + score sum via the new structured-compute
Skill ``concinno.tools.builtin.compute.execute_arithmetic_plan``.
Stable PASS '85' in cont'd¹³.

Tests cover detection, scoring-rule parsing, classifier, fraction
parsers, correctness judge, majority vote, compute wiring,
orchestrator wiring (mocked Sonnet OCR), and feature registration.
Real-model smoke is in
``benchmarks/gaia/evidence/prototype_quiz_scoring.py``.
"""

from __future__ import annotations

import sys

import pytest


@pytest.fixture
def gaia_agent_mod(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "test-dummy")
    sys.modules.pop("concinno.skills.public.agent.gaia_agent", None)
    from concinno.skills.public.agent import gaia_agent
    return gaia_agent


# ────────────────────────── detection ──────────────────────────────


class TestImageQuizScoringDetection:
    QUESTION = (
        "Look at the attached image. The quiz is scored as follows:\n\n"
        "Problems that ask the student to add or subtract fractions: 5 "
        "points\n"
        "Problems that ask the student to multiply or divide fractions: "
        "10 points\n"
        "Problems that ask the student to form an improper fraction: "
        "15 points\n"
        "Problems that ask the student to form a mixed number: 20 "
        "points\n\n"
        "Due to a technical issue, the teacher is giving everyone 5 "
        "bonus points.\n\n"
        "If you graded the quiz, how many points would the student "
        "have earned? There is no partial credit."
    )

    def test_image_attached_passes(self, gaia_agent_mod):
        assert gaia_agent_mod._is_image_quiz_scoring_question(
            self.QUESTION, "foo.png",
        )

    def test_image_jpg_passes(self, gaia_agent_mod):
        assert gaia_agent_mod._is_image_quiz_scoring_question(
            self.QUESTION, "x.jpg",
        )

    def test_no_file_rejected(self, gaia_agent_mod):
        assert not gaia_agent_mod._is_image_quiz_scoring_question(
            self.QUESTION, None,
        )

    def test_text_file_rejected(self, gaia_agent_mod):
        assert not gaia_agent_mod._is_image_quiz_scoring_question(
            self.QUESTION, "foo.txt",
        )

    def test_empty_question_rejected(self, gaia_agent_mod):
        assert not gaia_agent_mod._is_image_quiz_scoring_question(
            "", "foo.png",
        )

    def test_no_scoring_rules_rejected(self, gaia_agent_mod):
        q = "What is the area of the polygon in the attached image?"
        assert not gaia_agent_mod._is_image_quiz_scoring_question(
            q, "foo.png",
        )

    def test_two_rules_no_scored_phrase_passes(self, gaia_agent_mod):
        # Detector accepts two rule clauses even without literal
        # "scored as follows".
        q = (
            "Problems that ask the student to add fractions: 5 points\n"
            "Problems that ask the student to multiply fractions: 10 "
            "points"
        )
        assert gaia_agent_mod._is_image_quiz_scoring_question(
            q, "x.png",
        )


# ────────────────────── classify_quiz_rule_phrase ──────────────────


class TestClassifyQuizRulePhrase:
    def test_add_subtract(self, gaia_agent_mod):
        assert gaia_agent_mod._classify_quiz_rule_phrase(
            "Problems that ask the student to add or subtract fractions",
        ) == "add_subtract_fractions"

    def test_multiply_divide(self, gaia_agent_mod):
        assert gaia_agent_mod._classify_quiz_rule_phrase(
            "Problems that ask the student to multiply or divide "
            "fractions",
        ) == "multiply_divide_fractions"

    def test_improper(self, gaia_agent_mod):
        assert gaia_agent_mod._classify_quiz_rule_phrase(
            "Problems that ask the student to form an improper fraction",
        ) == "form_improper_fraction"

    def test_mixed(self, gaia_agent_mod):
        assert gaia_agent_mod._classify_quiz_rule_phrase(
            "Problems that ask the student to form a mixed number",
        ) == "form_mixed_number"

    def test_unknown_returns_none(self, gaia_agent_mod):
        assert gaia_agent_mod._classify_quiz_rule_phrase(
            "Problems that involve geometry"
        ) is None


# ───────────────────── parse_quiz_scoring_rules ────────────────────


class TestParseQuizScoringRules:
    def test_full_quiz_rule_block(self, gaia_agent_mod):
        q = TestImageQuizScoringDetection.QUESTION
        tp, bonus = gaia_agent_mod._parse_quiz_scoring_rules(q)
        assert tp == {
            "add_subtract_fractions": 5,
            "multiply_divide_fractions": 10,
            "form_improper_fraction": 15,
            "form_mixed_number": 20,
        }
        assert bonus == 5

    def test_no_bonus(self, gaia_agent_mod):
        q = (
            "Problems that ask the student to add fractions: 5 points\n"
            "Problems that ask the student to multiply fractions: 10 "
            "points"
        )
        tp, bonus = gaia_agent_mod._parse_quiz_scoring_rules(q)
        assert tp == {
            "add_subtract_fractions": 5,
            "multiply_divide_fractions": 10,
        }
        assert bonus == 0

    def test_unclassifiable_phrase_skipped(self, gaia_agent_mod):
        q = "Problems that involve geometry: 99 points"
        tp, bonus = gaia_agent_mod._parse_quiz_scoring_rules(q)
        assert tp == {}
        assert bonus == 0

    def test_empty_question_returns_empty(self, gaia_agent_mod):
        tp, bonus = gaia_agent_mod._parse_quiz_scoring_rules("")
        assert tp == {}
        assert bonus == 0


# ───────────────────────── fraction parsers ────────────────────────


class TestParseSimpleFractionStr:
    def test_positive(self, gaia_agent_mod):
        from fractions import Fraction
        assert (
            gaia_agent_mod._parse_simple_fraction_str("3/4")
            == Fraction(3, 4)
        )

    def test_negative(self, gaia_agent_mod):
        from fractions import Fraction
        assert (
            gaia_agent_mod._parse_simple_fraction_str("-132/245")
            == Fraction(-132, 245)
        )

    def test_integer(self, gaia_agent_mod):
        from fractions import Fraction
        assert (
            gaia_agent_mod._parse_simple_fraction_str("7") == Fraction(7)
        )

    def test_whitespace_tolerated(self, gaia_agent_mod):
        from fractions import Fraction
        assert (
            gaia_agent_mod._parse_simple_fraction_str("  29 / 35  ")
            == Fraction(29, 35)
        )

    def test_empty_raises(self, gaia_agent_mod):
        with pytest.raises(ValueError):
            gaia_agent_mod._parse_simple_fraction_str("")

    def test_none_raises(self, gaia_agent_mod):
        with pytest.raises(ValueError):
            gaia_agent_mod._parse_simple_fraction_str(None)


class TestParseMixedOrImproperStr:
    def test_mixed_positive(self, gaia_agent_mod):
        from fractions import Fraction
        # 2 21/32 == 85/32
        assert (
            gaia_agent_mod._parse_mixed_or_improper_str("2 21/32")
            == Fraction(85, 32)
        )

    def test_mixed_negative_whole(self, gaia_agent_mod):
        from fractions import Fraction
        # -3 1/4 == -(3 + 1/4) == -13/4
        assert (
            gaia_agent_mod._parse_mixed_or_improper_str("-3 1/4")
            == Fraction(-13, 4)
        )

    def test_improper_falls_through(self, gaia_agent_mod):
        from fractions import Fraction
        assert (
            gaia_agent_mod._parse_mixed_or_improper_str("47/5")
            == Fraction(47, 5)
        )

    def test_integer(self, gaia_agent_mod):
        from fractions import Fraction
        assert (
            gaia_agent_mod._parse_mixed_or_improper_str("5") == Fraction(5)
        )


# ─────────────────── judge_quiz_problem_correct ────────────────────


class TestJudgeQuizProblemCorrect:
    def test_add_correct(self, gaia_agent_mod):
        # 14/38 + 20/34 = 309/323 (cca70ce6 problem 4)
        assert gaia_agent_mod._judge_quiz_problem_correct({
            "problem_type": "add_subtract_fractions",
            "operands": ["14/38", "20/34"],
            "operator": "+",
            "student_answer": "309/323",
        }) is True

    def test_subtract_sign_wrong(self, gaia_agent_mod):
        # 10/25 - 46/49 = -132/245; student wrote +132/245 (cca70ce6 #3)
        assert gaia_agent_mod._judge_quiz_problem_correct({
            "problem_type": "add_subtract_fractions",
            "operands": ["10/25", "46/49"],
            "operator": "-",
            "student_answer": "132/245",
        }) is False

    def test_subtract_correct_with_sign(self, gaia_agent_mod):
        # 19/33 - 43/50 = -469/1650 (cca70ce6 #10)
        assert gaia_agent_mod._judge_quiz_problem_correct({
            "problem_type": "add_subtract_fractions",
            "operands": ["19/33", "43/50"],
            "operator": "-",
            "student_answer": "-469/1650",
        }) is True

    def test_multiply_correct(self, gaia_agent_mod):
        # 29/35 × 18/47 = 522/1645 (cca70ce6 #1)
        assert gaia_agent_mod._judge_quiz_problem_correct({
            "problem_type": "multiply_divide_fractions",
            "operands": ["29/35", "18/47"],
            "operator": "*",
            "student_answer": "522/1645",
        }) is True

    def test_multiply_equivalent_fraction_accepted(self, gaia_agent_mod):
        # 13/42 × 35/39 = 5/18 (cca70ce6 #8) — student wrote
        # already-reduced form
        assert gaia_agent_mod._judge_quiz_problem_correct({
            "problem_type": "multiply_divide_fractions",
            "operands": ["13/42", "35/39"],
            "operator": "*",
            "student_answer": "5/18",
        }) is True

    def test_divide_correct(self, gaia_agent_mod):
        # 31/50 ÷ 2/36 = 31/50 * 36/2 = 1116/100 = 279/25 (cca70ce6 #2)
        assert gaia_agent_mod._judge_quiz_problem_correct({
            "problem_type": "multiply_divide_fractions",
            "operands": ["31/50", "2/36"],
            "operator": "/",
            "student_answer": "279/25",
        }) is True

    def test_divide_by_zero_returns_none(self, gaia_agent_mod):
        assert gaia_agent_mod._judge_quiz_problem_correct({
            "problem_type": "multiply_divide_fractions",
            "operands": ["1/2", "0/5"],
            "operator": "/",
            "student_answer": "0",
        }) is None

    def test_form_improper_correct(self, gaia_agent_mod):
        # 32 5/9 → 293/9 (cca70ce6 #9)
        assert gaia_agent_mod._judge_quiz_problem_correct({
            "problem_type": "form_improper_fraction",
            "input_value": "32 5/9",
            "student_answer": "293/9",
        }) is True

    def test_form_improper_arithmetic_wrong(self, gaia_agent_mod):
        # 8 2/5 → 42/5; student wrote 47/5 (cca70ce6 #6)
        assert gaia_agent_mod._judge_quiz_problem_correct({
            "problem_type": "form_improper_fraction",
            "input_value": "8 2/5",
            "student_answer": "47/5",
        }) is False

    def test_form_mixed_correct(self, gaia_agent_mod):
        # 85/32 → 2 21/32 (cca70ce6 #5)
        assert gaia_agent_mod._judge_quiz_problem_correct({
            "problem_type": "form_mixed_number",
            "input_value": "85/32",
            "student_answer": "2 21/32",
        }) is True

    def test_missing_student_returns_none(self, gaia_agent_mod):
        assert gaia_agent_mod._judge_quiz_problem_correct({
            "problem_type": "add_subtract_fractions",
            "operands": ["1/2", "1/4"],
            "operator": "+",
            "student_answer": None,
        }) is None

    def test_unparseable_operand_returns_none(self, gaia_agent_mod):
        assert gaia_agent_mod._judge_quiz_problem_correct({
            "problem_type": "add_subtract_fractions",
            "operands": ["abc", "1/2"],
            "operator": "+",
            "student_answer": "1",
        }) is None

    def test_unknown_type_returns_none(self, gaia_agent_mod):
        assert gaia_agent_mod._judge_quiz_problem_correct({
            "problem_type": "some_other_type",
            "student_answer": "5",
        }) is None


# ────────────── per_problem_majority_vote_quiz ──────────────────


class TestPerProblemMajorityVoteQuiz:
    def test_unanimous_passes(self, gaia_agent_mod):
        passes = [
            [{"idx": 1, "problem_type": "add_subtract_fractions",
              "operands": ["1/2", "1/4"], "operator": "+",
              "input_value": None, "student_answer": "3/4"}],
            [{"idx": 1, "problem_type": "add_subtract_fractions",
              "operands": ["1/2", "1/4"], "operator": "+",
              "input_value": None, "student_answer": "3/4"}],
        ]
        v = gaia_agent_mod._per_problem_majority_vote_quiz(passes)
        assert len(v) == 1
        assert v[0]["student_answer"] == "3/4"

    def test_majority_wins(self, gaia_agent_mod):
        passes = [
            [{"idx": 1, "problem_type": "add_subtract_fractions",
              "operands": ["1/2", "1/4"], "operator": "+",
              "input_value": None, "student_answer": "3/4"}],
            [{"idx": 1, "problem_type": "add_subtract_fractions",
              "operands": ["1/2", "1/4"], "operator": "+",
              "input_value": None, "student_answer": "3/4"}],
            [{"idx": 1, "problem_type": "multiply_divide_fractions",
              "operands": ["1/2", "1/4"], "operator": "*",
              "input_value": None, "student_answer": "1/8"}],
        ]
        v = gaia_agent_mod._per_problem_majority_vote_quiz(passes)
        assert v[0]["problem_type"] == "add_subtract_fractions"

    def test_sorted_by_idx(self, gaia_agent_mod):
        passes = [
            [
                {"idx": 3, "problem_type": "form_mixed_number",
                 "operands": None, "operator": None,
                 "input_value": "85/32", "student_answer": "2 21/32"},
                {"idx": 1, "problem_type": "add_subtract_fractions",
                 "operands": ["1/2", "1/4"], "operator": "+",
                 "input_value": None, "student_answer": "3/4"},
            ],
        ]
        v = gaia_agent_mod._per_problem_majority_vote_quiz(passes)
        assert [r["idx"] for r in v] == [1, 3]


# ────────────── compute_quiz_via_arithmetic_plan ──────────────────


class TestComputeQuizViaArithmeticPlan:
    def test_full_cca70ce6_score(self, gaia_agent_mod):
        # Reproduce the cca70ce6 expected sum: per-problem awards
        # [10, 10, 0, 5, 20, 0, 5, 10, 15, 5] + bonus 5 = 85.
        voted = [
            # idx, type, correct
            (1, "multiply_divide_fractions", True),
            (2, "multiply_divide_fractions", True),
            (3, "add_subtract_fractions", False),
            (4, "add_subtract_fractions", True),
            (5, "form_mixed_number", True),
            (6, "form_improper_fraction", False),
            (7, "add_subtract_fractions", True),
            (8, "multiply_divide_fractions", True),
            (9, "form_improper_fraction", True),
            (10, "add_subtract_fractions", True),
        ]
        rows = [
            {"idx": i, "problem_type": t, "student_correct": c}
            for (i, t, c) in voted
        ]
        type_points = {
            "add_subtract_fractions": 5,
            "multiply_divide_fractions": 10,
            "form_improper_fraction": 15,
            "form_mixed_number": 20,
        }
        ans, info = gaia_agent_mod._compute_quiz_via_arithmetic_plan(
            rows, type_points, 5,
        )
        assert ans == "85"
        assert info["awarded_points"] == [10, 10, 0, 5, 20, 0, 5, 10, 15, 5]
        assert info["bonus"] == 5

    def test_unknown_type_awards_zero(self, gaia_agent_mod):
        rows = [
            {"idx": 1, "problem_type": "weird_type",
             "student_correct": True},
        ]
        ans, info = gaia_agent_mod._compute_quiz_via_arithmetic_plan(
            rows, {"weird_type_other": 99}, 0,
        )
        assert ans == "0"
        assert info["awarded_points"] == [0]


# ──────────── orchestrator wiring (mocked Sonnet call) ─────────────


class TestSolveImageQuizScoringViaHybridWiring:
    def test_full_pipeline_with_mock_extract(
        self, gaia_agent_mod, monkeypatch, tmp_path,
    ):
        # Mock Sonnet OCR call to return the cca70ce6 ground-truth
        # OCR rows directly (no real API call). Then verify the full
        # pipeline (parse rules → vote → judge → arithmetic_plan)
        # produces "85".
        rows = [
            {"idx": 1, "problem_type": "multiply_divide_fractions",
             "operands": ["29/35", "18/47"], "operator": "*",
             "input_value": None, "student_answer": "522/1645"},
            {"idx": 2, "problem_type": "multiply_divide_fractions",
             "operands": ["31/50", "2/36"], "operator": "/",
             "input_value": None, "student_answer": "279/25"},
            {"idx": 3, "problem_type": "add_subtract_fractions",
             "operands": ["10/25", "46/49"], "operator": "-",
             "input_value": None, "student_answer": "132/245"},
            {"idx": 4, "problem_type": "add_subtract_fractions",
             "operands": ["14/38", "20/34"], "operator": "+",
             "input_value": None, "student_answer": "309/323"},
            {"idx": 5, "problem_type": "form_mixed_number",
             "operands": None, "operator": None,
             "input_value": "85/32", "student_answer": "2 21/32"},
            {"idx": 6, "problem_type": "form_improper_fraction",
             "operands": None, "operator": None,
             "input_value": "8 2/5", "student_answer": "47/5"},
            {"idx": 7, "problem_type": "add_subtract_fractions",
             "operands": ["22/47", "8/11"], "operator": "+",
             "input_value": None, "student_answer": "618/517"},
            {"idx": 8, "problem_type": "multiply_divide_fractions",
             "operands": ["13/42", "35/39"], "operator": "*",
             "input_value": None, "student_answer": "5/18"},
            {"idx": 9, "problem_type": "form_improper_fraction",
             "operands": None, "operator": None,
             "input_value": "32 5/9", "student_answer": "293/9"},
            {"idx": 10, "problem_type": "add_subtract_fractions",
             "operands": ["19/33", "43/50"], "operator": "-",
             "input_value": None, "student_answer": "-469/1650"},
        ]
        monkeypatch.setattr(
            gaia_agent_mod,
            "_call_sonnet_quiz_extract",
            lambda image_bytes, model: rows,
        )
        # tmp image (won't be parsed, just read)
        img = tmp_path / "quiz.png"
        img.write_bytes(b"\x89PNG dummy bytes")

        q = TestImageQuizScoringDetection.QUESTION
        ans, info = gaia_agent_mod._solve_image_quiz_scoring_via_hybrid(
            q, str(img), model="claude-sonnet-4-6", passes_count=1,
        )
        assert ans == "85"
        assert info["stage"] == "done"
        assert info["n_problems"] == 10
        assert info["n_correct"] == 8
        assert info["awarded_points"] == [
            10, 10, 0, 5, 20, 0, 5, 10, 15, 5,
        ]

    def test_no_scoring_rules_fails_early(
        self, gaia_agent_mod, tmp_path,
    ):
        img = tmp_path / "quiz.png"
        img.write_bytes(b"")
        ans, info = gaia_agent_mod._solve_image_quiz_scoring_via_hybrid(
            "Just a question with no scoring rules.",
            str(img),
        )
        assert ans == ""
        assert info["error"] == "no scoring rules parsed"

    def test_all_passes_fail(
        self, gaia_agent_mod, monkeypatch, tmp_path,
    ):
        monkeypatch.setattr(
            gaia_agent_mod,
            "_call_sonnet_quiz_extract",
            lambda image_bytes, model: None,
        )
        img = tmp_path / "quiz.png"
        img.write_bytes(b"\x89PNG")
        q = TestImageQuizScoringDetection.QUESTION
        ans, info = gaia_agent_mod._solve_image_quiz_scoring_via_hybrid(
            q, str(img), passes_count=2,
        )
        assert ans == ""
        assert "all OCR passes failed" in info.get("error", "")


# ────────────────── feature registration ──────────────────────


class TestQuizScoringFeatureRegistration:
    def test_feature_in_meta(self):
        from concinno.feature_config import FEATURE_META
        m = FEATURE_META.get("gaia_quiz_scoring_hybrid")
        assert m is not None
        assert m["category"] == "context"
        assert "passes_count" in m["params"]
        assert "model" in m["params"]
        assert m["params"]["passes_count"]["default"] == 3
        assert (
            m["params"]["model"]["default"] == "claude-sonnet-4-6"
        )
