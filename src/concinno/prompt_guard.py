"""concinno.prompt_guard — Clarity gate + multi-question detection for UserPromptSubmit.

@module prompt_guard
@responsibility Rule-based prompt analysis: clarity gate (deny ambiguous + irreversible),
    multi-question checklist injection, and preference calibration (TADS-3).
@dependencies none (stdlib only)
@exports run_clarity_gate, multi_question_injection, clarity_score, count_questions

Provides rule-based prompt analysis before execution:
1. Clarity gate: deny ambiguous + irreversible prompts (TADS-3)
2. Multi-question detection: inject checklist discipline
3. Preference calibration: adjust thresholds from history

Usage:
    from concinno.prompt_guard import run_clarity_gate, multi_question_injection

    # In UserPromptSubmit hook:
    deny = run_clarity_gate(prompt)
    if deny:
        return deny  # block
    injection = multi_question_injection(prompt)
    if injection:
        return {"additionalContext": injection}
"""

from __future__ import annotations

import json
import os
import re
from typing import Optional

from concinno.i18n import msg as i18n_msg
from concinno.i18n import patterns as i18n_patterns

# ── Constants (lazy-loaded from i18n) ────────────────────

_irreversible: tuple[str, ...] | None = None
_vague: tuple[str, ...] | None = None
_known_commands: tuple[str, ...] | None = None
_multi_topic: tuple[str, ...] | None = None


def _get_irreversible() -> tuple[str, ...]:
    global _irreversible
    if _irreversible is None:
        _irreversible = tuple(i18n_patterns("prompt_guard.irreversible"))
    return _irreversible


def _get_vague() -> tuple[str, ...]:
    global _vague
    if _vague is None:
        _vague = tuple(i18n_patterns("prompt_guard.vague"))
    return _vague


def _get_known_commands() -> tuple[str, ...]:
    global _known_commands
    if _known_commands is None:
        _known_commands = tuple(i18n_patterns("prompt_guard.known_commands"))
    return _known_commands


def _get_multi_topic() -> tuple[str, ...]:
    global _multi_topic
    if _multi_topic is None:
        _multi_topic = tuple(i18n_patterns("prompt_guard.multi_topic"))
    return _multi_topic


MULTI_Q_MARKERS: tuple[str, ...] = ("？", "?")


# ── Multi-Question Detection ────────────────────────────


def count_questions(prompt: str) -> int:
    """Count distinct questions/topics in a prompt.

    Signals:
    - Question marks (？ ?)
    - Topic transition markers (also, besides, etc.)
    - Multiple line blocks (≥3 blocks of >15 chars)
    """
    q_count = 0
    for m in MULTI_Q_MARKERS:
        q_count += prompt.count(m)
    lower = prompt.lower()
    for m in _get_multi_topic():
        if m in lower:
            q_count += 1
    blocks = [b.strip() for b in prompt.split("\n") if len(b.strip()) > 15]
    if len(blocks) >= 3:
        q_count += len(blocks) - 1
    return q_count


def multi_question_injection(prompt: str) -> Optional[str]:
    """Detect multi-question prompt → return checklist discipline text.

    Returns injection text if ≥2 questions/topics detected, else None.
    Skips / commands and prompts < 20 chars.
    """
    stripped = prompt.strip()
    if stripped.startswith("/"):
        return None
    if len(stripped) < 20:
        return None
    q_count = count_questions(stripped)
    if q_count < 2:
        return None
    return i18n_msg("prompt_guard.multi_question", count=q_count)


# ── Clarity Score ────────────────────────────────────────


def clarity_score(
    prompt: str,
    *,
    prefs_path: Optional[str] = None,
) -> float:
    """Rate prompt clarity 0.0–1.0 (1 = perfectly clear).

    Rule-based (no LLM call). Factors:
    - Length deductions (< 6 chars: -0.3, < 15: -0.2)
    - Vague reference deductions (-0.2 each, max 2)
    - No technical identifiers: -0.15
    - Specific file path bonus: +0.2
    - Known command bonus: +0.3
    - Historical calibration from prefs_path: ±0.1
    """
    score = 1.0
    lower = prompt.lower().strip()

    # Deductions
    if len(prompt) < 6:
        score -= 0.3
    elif len(prompt) < 15:
        score -= 0.2
    vague_count = sum(1 for w in _get_vague() if w in lower)
    score -= 0.2 * min(vague_count, 2)
    has_tech = any(c in prompt for c in ("/", ".", "\\", "(", "def ", "class "))
    if not has_tech:
        score -= 0.15

    # Bonuses
    if re.search(r"[/\\][\w.]+\.\w+", prompt):
        score += 0.2
    if any(prompt.strip().startswith(c) for c in _get_known_commands()):
        score += 0.3

    # Historical calibration
    score += _preference_adjustment(lower, prefs_path)

    return max(0.0, min(1.0, score))


def _preference_adjustment(
    prompt_lower: str,
    prefs_path: Optional[str],
) -> float:
    """Adjust clarity score from preference_model.json.

    Keys:
    - asked_too_much_patterns: raise threshold → +0.1 (don't ask)
    - should_have_asked_patterns: lower threshold → -0.1 (do ask)
    """
    if not prefs_path or not os.path.isfile(prefs_path):
        return 0.0
    try:
        with open(prefs_path, encoding="utf-8") as f:
            prefs = json.load(f)
        for p in prefs.get("asked_too_much_patterns", []):
            if p in prompt_lower:
                return 0.1
        for p in prefs.get("should_have_asked_patterns", []):
            if p in prompt_lower:
                return -0.1
    except Exception:
        pass
    return 0.0


def is_irreversible(
    prompt: str,
    *,
    keywords: Optional[tuple[str, ...]] = None,
) -> bool:
    """Check if prompt involves irreversible actions."""
    kws = keywords or _get_irreversible()
    lower = prompt.lower()
    return any(kw in lower for kw in kws)


def run_clarity_gate(
    prompt: str,
    *,
    prefs_path: Optional[str] = None,
) -> Optional[dict]:
    """TADS-3 clarity gate: deny if ambiguous + irreversible.

    Returns a deny dict (hookSpecificOutput) if blocked, else None.
    Design principles:
    - Default: allow (asking bar > not-asking bar)
    - / commands: never block
    - Long prompts (>500 chars): skip (likely pasted code)
    - Only blocks when BOTH clarity < 0.4 AND irreversible
    """
    stripped = prompt.strip()
    if stripped.startswith("/"):
        return None
    if len(stripped) > 500:
        return None

    score = clarity_score(stripped, prefs_path=prefs_path)

    if score < 0.4 and is_irreversible(stripped):
        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "decision": "block",
                "reason": (
                    i18n_msg(
                        "prompt_guard.clarity_deny",
                        score=f"{score:.1f}",
                    )
                ),
            }
        }
    return None
