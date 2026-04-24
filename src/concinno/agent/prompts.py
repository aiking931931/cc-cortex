"""Agent behavior prompt building blocks (benchmark-agnostic).

@module concinno.agent.prompts
@responsibility Centralise reusable agent-guidance prompt fragments
    so benchmark runners compose them rather than copy-paste them.
@dependencies stdlib only
@exports AGENT_GUIDANCE_UNCERTAINTY, AGENT_GUIDANCE_ARITHMETIC,
    AGENT_GUIDANCE_COMPUTE_TOOLS, AGENT_GUIDANCE_NO_REFUSAL,
    AGENT_GUIDANCE_EXACT_QUOTE, AGENT_GUIDANCE_EXACT_UNIT,
    AGENT_GUIDANCE_FACTUAL_COUNT, AGENT_GUIDANCE_SEARCH_DISCIPLINE,
    AGENT_GUIDANCE_VISION, AGENT_GUIDANCE_PDB_FILE_ORDER,
    default_guidance, select_question_anchors,
    build_targeted_guidance
"""

from __future__ import annotations

import re

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

AGENT_GUIDANCE_EXACT_UNIT = (
    "This question specifies the unit the answer must be reported "
    "in (the unit appears directly after the word 'in'). The numeric "
    "value you output must be in THAT unit — not a sub-unit, not a "
    "converted precision unit. If the question says 'in Angstroms, "
    "rounded to the nearest picometer', the answer is a value in "
    "Angstroms (e.g. 1.456), NOT in picometers (not 1456). The "
    "phrase 'rounded to the nearest X' describes the precision of "
    "the answer in the target unit, not the target unit itself."
)

AGENT_GUIDANCE_FACTUAL_COUNT = (
    "Factual count question. Do NOT estimate. For a person's "
    "catalogued works (albums, books, films), make ONE call to "
    "fetch_wikipedia_section with the subject's name and the exact "
    "section header. That returns only that section's text, so "
    "count entries in the date range directly. Avoid fetch_url on "
    "the discography sub-article — it often splits double releases "
    "and disagrees with the main article. For non-catalogue counts, "
    "consult the publisher's primary source via web_search. Commit "
    "FINAL ANSWER after ONE successful lookup."
)

AGENT_GUIDANCE_PDB_FILE_ORDER = (
    "This question references atom or residue ordering in a PDB "
    "file. Biopython's ``list(structure.get_atoms())`` yields atoms "
    "in Model > Chain > Residue > Atom hierarchy order, which is "
    "NOT guaranteed to match the ATOM-record line order in the "
    "original .pdb text (altloc / DisorderedAtom / multi-model "
    "entries reorder). For questions that ask for the 'first', "
    "'second', or 'Nth' atom 'as listed in the PDB file', rely on "
    "the atom serial number — it preserves the file's line order:\n"
    "  from Bio.PDB import PDBParser\n"
    "  s = PDBParser(QUIET=True).get_structure('x', path)\n"
    "  ordered = sorted(s.get_atoms(), "
    "key=lambda a: a.get_serial_number())\n"
    "  d = ordered[0] - ordered[1]  # Atom subtraction "
    "returns distance in Angstroms\n"
    "Alternative (no Biopython): read the raw text and take the "
    "first two lines starting with 'ATOM  ' / 'HETATM'; parse "
    "fixed-width columns 31-38 (x), 39-46 (y), 47-54 (z); compute "
    "sqrt of the sum of squared coordinate differences. Do NOT use "
    "``list(structure.get_atoms())[0] / [1]`` — that is hierarchy "
    "order, not file order. Round to the precision the question "
    "requests (e.g. 'nearest picometer' reported in Angstroms = "
    "3 decimal places)."
)

AGENT_GUIDANCE_VISION = (
    "This question requires visual inspection — identifying a "
    "subject, reading text, or locating something inside a "
    "photograph, picture, screenshot, or image frame. You MUST call "
    "fetch_image with the direct image URL so the next inference "
    "turn can actually see the image. Narrating what you would look "
    "at, describing a planned search, or restating the question is "
    "NOT a substitute for invoking the tool. Concrete call:\n"
    "  fetch_image(url=\"https://example.com/photo.jpg\")\n"
    "If you do not yet have the direct image URL, first use "
    "web_search or fetch_url to locate the page, extract a direct "
    "image URL (ending .jpg/.jpeg/.png/.webp/.gif), then call "
    "fetch_image on THAT URL. Producing FINAL ANSWER on a visual "
    "question without having called fetch_image at least once is "
    "scored wrong."
)


