"""Agent behavior prompt building blocks (benchmark-agnostic).

@module concinno.agent.prompts
@responsibility Centralise reusable agent-guidance prompt fragments
    so benchmark runners compose them rather than copy-paste them.
@dependencies stdlib only
@exports AGENT_GUIDANCE_UNCERTAINTY, AGENT_GUIDANCE_ARITHMETIC,
    AGENT_GUIDANCE_COMPUTE_TOOLS, AGENT_GUIDANCE_NO_REFUSAL,
    default_guidance
"""

from __future__ import annotations

AGENT_GUIDANCE_UNCERTAINTY = (
    "If the question mentions any technical specification, domain "
    "term, API, or fact you are not 100% certain about, you MUST "
    "call web_search or fetch_url before answering. Do not guess."
)

AGENT_GUIDANCE_ARITHMETIC = (
    "For any multi-step arithmetic or unit conversion, use the "
    "run_bash tool with `python3 -c \"print(<expression>)\"` to "
    "compute the result. Do not rely on mental math. Redo the "
    "calculation once to verify."
)

AGENT_GUIDANCE_COMPUTE_TOOLS = (
    "For calendar arithmetic call date_calc with exact strptime "
    "formats — do not compute days by hand:\n"
    "  date_calc(op=\"delta\", date_from=\"1969-07-20\", "
    "date_to=\"2024-07-20\") -> \"20089 days (calendar: 55 years, "
    "0 months, 0 days)\"\n"
    "  date_calc(op=\"parse\", date_str=\"July 20, 1969\", "
    "format_str=\"%B %d, %Y\") -> \"1969-07-20\"\n"
    "For any non-trivial arithmetic, sum, average, unit conversion, "
    "or list reduction call python_exec with a single expression "
    "rather than run_bash:\n"
    "  python_exec(code=\"sum([12.5, 9.8, 14.2, 7.1])\") -> "
    "\"43.6\"\n"
    "  python_exec(code=\"round(1609.34 * 26.2, 2)\") -> "
    "\"42164.71\"\n"
    "python_exec accepts pure expressions only (no import, no "
    "assignment, no attribute access). Usable builtins include "
    "abs, round, pow, divmod, min, max, sum, len, sorted, zip, "
    "bool, int, float, str, list, tuple, dict, set."
)

AGENT_GUIDANCE_NO_REFUSAL = (
    "Even if you cannot find perfect information, commit to your "
    "best single concrete answer. Never output placeholder text "
    "such as 'I cannot', 'I am unable', 'I need more information', "
    "'Once I have access', or partial sentences — these are all "
    "scored as wrong. A best-guess value beats a refusal."
)

AGENT_GUIDANCE_SEARCH_DISCIPLINE = (
    "For any factual claim (dates, numbers, names, titles), call "
    "web_search with AT LEAST 2 different query phrasings and "
    "cross-reference the results before committing. If sources "
    "disagree, prefer the most-cited or most-authoritative answer "
    "over the first. If the initial search returns empty, "
    "reformulate with different keywords and retry — do not give up "
    "after a single query. Cap at 5 web_search calls per question "
    "(diminishing returns beyond that)."
)

AGENT_GUIDANCE_EXACT_QUOTE = (
    "For questions asking for a quote, lyric, line, title, or any "
    "verbatim excerpt: call fetch_url on the source page when "
    "available and reproduce the EXACT text, including original "
    "punctuation, capitalization, and spacing. Never paraphrase, "
    "summarize, or rephrase — the grader compares character-for-"
    "character."
)


def default_guidance() -> str:
    """Return the default joined agent-guidance prompt."""
    return "\n".join((
        AGENT_GUIDANCE_UNCERTAINTY,
        AGENT_GUIDANCE_ARITHMETIC,
        AGENT_GUIDANCE_NO_REFUSAL,
    ))
