"""Agent output-format failure classifier + retry-reminder prompt.

@module concinno.agent.format_guard
@responsibility Detect four common agent output-format failure modes
    (empty, retry-talk, quote-dump, special-token leak) that a single
    deterministic re-ask with a tighter format rule can rescue.
    Library module — pure functions + constants only.
@dependencies stdlib only (``re``)
@exports FormatFailureMode, classify_output_format,
    FORMAT_RETRY_REMINDER

Why this exists
---------------
GAIA-style benchmarks (and any tool-using agent loop) sometimes
ship the agent's internal monologue or a half-formed quantization
token as the final answer — the model gathered correct evidence
but missed the final-answer sentinel. Gemma4-Q4_K_M regressed four
classes in the 2026-04-22j baseline_26b_seed42 run:

* ``empty``        — raw stream is blank after iteration completes.
* ``retry_talk``   — answer starts with think-aloud lead-in (``"Wait,"``,
  ``"Let's try searching"``, ``"And I'll check"``, ``"Based on"``).
* ``quote_dump``   — the search-query argument list leaked out as the
  final answer (``'average p-value" "Nature" "2020" "0.0'``).
* ``special_token`` — chat-template leak (``<|tool_call|>``,
  ``<channel|>``, ``<im_start|>``) survived into the extracted
  answer slot. Distinct from the normal Gemma4 ``<channel|>`` markers
  that appear in the raw reasoning stream for every question —
  classifier keys off the EXTRACTED answer, not raw.

Who should call this
--------------------
Any runtime driving a tool-using loop that extracts a final answer
from the last SSE ``token`` event and wants to give the model one
deterministic retry with a format reminder before accepting the
bad output. Designed to be CC-compatible (``ThinkingDepthGuard``
style pure classifier, no async, no IO) so a Concinno consumer
can import it without pulling Sancio / benchmark deps.

No-cheat contract
-----------------
Inputs to :func:`classify_output_format` are the raw SSE-concatenated
stream and the extractor-normalized answer — neither contains the
expected answer or ground-truth label. The :data:`FORMAT_RETRY_REMINDER`
string is question-agnostic and injects no hint that could leak the
correct answer. Safe for any-benchmark-or-production use.
"""

from __future__ import annotations

import re
from enum import Enum


class FormatFailureMode(str, Enum):
    """Distinct failure categories keyed off output-format signals.

    The first four are question-independent (raw + extracted answer
    only). ``PARAPHRASE_RISK`` is the one member that also inspects
    ``question`` text — still no expected-answer / ground-truth
    read, so the no-cheat contract holds, but the trigger is
    question-structural: the grader for this class (exact-quote,
    lyric, epitaph, headstone inscription, rhyme) rejects anything
    that isn't character-for-character.
    """

    EMPTY = "empty"
    RETRY_TALK = "retry_talk"
    QUOTE_DUMP = "quote_dump"
    SPECIAL_TOKEN = "special_token"
    PARAPHRASE_RISK = "paraphrase_risk"


# Lead-in / think-aloud openers. Also accepts a short
# ``and`` / ``so`` / ``then`` / ``but`` prefix because Gemma4-Q4_K_M
# sometimes emits the think-aloud as a continuation of a prior
# sentence that got clipped from the extract.
_RETRY_TALK_RE = re.compile(
    r"^\s*(?:and |so |then |but |ok[, ]|okay[, ])?"
    r"(?:"
    r"wait[, ]|let'?s |i['']?ll |i'?m going to |i need to |"
    r"searching? (?:for|the|through)?|based on (?:the |my |this )?|"
    r"first[, ]|now i |actually[, ]|hmm[, ]|"
    r"let me |thinking|my plan|to (?:solve|answer|find) |"
    r"looking (?:up|for|at)|checking |i should |"
    r"i (?:will|need to|have to) (?:search|check|look|find|try)|"
    r"i'?m (?:going|trying|looking) to"
    r")",
    re.IGNORECASE,
)

# Quote-dump: more than one short quoted token back-to-back is
# almost never a concrete answer — it's the agent echoing its own
# search-query argument list.
_QUOTE_DUMP_RE = re.compile(
    r'^[^"]{0,40}?"[^"]{1,40}"[^"]{0,6}?"[^"]{1,40}"'
)

# Chat-template leaks. Two families: full ``<|foo|>`` tokens and
# the half-tagged ``<channel|>`` / ``<start|>`` variants Gemma4 streams
# when the template breaks. Distinct from ``<channel|>`` markers in
# the raw reasoning stream — classifier only scans the extracted
# answer, so normal reasoning markers don't false-positive.
_SPECIAL_TOKEN_RE = re.compile(
    r"<\|[^|]{0,30}\|>|<(?:channel|start|end|im_start|im_end)\|>"
)

# Questions that require verbatim-quote output — the grader rejects
# any rephrasing, summary, or added lead-in like "The line reads".
# Observed offender #15: "What's the last line of the rhyme under
# the flavor name on the headstone …" → expected "So we had to let
# it die.", got "- physical graveyard is located in factory"
# (summary). Structure-only match: question text read, no expected
# answer leak.
_PARAPHRASE_QUESTION_RE = re.compile(
    r"\b(?:last|first|closing|opening|final|second)\s+"
    r"(?:line|sentence|verse|lyric|stanza|paragraph|word)s?\b"
    r"|\bverbatim\b"
    r"|\bexact\s+(?:quote|wording|text|phrase|sentence)\b"
    r"|\bquote\s+(?:the|from|exactly|verbatim)\b"
    r"|\bepitaph\b|\binscription\b|\bheadstone\b"
    r"|\b(?:rhyme|poem|lyric|chorus)\s+(?:under|above|on|of|for|at)\b",
    re.IGNORECASE,
)


