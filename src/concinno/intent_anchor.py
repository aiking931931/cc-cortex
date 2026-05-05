"""concinno.intent_anchor — Stage -1 intent anchoring (CBUA v3.2 minimal).

@module intent_anchor
@responsibility Persist a user's task intent across the session as a
    structured anchor with two extension fields beyond the bare summary:
    ``done_spec`` ("what done looks like") and ``constraints``
    ("boundaries / exclusions / disambiguators"). Used by
    :class:`concinno.intent_anchor_guard.IntentAnchorGuard` for periodic
    re-injection and by :func:`concinno.hooks.on_prompt_submit
    .handle_prompt_submit` for first-turn Stage -1 injection.
@dependencies (pure stdlib + soft import of intent_anchor_guard for
    summary extraction)
@exports IntentAnchor, extract_anchor, render_anchor_block

Ship gate (2026-04-25 paper-pass ablation on 12 GAIA baseline_v1 FAIL
items): full Stage -1 (RaR + DoneFramework + Step-Back) yielded only
3-4 hard flips. Per gate (<3 = KILL, 3-4 = minimal, >=5 = full),
shipped minimal version is just ``done_spec`` + ``constraints`` —
``.interpretation`` (RaR rephrase), ``.inputs_needed`` (DoneFramework
Q2), and ``.principle`` (Step-Back) were dropped.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

# Patterns for done_spec — output-form cues. Order matters: more specific
# patterns first so they take precedence. ``\s*`` (not ``\s+``) is used
# wherever a CJK script is also a valid token, since Chinese / Japanese
# typically join tokens without whitespace.
_DONE_SPEC_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"\bas (?:a )?(?:list|number|integer|float|string|json|csv|"
        r"markdown table|table|yaml|toml|date|year)\b[^.!?\n]{0,60}",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:回答|answer|return|output|寫出|列出|generate|produce|give "
        r"me|報告|提供)\s*(?:[a-z一-鿿][^.!?\n]{2,80})",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:count|number|quantity|sum|total)\s+of\b[^.!?\n]{2,60}",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:[a-z一-鿿][^.!?\n]{2,40})\s*(?:的數量|的數字|的個數)",
        re.IGNORECASE,
    ),
]

# Patterns for constraints — limit / disambiguation cues.
_CONSTRAINT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"(?:must not|cannot|不能|禁止|exclude|不要|do not|don'?t)\s*"
        r"(?:[a-z一-鿿][^.!?\n]{2,60})",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:as of|截至|by|before|prior to)\s+(\d{4}(?:-\d{1,2}-\d{1,2})?"
        r"|\d{1,2}/\d{1,2}/\d{2,4})",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bonly\s+(?:if|when|the|those|[a-z一-鿿][^.!?\n]{2,40})",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:必須|限定|限於|限制\s*為|限制\s*在)\s*[^.!?\n]{2,40}",
    ),
]


@dataclass
class IntentAnchor:
    """Structured intent capture for CBUA Stage -1 anchoring.

    Fields:
        summary: One-line restatement of the task (existing v2.9 ``intent``).
        original: Raw first user prompt — Reflexion grounds to this, never
            to ``summary``, to avoid Reverse Goodhart drift.
        done_spec: "What done looks like" — output form / type / scope.
        constraints: "Boundaries / exclusions" — ambiguity disambiguators.
    """

    summary: str = ""
    original: str = ""
    done_spec: str = ""
    constraints: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> IntentAnchor:
        """Deserialise. Honours legacy ``intent`` key from v2.9 state."""
        return cls(
            summary=d.get("summary") or d.get("intent", "") or "",
            original=d.get("original", "") or "",
            done_spec=d.get("done_spec", "") or "",
            constraints=d.get("constraints", "") or "",
        )

    def is_empty(self) -> bool:
        return not (self.summary or self.original)


def _heuristic_done_spec(prompt: str) -> str:
    """Pull a done_spec hint from *prompt* by pattern matching only.

    Returns the empty string when no pattern matches. Heuristic misses
    leave the field blank rather than fabricating a spec — Stage -1
    treats the anchor as a re-orientation cue, not as ground truth, so
    an empty field is honest and safe.
    """
    if not prompt:
        return ""
    text = prompt.strip()
    for pat in _DONE_SPEC_PATTERNS:
        m = pat.search(text)
        if m:
            captured = m.group(0).strip()
            return captured[:120]
    return ""


def _heuristic_constraints(prompt: str) -> str:
    if not prompt:
        return ""
    text = prompt.strip()
    found: list[str] = []
    for pat in _CONSTRAINT_PATTERNS:
        for m in pat.finditer(text):
            captured = m.group(0).strip()[:80]
            if captured and captured not in found:
                found.append(captured)
            if len(found) >= 3:
                break
        if len(found) >= 3:
            break
    return " | ".join(found)


def extract_anchor(
    prompt: str,
    *,
    summary_max_len: int = 200,
) -> IntentAnchor:
    """Build an :class:`IntentAnchor` from a raw user prompt.

    Pure heuristics — no LLM, no network. Designed to be cheap enough to
    run on every UserPromptSubmit hook on first turn. Heuristic misses
    leave fields blank rather than calling out to an LLM judge (per the
    2026-04-25 ship-gate decision: deterministic NER+number diff, never
    LLM judgment, to avoid persona priming side effects).
    """
    if not prompt:
        return IntentAnchor()
    try:
        from concinno.intent_anchor_guard import _extract_intent

        summary = _extract_intent(prompt, max_len=summary_max_len)
    except Exception:  # noqa: BLE001 - soft import, never break extraction
        summary = (prompt or "")[:summary_max_len].strip()
    return IntentAnchor(
        summary=summary,
        original=prompt,
        done_spec=_heuristic_done_spec(prompt),
        constraints=_heuristic_constraints(prompt),
    )


def render_anchor_block(
    anchor: IntentAnchor,
    *,
    title: str = "🎯 意圖錨定",
) -> str:
    """Render an :class:`IntentAnchor` as an additionalContext block."""
    lines = [title]
    if anchor.summary:
        lines.append(f"原始意圖：{anchor.summary}")
    if anchor.done_spec:
        lines.append(f"做完長什麼樣：{anchor.done_spec}")
    if anchor.constraints:
        lines.append(f"限制：{anchor.constraints}")
    if not (anchor.done_spec or anchor.constraints):
        lines.append(
            "（尚未建立 done_spec / constraints — 答前可自行補完。）"
        )
    lines.append("當前動作是否對齊原始目標？偏離 → 拉回。")
    return "\n".join(lines)
