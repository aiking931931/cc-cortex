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
    PARAPHRASE_RETRY_REMINDER,
    THOUGHT_LOOP_MIN_RAW_LEN,
    THOUGHT_LOOP_RETRY_REMINDER,
    FormatFailureMode,
    classify_output_format,
    retry_reminder_for_mode,
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


# ─── thought_loop mode (long raw + retry_talk lead-in) ───────────


def test_thought_loop_promoted_when_raw_long() -> None:
    # GAIA 17b5a6a3 signature: 25k chars of narration, zero tool
    # calls, extracted answer starts with "Wait,".
    raw = "x" * (THOUGHT_LOOP_MIN_RAW_LEN + 1)
    ans = "Wait, is there any other Amphiprion species that could fit"
    assert classify_output_format(raw, ans) is FormatFailureMode.THOUGHT_LOOP


def test_thought_loop_boundary_exact_threshold() -> None:
    raw = "x" * THOUGHT_LOOP_MIN_RAW_LEN
    ans = "Let's try searching for SPFMV SPCSV uniprot entries"
    assert classify_output_format(raw, ans) is FormatFailureMode.THOUGHT_LOOP


def test_thought_loop_gaia_2a649bb1_signature() -> None:
    # GAIA 2a649bb1 — SPFMV/SPCSV virus enzyme lookup, thought loop
    raw = "stream " * 5000  # ~30k chars — way over threshold
    ans = 'Let\'s try searching for "SPFMV" "SPCSV" "20'
    assert classify_output_format(raw, ans) is FormatFailureMode.THOUGHT_LOOP


def test_thought_loop_gaia_676e5e31_signature() -> None:
    # GAIA 676e5e31 — thought loop starting with "And I'll search"
    raw = "x" * 11000
    ans = (
        "And I'll search for \"Frozen/Chilled section\" that contain "
        "whole name of item"
    )
    assert classify_output_format(raw, ans) is FormatFailureMode.THOUGHT_LOOP


def test_retry_talk_stays_when_raw_short() -> None:
    # Backward compat: short raw + lead-in stays RETRY_TALK.
    raw = "stream content"  # ~14 chars
    ans = "Wait, is there any other Amphiprion species"
    assert classify_output_format(raw, ans) is FormatFailureMode.RETRY_TALK


def test_retry_talk_stays_just_below_threshold() -> None:
    raw = "x" * (THOUGHT_LOOP_MIN_RAW_LEN - 1)
    ans = "Let me check the primary source one more time"
    assert classify_output_format(raw, ans) is FormatFailureMode.RETRY_TALK


def test_thought_loop_reminder_pushes_tool_first() -> None:
    rem = retry_reminder_for_mode(FormatFailureMode.THOUGHT_LOOP)
    # The reminder must actively push tool-first behaviour, NOT
    # the FORMAT_RETRY_REMINDER "do not call more tools" message.
    assert "tool call" in rem.lower()
    assert "first action" in rem.lower() or "first" in rem.lower()
    assert "web_search" in rem
    assert "fetch_url" in rem


def test_thought_loop_reminder_distinct_from_retry_talk() -> None:
    # The two modes must route to different reminders — wiring the
    # wrong one would silently regress #15/#17b5a6a3-style cases.
    rt = retry_reminder_for_mode(FormatFailureMode.RETRY_TALK)
    tl = retry_reminder_for_mode(FormatFailureMode.THOUGHT_LOOP)
    assert rt != tl


def test_thought_loop_reminder_no_answer_leak() -> None:
    # No-cheat: the reminder must not contain any GAIA expected-
    # answer keywords that could bias the retry toward specific
    # ground-truth values.
    rem = THOUGHT_LOOP_RETRY_REMINDER.lower()
    for leak in (
        "morarji desai",  # 87c610df
        "egalitarian",    # c61d22de
        "amphiprion",     # 17b5a6a3
        "so we had to let it die",  # 624cbf11
    ):
        assert leak not in rem


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


# ─── paraphrase_risk mode (requires question argument) ───


