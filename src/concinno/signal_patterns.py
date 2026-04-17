"""Persona signal regex patterns — public CCC building block.

Extracted from Aegis persona-api router.py as part of the
Aegis → CCC downsink (session 2026-04-15 M4). Aegis imports
these via ``from concinno.signal_patterns import ...`` and
keeps only its thin RouteDecision logic.

Three public compiled patterns plus matcher helpers:

* ``GREETING_PATTERN``       — fast-path greetings (EN + CJK)
* ``IDENTITY_PROBE_PATTERN`` — deep-depth persona probing
* ``CHARACTER_CHALLENGE_PATTERN`` — break-character attempts

Byte-level note: the CJK escapes (``\\u4f60\\u597d`` etc.) are
preserved verbatim from the Aegis source. Do NOT edit the raw
strings without cross-checking round-trip match behaviour on
both CPython 3.11 and 3.12 — Unicode property classes differ
across Python minor versions and we rely on the literal escape
form for stability.

Pure stdlib. Zero CCC runtime dependencies.
"""

from __future__ import annotations

import re

__all__ = [
    "CHARACTER_CHALLENGE_PATTERN",
    "GREETING_PATTERN",
    "IDENTITY_PROBE_PATTERN",
    "detect_signals",
    "is_character_challenge",
    "is_greeting",
    "is_identity_probe",
]


# ── Compiled patterns ──────────────────────────────────────────

GREETING_PATTERN = re.compile(
    r"(?:^hi\b|^hello\b|^hey\b|^\u4f60\u597d"
    r"|^\u563f|^\u55e8|^good\s(?:morning|evening|night)"
    r"|^how\sare\syou|^what'?s\sup"
    r"|^thanks|^\u8b1d\u8b1d|^\u611f\u8b1d)",
    re.IGNORECASE,
)

IDENTITY_PROBE_PATTERN = re.compile(
    r"(?:why\sdo\syou|what\swould\syou\sdo\sif"
    r"|tell\sme\sabout\syour"
    r"|how\sdo\syou\sfeel\sabout"
    r"|describe\syour\spersonality"
    r"|your\spersonality|your\sbackground"
    r"|stay\sin\scharacter"
    r"|persona\sconsistency"
    r"|\u70ba\u4ec0\u9ebc\u4f60|\u4f60\u7684\u904e\u53bb"
    r"|\u4f60\u600e\u9ebc\u770b|\u4fdd\u6301\u89d2\u8272)",
    re.IGNORECASE,
)

CHARACTER_CHALLENGE_PATTERN = re.compile(
    r"(?:break\scharacter|stop\spretending"
    r"|you'?re\sjust\san?\sai"
    r"|drop\sthe\sact|\u4f60\u662f\u4eba\u5de5\u667a\u80fd"
    r"|\u5225\u88dd\u4e86|\u4f60\u4e0d\u662f\u771f\u7684)",
    re.IGNORECASE,
)


# ── Matcher helpers ────────────────────────────────────────────


def is_greeting(text: str) -> bool:
    """Return True if ``text`` starts with a known greeting.

    Matches English (``hi``, ``hello``, ``good morning``) and
    common CJK openers (``你好``, ``嗨``, ``嘿``, ``謝謝``).
    """
    return bool(GREETING_PATTERN.search(text))


def is_identity_probe(text: str) -> bool:
    """Return True if ``text`` probes persona identity.

    These questions justify routing to DEEP processing depth:
    a lightweight FAST lane would fail to maintain character.
    """
    return bool(IDENTITY_PROBE_PATTERN.search(text))


def is_character_challenge(text: str) -> bool:
    """Return True if ``text`` attempts to break character.

    Examples: ``break character``, ``you're just an AI``,
    ``你是人工智能``, ``別裝了``. Any hit should force the
    deepest pipeline to maintain persona consistency.
    """
    return bool(CHARACTER_CHALLENGE_PATTERN.search(text))


def detect_signals(text: str) -> dict[str, bool]:
    """Return a signals dict for routing decisions.

    Shape: ``{"fast_match": bool, "identity_probe": bool,
    "challenge": bool}``. Key names match the Aegis
    ``RouteDecision.signals`` schema exactly so callers can
    merge this into their own signals map without renaming.
    """
    return {
        "fast_match": is_greeting(text),
        "identity_probe": is_identity_probe(text),
        "challenge": is_character_challenge(text),
    }
