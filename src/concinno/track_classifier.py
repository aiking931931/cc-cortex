"""Track classifier — safety/cyber/coding/persona keyword routing.

Extracted from Aegis persona-api ``api.py`` as part of the
Aegis → CCC downsink (session 2026-04-15 M6). Aegis imports
``classify_track`` and drops its local ``_detect_track`` body.

The four compiled keyword patterns and the priority-order
logic are preserved byte-for-byte from the Aegis source.
Behavioral contract:

1. **Persona signal wins** (even 1 match) → returns ``"safety"``
   because the persona engine requires the safety track's
   guardrails.
2. **Coding-dominant** (≥2 coding hits AND strictly more than
   max(safety, cyber)) → ``"coding"``.
3. **Cyber-dominant** (≥2 cyber hits OR strictly max of all
   three keyword buckets) → ``"cyber"``.
4. **Default** → ``"safety"`` (fail-safe: always runs guards).

Note the asymmetry: ``PERSONA_KW`` routes to ``"safety"`` not
``"persona"``. That is intentional upstream behavior — the
persona engine is layered on top of the safety track, not a
sibling of it. ``TRACKS`` omits ``"persona"`` for the same
reason.

Pure stdlib. Zero runtime dependencies.
"""

from __future__ import annotations

import re

__all__ = [
    "CODING_KW",
    "CYBER_KW",
    "PERSONA_KW",
    "SAFETY_KW",
    "TRACKS",
    "classify_track",
]


TRACKS: tuple[str, ...] = ("safety", "cyber", "coding")


# ── Keyword patterns ───────────────────────────────────────────
#
# Prefix-safe (no trailing \b that blocks "vulnerability" etc.).
# Preserved byte-for-byte from Aegis ``api.py`` lines 111-144.

SAFETY_KW = re.compile(
    r"(?:\bsafety\b|\bguard\w*"
    r"|\bprompt[- ]?inject\w*|\bcontext[- ]?inject\w*"
    r"|\bexfiltrat\w*|\bdestruct\w*|\bpolicy\b"
    r"|\bviolation\w*|\bharmful\b|\btoxic\w*"
    r"|\bjailbreak\w*|\bunsafe\b|\bbenign\b)",
    re.IGNORECASE,
)

CYBER_KW = re.compile(
    r"(?:\bscan\w*|\bvulnerabil\w*|\bcve[- ]?\d*"
    r"|\bexploit\w*|\bthreat\w*|\bmalware\b"
    r"|\bsecurity\b|\bcyber\w*|\bxss\b"
    r"|\bsql[- ]?inject\w*|\bowasp\b|\breverse[- ]?engineer\w*"
    r"|\bpenetration\b|\bpentest\w*|\bfirewall\b)",
    re.IGNORECASE,
)

CODING_KW = re.compile(
    r"(?:\bcode\b|\bcoding\b|\bfunction\b|\bdebug\w*"
    r"|\bimplement\w*|\balgorithm\w*|\bprogram\w*"
    r"|\bcompile\w*|\bsyntax\b|\brefactor\w*"
    r"|\bbugfix\b|\bunit[- ]?test\w*|\bpython\b|\bjavascript\b"
    r"|\bkubernetes\b|\bk8s\b|\bdocker\w*|\bdevops\b"
    r"|\byaml\b|\bmanifest\b|\bhelm\b|\bterraform\b)",
    re.IGNORECASE,
)

PERSONA_KW = re.compile(
    r"(?:\btalk\s+as\b|\bact\s+as\s+a\b|\bplay\s+(?:the\s+)?role"
    r"|\bpretend\s+(?:to\s+be|you\s+are)"
    r"|\bpersona\b|\bcharacter\b|\bin[- ]?character\b"
    r"|\bocean\b|\bbig[- ]?five\b|\byour\s+personality"
    r"|\byou\s+are\s+(?:a|an)\s+(?:\d+[- ]?year[- ]?old"
    r"|cheerful|serious|shy|outgoing|introvert|extrovert))",
    re.IGNORECASE,
)


def classify_track(text: str) -> str:
    """Classify ``text`` into one of the four Aegis tracks.

    Returns one of ``"safety"``, ``"cyber"``, ``"coding"``.
    Persona hits collapse to ``"safety"`` because the persona
    engine runs on top of the safety track — this mirrors the
    upstream Aegis behavior bit-for-bit.

    Priority order (highest first):

    1. Persona keyword → ``"safety"`` (persona engine path)
    2. Coding-dominant (≥2 hits AND strictly > safety, cyber)
       → ``"coding"``
    3. Cyber-dominant (≥2 hits OR strictly > safety, coding)
       → ``"cyber"``
    4. Default → ``"safety"``
    """
    safety = len(SAFETY_KW.findall(text))
    cyber = len(CYBER_KW.findall(text))
    coding = len(CODING_KW.findall(text))
    persona = len(PERSONA_KW.findall(text))

    # 1. Persona request wins — routes to safety + persona engine
    if persona >= 1:
        return "safety"

    # 2. Coding wins only if 2+ coding signals AND not security-heavy
    if coding >= 2 and coding > max(safety, cyber):  # noqa: PLR2004
        return "coding"

    # 3. Cyber wins if 2+ cyber signals OR strictly max
    if cyber >= 2 or (cyber > safety and cyber > coding):  # noqa: PLR2004
        return "cyber"

    # 4. Safety wins on any match or as default
    return "safety"