def classify_output_format(
    raw: str,
    extracted_answer: str,
    *,
    question: str | None = None,
) -> FormatFailureMode | None:
    """Return a failure mode if the agent output looks malformed, else None.

    Parameters
    ----------
    raw
        The SSE-concatenated stream returned by the agent loop. Used
        only for the empty-check — special-token detection keys off
        ``extracted_answer`` because Gemma4 streams ``<channel|>``
        reasoning markers in the raw for every question.
    extracted_answer
        The value produced by the caller's ``FINAL ANSWER:`` extractor
        (or equivalent last-line fallback). This is what gets scored,
        so this is what needs to be well-formed.

    Returns
    -------
    FormatFailureMode | None
        One of the four enum members if a failure pattern matches,
        ``None`` if the output is structurally plausible. Callers
        should retry the request once with the same question plus
        :data:`FORMAT_RETRY_REMINDER` on a non-None result, then
        accept whichever attempt produces a clean output.
    """
    stripped = (raw or "").strip()
    if not stripped:
        return FormatFailureMode.EMPTY
    if extracted_answer and _SPECIAL_TOKEN_RE.search(extracted_answer):
        return FormatFailureMode.SPECIAL_TOKEN
    if extracted_answer and _RETRY_TALK_RE.match(extracted_answer):
        return FormatFailureMode.RETRY_TALK
    if extracted_answer and _QUOTE_DUMP_RE.match(extracted_answer):
        return FormatFailureMode.QUOTE_DUMP
    # Verbatim-quote class: question asks for an exact line /
    # inscription / epitaph / lyric AND answer is multi-word (a
    # single-word answer like "backtick" can't be a paraphrase).
    # The grader rejects anything that isn't character-for-character,
    # so a retry with a verbatim reminder is the cheap save.
    if (
        question
        and extracted_answer
        and len(extracted_answer.split()) >= 3
        and _PARAPHRASE_QUESTION_RE.search(question)
    ):
        return FormatFailureMode.PARAPHRASE_RISK
    return None


FORMAT_RETRY_REMINDER = (
    "\n\nFORMAT REMINDER (this is a retry — your previous reply was "
    "rejected by the grader's format check): reply with EXACTLY one "
    "line, starting with 'FINAL ANSWER: ' and followed by the single "
    "concrete value. Do not prefix with 'Wait', 'Let me', "
    "'Searching', 'Based on', 'I'll', or any other thinking lead-in. "
    "Do not output tool-call syntax (no '<|tool_call|>'). Do not "
    "call more tools in this turn. If the evidence gathered so far "
    "isn't perfect, commit your single best-guess value anyway — "
    "empty / 'unknown' / refusal all score wrong."
)
"""Single, question-agnostic reminder appended to the retry prompt.

Inject by concatenating to the ORIGINAL user message — never feed
the bad first reply back in as assistant context, that just locks
the model into doubling down on the bad format.
"""


PARAPHRASE_RETRY_REMINDER = (
    "\n\nFORMAT REMINDER (verbatim-quote check): this question asks "
    "for an EXACT line / rhyme / epitaph / inscription / verbatim "
    "excerpt. The grader compares character-for-character — matching "
    "the meaning is NOT enough. You must reproduce the text EXACTLY "
    "as it appears at the source (original capitalization, "
    "punctuation, spacing, and word order). Use fetch_url on the "
    "source page if you have the URL, find the exact span, and "
    "quote it. Do NOT paraphrase, summarize, add 'The line reads' / "
    "'The inscription is' / 'Effectiveness of' or any other prefix, "
    "and do not drop trailing punctuation. Reply with exactly one "
    "line: 'FINAL ANSWER: <exact text>'."
)
"""Verbatim-quote retry reminder for :attr:`FormatFailureMode.PARAPHRASE_RISK`.

Distinct from :data:`FORMAT_RETRY_REMINDER` because paraphrase-risk
questions need a verbatim-match instruction, not a thinking-lead-in
rejection. Still fully question-agnostic (no expected answer leak).
"""


_REMINDER_BY_MODE: dict[FormatFailureMode, str] = {
    FormatFailureMode.EMPTY: FORMAT_RETRY_REMINDER,
    FormatFailureMode.RETRY_TALK: FORMAT_RETRY_REMINDER,
    FormatFailureMode.QUOTE_DUMP: FORMAT_RETRY_REMINDER,
    FormatFailureMode.SPECIAL_TOKEN: FORMAT_RETRY_REMINDER,
    FormatFailureMode.PARAPHRASE_RISK: PARAPHRASE_RETRY_REMINDER,
}


def retry_reminder_for_mode(mode: FormatFailureMode) -> str:
    """Return the right retry-reminder string for a given failure mode.

    Consumers (Sancio's ``_run_and_stream``, benchmark runners) call
    this with the enum member from :func:`classify_output_format`
    and append the returned string to the original user message
    before re-running the agent loop. No-op fallback to
    :data:`FORMAT_RETRY_REMINDER` if an unknown mode is passed — the
    contract is "never raise from a retry path".
    """
    return _REMINDER_BY_MODE.get(mode, FORMAT_RETRY_REMINDER)


__all__ = [
    "FORMAT_RETRY_REMINDER",
    "FormatFailureMode",
    "PARAPHRASE_RETRY_REMINDER",
    "classify_output_format",
    "retry_reminder_for_mode",
]