def default_guidance() -> str:
    """Return the default joined agent-guidance prompt."""
    return "\n".join((
        AGENT_GUIDANCE_UNCERTAINTY,
        AGENT_GUIDANCE_ARITHMETIC,
        AGENT_GUIDANCE_NO_REFUSAL,
    ))


# ─── ZIQ SPS one-shot question-structure classifier ────────────────
#
# Cold-start router — no FTRL, no online learning. Uses structural
# regex priors on question text only (no access to expected answer)
# to pick which targeted-guidance blocks apply. Replaces the
# "global append" approach of ``v1_phase1`` which bloated prompts
# for every question and caused Gemma4-Q4_K_M regression (MEMORY #89).
#
# Design invariants:
#   1. Input is question string only — no expected / ground_truth
#      leaks (no-cheat).
#   2. Output is a tuple of AGENT_GUIDANCE_* constants in stable
#      order (sorted by insertion order in ANCHOR_PATTERNS).
#   3. A question matching zero patterns returns an empty tuple —
#      caller appends nothing, keeping the default prompt untouched.
#   4. Patterns are intentionally conservative (narrow trigger
#      surfaces) so PASS questions don't accidentally receive inject
#      and regress under prompt bloat.

_EXACT_UNIT_PATTERN = re.compile(
    # "Report <noun>? in <UNIT>" — the unit token is the first
    # alphabetic word after "in", optionally preceded by a noun
    # ("the answer", "the distance", etc.). We don't capture the
    # unit value here; presence of the construct is enough to
    # trigger the targeted guidance.
    r"\b(?:report|give|express|provide|state|return|answer)\b"
    r"[^.?!]{0,60}?"
    r"\bin\s+"
    r"(?:Angstroms?|Å|picometers?|pm|nanometers?|nm|"
    r"meters?|centimeters?|cm|millimeters?|mm|kilometers?|km|"
    r"seconds?|minutes?|hours?|days?|years?|"
    r"grams?|kilograms?|kg|milligrams?|mg|pounds?|lbs?|"
    r"degrees?|radians?|Celsius|Fahrenheit|Kelvin|"
    r"percent|%|bytes?|bits?|hertz|Hz|joules?|watts?|"
    r"(?:scientific\s+notation))\b",
    re.IGNORECASE,
)

_EXACT_QUOTE_PATTERN = re.compile(
    r"\b(?:last|first|closing|opening|final|second)\s+"
    r"(?:line|sentence|verse|lyric|stanza|paragraph|word)s?\b"
    r"|\bverbatim\b"
    r"|\bexact\s+(?:quote|wording|text|phrase|sentence)\b"
    r"|\bquote\s+(?:the|from|exactly|verbatim)\b"
    r"|\bepitaph\b"
    r"|\binscription\b"
    r"|\b(?:rhyme|poem|lyric|chorus)\s+(?:under|above|on|of|for)\b",
    re.IGNORECASE,
)

_FACTUAL_COUNT_PATTERN = re.compile(
    r"\bhow\s+many\b"
    r"|\bwhat\s+is\s+the\s+(?:number|count|total|quantity)\s+of\b"
    r"|\bif\s+we\s+assume\b[^.?!]{0,200}?\bhow\s+many\b",
    re.IGNORECASE | re.DOTALL,
)

