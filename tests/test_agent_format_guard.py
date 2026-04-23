"""Unit tests for concinno.agent.format_guard.

Verifies:
* Each failure mode triggers on its intended pattern.
* PASS-like clean answers return None (zero false positives).
* No-cheat contract — inputs are raw + extracted_answer only.
* ``FORMAT_RETRY_REMINDER`` is question-agnostic (contains no
  answer-like keywords that could leak).
"""

from __future__ import annotations

from concinno.agent.format_guard import (
    FORMAT_RETRY_REMINDER,
    FormatFailureMode,
    classify_output_format,
)

# ─── empty mode ─────────────────────────────────────


def test_empty_raw_returns_empty_mode() -> None:
    assert classify_output_format("", "") is FormatFailureMode.EMPTY


def test_whitespace_raw_returns_empty_mode() -> None:
    assert classify_output_format("   \n\n\t", "") is FormatFailureMode.EMPTY


def test_none_raw_returns_empty_mode() -> None:
    assert classify_output_format(None, "") is FormatFailureMode.EMPTY  # type: ignore[arg-type]


# ─── retry_talk mode ────────────────────────────────


def test_retry_talk_wait() -> None:
    raw = "stream has content"
    ans = "Wait, is there any other Amphiprion species that could fit"
    assert classify_output_format(raw, ans) is FormatFailureMode.RETRY_TALK


def test_retry_talk_lets_try() -> None:
    raw = "stream content"
    ans = 'Let\'s try searching for "SPFMV" "SPCSV"'
    assert classify_output_format(raw, ans) is FormatFailureMode.RETRY_TALK


def test_retry_talk_and_ill() -> None:
    raw = "stream content"
    ans = 'And I\'ll search for "Frozen/Chilled section" that fits'
    assert classify_output_format(raw, ans) is FormatFailureMode.RETRY_TALK


def test_retry_talk_searching_for() -> None:
    raw = "stream content"
    ans = "Searching for the Cambridge dictionary entry…"
    assert classify_output_format(raw, ans) is FormatFailureMode.RETRY_TALK


def test_retry_talk_based_on() -> None:
    raw = "stream content"
    ans = "Based on the search results, I need to refine my query"
    assert classify_output_format(raw, ans) is FormatFailureMode.RETRY_TALK


def test_retry_talk_let_me() -> None:
    raw = "stream content"
    ans = "Let me check the primary source one more time"
    assert classify_output_format(raw, ans) is FormatFailureMode.RETRY_TALK


# ─── quote_dump mode ────────────────────────────────


def test_quote_dump_four_tokens() -> None:
    raw = "stream"
    ans = 'average p-value" "Nature" "2020" "0.0'
    assert classify_output_format(raw, ans) is FormatFailureMode.QUOTE_DUMP


def test_quote_dump_does_not_trigger_on_single_quote() -> None:
    # A real quoted answer like a book title or epitaph shouldn't
    # be confused with a search-query dump.
    raw = "stream"
    ans = 'The line reads "So we had to let it die."'
    assert classify_output_format(raw, ans) is not FormatFailureMode.QUOTE_DUMP


# ─── special_token mode ─────────────────────────────


def test_special_token_tool_call_leak() -> None:
    raw = "stream"
    ans = '<|"|>}<tool_call|>'
    assert classify_output_format(raw, ans) is FormatFailureMode.SPECIAL_TOKEN


def test_special_token_channel_leak() -> None:
    raw = "stream"
    ans = "<channel|>"
    assert classify_output_format(raw, ans) is FormatFailureMode.SPECIAL_TOKEN


def test_special_token_im_start_leak() -> None:
    raw = "stream"
    ans = "<|im_start|>some stuff"
    assert classify_output_format(raw, ans) is FormatFailureMode.SPECIAL_TOKEN


def test_special_token_NOT_triggered_by_raw_reasoning_marker() -> None:
    """Gemma4-Q4_K_M streams ``<channel|>`` in raw reasoning for every
    question. The classifier must key off the EXTRACTED answer, so
    normal reasoning markers in raw don't false-positive."""
    raw = "<channel|>The user wants …<end|>final answer 42"
    ans = "42"  # extractor got the clean value out
    assert classify_output_format(raw, ans) is None


# ─── no-false-positive regression (actual PASS baselines) ──


def test_pass_egalitarian() -> None:
    raw = "<channel|>long reasoning<end|>FINAL ANSWER: egalitarian"
    ans = "egalitarian"
    assert classify_output_format(raw, ans) is None


def test_pass_number() -> None:
    raw = "<channel|>reasoning<end|>FINAL ANSWER: 142"
    ans = "142"
    assert classify_output_format(raw, ans) is None


def test_pass_decimal() -> None:
    raw = "reasoning stream"
    ans = "0.1777"
    assert classify_output_format(raw, ans) is None


def test_pass_multi_word_title() -> None:
    raw = "reasoning stream"
    ans = "Time-Parking 2: Parallel Universe"
    assert classify_output_format(raw, ans) is None


def test_pass_person_name() -> None:
    raw = "reasoning stream"
    ans = "Morarji Desai"
    assert classify_output_format(raw, ans) is None


def test_pass_single_word() -> None:
    raw = "reasoning stream"
    ans = "backtick"
    assert classify_output_format(raw, ans) is None


# ─── retry reminder contract ────────────────────────


def test_format_retry_reminder_is_question_agnostic() -> None:
    """Sanity check that the reminder doesn't inject any hint that
    could leak an answer to a specific GAIA question. It should be
    purely about output format."""
    rem = FORMAT_RETRY_REMINDER.lower()
    # Must not contain any GAIA-specific answer-like tokens
    for forbidden in (
        "egalitarian",
        "34689",
        "142",
        "backtick",
        "morarji",
        "amphiprion",
        "biopython",
    ):
        assert forbidden not in rem, (
            f"reminder must be question-agnostic but contains {forbidden!r}"
        )
    # Must mention the sentinel so the model knows what to output
    assert "final answer" in rem


def test_format_retry_reminder_starts_with_newline_pair() -> None:
    """Callers concatenate this to the original user message; the
    leading blank-line separator makes the reminder visually break
    from the question text."""
    assert FORMAT_RETRY_REMINDER.startswith("\n\n")


# ─── mode priority ─────────────────────────────────


def test_priority_empty_over_others() -> None:
    """Empty raw wins over any extracted-answer content because the
    extractor returns '' when raw is empty — but defensively if
    caller passes a non-empty ans with empty raw, empty still wins."""
    assert (
        classify_output_format("", "Wait, something")
        is FormatFailureMode.EMPTY
    )


def test_priority_special_token_over_retry_talk() -> None:
    """If ans contains both a retry-talk lead-in AND a special token,
    special_token wins because quantization leak is a more specific
    failure signal than a think-aloud opener."""
    raw = "stream"
    ans = "Wait, let me check <|tool_call|>"
    assert (
        classify_output_format(raw, ans) is FormatFailureMode.SPECIAL_TOKEN
    )