def test_paraphrase_risk_headstone_rhyme() -> None:
    """Baseline GAIA #15: 'last line of the rhyme under … headstone'
    with a summary answer — classifier catches it so the retry layer
    gets a chance to re-ask for a verbatim quote."""
    q = (
        "What's the last line of the rhyme under the flavor name on "
        "the headstone visible in the background of the photo of the "
        "oldest flavor's headstone in the Ben & Jerry's online "
        "flavor graveyard as of the end of 2022?"
    )
    ans = "- physical graveyard is located in factory"
    assert (
        classify_output_format("stream", ans, question=q)
        is FormatFailureMode.PARAPHRASE_RISK
    )


def test_paraphrase_risk_verbatim_keyword() -> None:
    """The `verbatim` keyword alone is enough to classify."""
    q = "Give the verbatim text of the final stanza of the poem."
    ans = "three words here roughly paraphrased"
    assert (
        classify_output_format("stream", ans, question=q)
        is FormatFailureMode.PARAPHRASE_RISK
    )


def test_paraphrase_risk_does_not_trigger_on_single_word() -> None:
    """Single-word answers to verbatim questions can't be a
    paraphrase — a single concrete token is either right or wrong,
    no 'close enough' class. Avoid wasting a retry round on them."""
    q = "What is the last word of the lyric?"
    ans = "goodbye"
    assert classify_output_format("stream", ans, question=q) is None


def test_paraphrase_risk_does_not_trigger_without_question() -> None:
    """If callers don't pass ``question`` we can't evaluate
    paraphrase risk — classifier returns None instead of
    false-positive on all long answers."""
    ans = "Three or more words here but no question to check"
    assert classify_output_format("stream", ans) is None


def test_paraphrase_risk_does_not_trigger_on_non_quote_question() -> None:
    """A factual-count question must not inherit paraphrase risk."""
    q = "How many studio albums did the artist release between 2000 and 2010?"
    ans = "The artist released three studio albums during that period"
    assert classify_output_format("stream", ans, question=q) is None


# ─── retry_reminder_for_mode dispatch ────────────────


def test_retry_reminder_empty_is_format() -> None:
    assert (
        retry_reminder_for_mode(FormatFailureMode.EMPTY)
        is FORMAT_RETRY_REMINDER
    )


def test_retry_reminder_retry_talk_is_format() -> None:
    assert (
        retry_reminder_for_mode(FormatFailureMode.RETRY_TALK)
        is FORMAT_RETRY_REMINDER
    )


def test_retry_reminder_quote_dump_is_format() -> None:
    assert (
        retry_reminder_for_mode(FormatFailureMode.QUOTE_DUMP)
        is FORMAT_RETRY_REMINDER
    )


def test_retry_reminder_special_token_is_format() -> None:
    assert (
        retry_reminder_for_mode(FormatFailureMode.SPECIAL_TOKEN)
        is FORMAT_RETRY_REMINDER
    )


def test_retry_reminder_paraphrase_risk_is_paraphrase() -> None:
    """Paraphrase risk uses the verbatim-focused reminder, not the
    generic format reminder."""
    assert (
        retry_reminder_for_mode(FormatFailureMode.PARAPHRASE_RISK)
        is PARAPHRASE_RETRY_REMINDER
    )


def test_paraphrase_reminder_mentions_verbatim() -> None:
    assert "verbatim" in PARAPHRASE_RETRY_REMINDER.lower()
    assert "exact" in PARAPHRASE_RETRY_REMINDER.lower()


def test_paraphrase_reminder_is_question_agnostic() -> None:
    """Same no-cheat contract as FORMAT_RETRY_REMINDER — the verbatim
    reminder must not contain answer-like hints from any GAIA
    question."""
    rem = PARAPHRASE_RETRY_REMINDER.lower()
    for forbidden in (
        "egalitarian",
        "34689",
        "142",
        "morarji",
        "so we had to let it die",
    ):
        assert forbidden not in rem, (
            f"paraphrase reminder leaks answer-like token {forbidden!r}"
        )
