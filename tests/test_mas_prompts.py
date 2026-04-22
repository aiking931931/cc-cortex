"""Tests for :mod:`concinno.agent.mas_prompts`.

The critical regression test is
:meth:`TestFallbackFraming::test_fallback_no_refusal_phrases`. MEMORY
#5 / #89 record that Gemma-class models echo refusal text back as
their own answer when a critic-fallback prompt *describes* a refusal
("solver produced no answer"). This test locks in the positive
framing so a future well-meaning edit can't silently re-introduce
that regression.
"""

from __future__ import annotations

from concinno.agent.mas_prompts import (
    DEFAULT_CRITIC_FALLBACK_PROMPT,
    DEFAULT_CRITIC_PROMPT,
    DEFAULT_JUDGE_PROMPT,
)

# ─────────────────────── Slot contract ───────────────────────


class TestSlotContract:
    def test_critic_prompt_has_question_slot(self) -> None:
        assert "{question}" in DEFAULT_CRITIC_PROMPT

    def test_critic_prompt_has_solver_answer_slot(self) -> None:
        assert "{solver_answer}" in DEFAULT_CRITIC_PROMPT

    def test_critic_prompt_has_solver_trace_slot(self) -> None:
        assert "{solver_trace_summary}" in DEFAULT_CRITIC_PROMPT

    def test_critic_fallback_has_question_slot(self) -> None:
        assert "{question}" in DEFAULT_CRITIC_FALLBACK_PROMPT

    def test_critic_fallback_omits_solver_slots(self) -> None:
        """Fallback is for the solver-blank path — must not reference solver."""
        assert "{solver_answer}" not in DEFAULT_CRITIC_FALLBACK_PROMPT
        assert "{solver_trace_summary}" not in DEFAULT_CRITIC_FALLBACK_PROMPT

    def test_judge_prompt_has_question_slot(self) -> None:
        assert "{question}" in DEFAULT_JUDGE_PROMPT

    def test_judge_prompt_has_response_slots(self) -> None:
        assert "{response_1}" in DEFAULT_JUDGE_PROMPT
        assert "{response_2}" in DEFAULT_JUDGE_PROMPT

    def test_judge_prompt_omits_solver_critic_labels(self) -> None:
        """H1 fix — judge never sees who is solver vs critic."""
        assert "{solver" not in DEFAULT_JUDGE_PROMPT
        assert "{critic" not in DEFAULT_JUDGE_PROMPT


# ─────────────────────── Regression: positive framing ───────────────────────


class TestFallbackFraming:
    """Guard against MEMORY #5 / #89 refusal-echo regression (H2).

    The fallback prompt fires when the solver blanks. If the prompt
    text contains refusal phrases ("I cannot", "solver produced no
    answer", "unable"), Gemma echoes those strings back as its own
    final answer and the cascade produces a null. This test locks in
    the prohibited-phrase list so any future rewrite of the fallback
    fails CI loudly rather than silently re-introducing the 6/20
    empty-raw pattern that killed Phase 1.
    """

    _PROHIBITED = [
        "I cannot",
        "I am unable",
        "unable",
        "no answer",
        "cannot find",
        "solver produced",
        "solver failed",
        "I do not know",
        "I don't know",
    ]

    def test_fallback_no_refusal_phrases(self) -> None:
        lowered = DEFAULT_CRITIC_FALLBACK_PROMPT.lower()
        for phrase in self._PROHIBITED:
            assert phrase.lower() not in lowered, (
                f"fallback prompt contains prohibited phrase: {phrase!r} "
                f"— MEMORY #5/#89 refusal-echo regression risk"
            )

    def test_fallback_is_positive_framed(self) -> None:
        """Prompt should ask for what the model *can* produce."""
        text = DEFAULT_CRITIC_FALLBACK_PROMPT.lower()
        # Positive framing anchors: best effort / concrete / commit.
        assert "best" in text or "commit" in text or "concrete" in text

    def test_fallback_demands_final_answer_sentinel(self) -> None:
        """GAIA-style sentinel so the orchestrator parser can scrape it."""
        assert "FINAL ANSWER" in DEFAULT_CRITIC_FALLBACK_PROMPT


# ─────────────────────── Format smoke ───────────────────────


class TestFormatRenders:
    """Prompts render cleanly with their declared slots."""

    def test_critic_prompt_renders(self) -> None:
        out = DEFAULT_CRITIC_PROMPT.format(
            question="q",
            solver_answer="sa",
            solver_trace_summary="ts",
        )
        assert "q" in out
        assert "sa" in out
        assert "ts" in out

    def test_critic_fallback_renders_on_question_only(self) -> None:
        out = DEFAULT_CRITIC_FALLBACK_PROMPT.format(question="qq")
        assert "qq" in out

    def test_judge_prompt_renders(self) -> None:
        out = DEFAULT_JUDGE_PROMPT.format(
            question="q",
            response_1="one",
            response_2="two",
        )
        assert "one" in out
        assert "two" in out
        # Label ordering lives in the template structure, not in the
        # data — so "Response 1" / "Response 2" substrings present.
        assert "Response 1" in out
        assert "Response 2" in out