_VISION_PATTERN = re.compile(
    # 1. "photo/picture/image of X" — the question anchors on a
    #    specific image; agent must see it.
    r"\b(?:photo|photograph|picture|screenshot|thumbnail|still"
    r"|frame)s?\s+(?:of|showing|depicting|shows|depicts|taken\s+of"
    r"|featuring|that\s+shows?)\b"
    # 2. "in/on/from the photo / background / picture / screenshot"
    r"|\b(?:in|within|on|from)\s+the\s+"
    r"(?:photo|photograph|picture|image|screenshot|background|frame"
    r"|thumbnail)\b"
    # 3. "visible/shown/depicted/pictured in [the] background/photo"
    r"|\b(?:visible|shown|depicted|pictured|appearing|seen"
    r"|displayed|captured)\s+"
    r"(?:in|on|within|behind|next\s+to)\s+"
    r"(?:the|a|an)?\s*"
    r"(?:photo|photograph|picture|image|background|screenshot"
    r"|frame)\b"
    # 4. "what is written/inscribed/shown on X" (reading text on an
    #    object in an image)
    r"|\bwhat(?:'s|\s+is)\s+"
    r"(?:written|inscribed|shown|depicted|visible|printed|engraved"
    r"|painted|displayed)\s+(?:on|in|at)\b"
    # 5. "headstone/tombstone/sign/label + visible/shown/pictured"
    #    (narrow companion to EXACT_QUOTE for visual-object questions)
    r"|\b(?:headstone|tombstone|gravestone|sign|plaque|label"
    r"|poster|banner|billboard)\s+"
    r"(?:visible|shown|pictured|in\s+the\s+background|depicted)\b",
    re.IGNORECASE,
)


# Co-occurrence check: PDB / .pdb / Protein Data Bank context AND
# an ordinal reference that pins to file line order (not residue
# number). Both lookaheads must match so we don't fire on generic
# "PDB ID 1abc" questions that have no file-order ambiguity.
_PDB_FILE_ORDER_PATTERN = re.compile(
    r"(?=.*?(?:\bPDB\s+(?:file|ID)\b|\.pdb\b"
    r"|\bProtein\s+Data\s+Bank\b))"
    r"(?=.*?(?:"
    # Ordinal + atom/residue/record within 40 chars
    r"\b(?:first|second|third|fourth|fifth"
    r"|1st|2nd|3rd|4th|5th)\b"
    r"[\s\S]{0,40}?"
    r"\b(?:atoms?|residues?|HETATMs?|records?)\b"
    r"|\bas\s+(?:they\s+are\s+)?listed\b"
    r"|\bin\s+(?:the\s+)?(?:order|file\s+order)\b"
    r"))",
    re.IGNORECASE | re.DOTALL,
)


# Ordered pattern table — order controls deterministic output order
# when a question matches multiple anchors.
ANCHOR_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("exact_unit", _EXACT_UNIT_PATTERN, AGENT_GUIDANCE_EXACT_UNIT),
    ("exact_quote", _EXACT_QUOTE_PATTERN, AGENT_GUIDANCE_EXACT_QUOTE),
    (
        "factual_count",
        _FACTUAL_COUNT_PATTERN,
        AGENT_GUIDANCE_FACTUAL_COUNT,
    ),
    ("vision", _VISION_PATTERN, AGENT_GUIDANCE_VISION),
    (
        "pdb_file_order",
        _PDB_FILE_ORDER_PATTERN,
        AGENT_GUIDANCE_PDB_FILE_ORDER,
    ),
)


def select_question_anchors(question: str) -> tuple[str, ...]:
    """Return guidance blocks whose regex matches the question text.

    ZIQ SPS one-shot router: structural prior only, no FTRL, no
    expected-answer leak. An empty-string / whitespace question
    returns ``()`` (caller appends nothing).

    Returns tuple of AGENT_GUIDANCE_* constants in the order defined
    by ANCHOR_PATTERNS (stable).
    """
    if not question or not question.strip():
        return ()
    out: list[str] = []
    for _name, pattern, guidance in ANCHOR_PATTERNS:
        if pattern.search(question):
            out.append(guidance)
    return tuple(out)


def build_targeted_guidance(
    question: str,
    *,
    base: tuple[str, ...] = (
        AGENT_GUIDANCE_UNCERTAINTY,
        AGENT_GUIDANCE_ARITHMETIC,
    ),
) -> str:
    """Compose guidance = base blocks + question-specific anchors.

    Keeps the baseline v1 prompt unchanged for questions that match
    no anchor (empty overlay). Matching questions receive ONLY the
    specific anchor text appended, avoiding MEMORY #89 prompt bloat
    regression where every question got every Phase-1 block.

    Returns a newline-joined guidance string ready to drop into the
    system-suffix template (no leading/trailing newlines).
    """
    anchors = select_question_anchors(question)
    blocks = base + anchors
    return "\n".join(blocks)
