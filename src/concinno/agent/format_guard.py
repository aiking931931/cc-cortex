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
    """Distinct, question-independent format-failure categories."""

    EMPTY = "empty"
    RETRY_TALK = "retry_talk"
    QUOTE_DUMP = "quote_dump"
    SPECIAL_TOKEN = "special_token"


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


def classify_output_format(
    raw: str,
    extracted_answer: str,
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


__all__ = [
    "FORMAT_RETRY_REMINDER",
    "FormatFailureMode",
    "classify_output_format",
]
