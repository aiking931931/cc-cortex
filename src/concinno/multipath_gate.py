"""concinno.multipath_gate — Force multiple alternatives in plans.

@module multipath_gate
@responsibility RLHF Side-Effects B4 (Premature Convergence) and B5 (First-Answer
    Lock-in): when writing plans, proposals, or architectural decisions,
    force the AI to list ≥3 alternatives before committing to one.
@dependencies concinno.guards.base, concinno.i18n
@exports MultiPathGate
"""

from __future__ import annotations

import re
from typing import Optional

from concinno.constants import WRITE_TOOLS_EXT
from concinno.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

# Files where multi-path analysis is required
_DECISION_FILE_PATTERNS = (
    "planning", "05_planning", "plan", "architecture",
    "decision", "proposal", "spec", "rfc", "adr",
    "規劃", "計畫", "決策", "架構", "提案",
)

# Markers that indicate this is a decision/plan (not just a status update)
_DECISION_MARKERS = re.compile(
    r"(?:"
    r"\b(?:approach|strategy|design|solution|implementation)\b|"
    r"\b(?:we (?:should|will|can)|I (?:recommend|suggest|propose))\b|"
    r"\b(?:the plan is|proposed approach|selected option)\b|"
    r"(?:方案|策略|設計|方向|建議|提案|實作方式)\b"
    r")",
    re.IGNORECASE,
)

# Evidence that alternatives were considered (≥3 options listed)
_ALTERNATIVE_PATTERNS = [
    # Numbered options: "Option 1", "方案 A", "Approach 1"
    re.compile(
        r"(?:option|approach|方案|選項|alternative)\s*[A-C1-3]",
        re.IGNORECASE,
    ),
    # Comparison tables: "| Option |" or "| 方案 |"
    re.compile(
        r"\|\s*(?:option|approach|方案|選項|name|名稱)\s*\|",
        re.IGNORECASE,
    ),
    # Pros/cons structure
    re.compile(
        r"(?:pros?\s*(?:&|and|/)\s*cons?|advantages?\s*(?:&|and|/)\s*disadvantages?|"
        r"優缺點|利弊)",
        re.IGNORECASE,
    ),
    # Explicit listing: "1.", "2.", "3." in sequence for options
    re.compile(
        r"(?:^|\n)\s*[1①]\s*[.）].+\n\s*[2②]\s*[.）].+\n\s*[3③]\s*[.）]",
        re.MULTILINE,
    ),
    # "三" or "three" + options/alternatives
    re.compile(
        r"(?:three|3|三)\s*(?:options?|alternatives?|approaches?|選項|方案)",
        re.IGNORECASE,
    ),
]

# Minimum content length to trigger (short edits are exempt)
_MIN_CONTENT_LEN = 100


def _is_decision_file(path: str) -> bool:
    """Check if the file is a planning/decision document."""
    if not path:
        return False
    norm = path.replace("\\", "/").lower()
    return any(p in norm for p in _DECISION_FILE_PATTERNS)


def _has_decision_content(content: str) -> bool:
    """Check if content contains decision/plan language."""
    return bool(_DECISION_MARKERS.search(content))


def _has_alternatives(content: str) -> bool:
    """Check if content lists ≥3 alternatives."""
    return any(pat.search(content) for pat in _ALTERNATIVE_PATTERNS)


class MultiPathGate(BaseGuard):
    """Force ≥3 alternatives in planning/decision documents.

    RLHF Side-Effects B4 (Premature Convergence) and B5 (First-Answer Lock-in):
    the AI locks onto the first viable solution without exploring alternatives.
    Auto-regressive generation makes the first token determine direction,
    and backtracking cost is high.

    This gate ensures that when writing to planning/decision files,
    the AI has considered at least 3 options before committing.
    """

    name = "multipath_gate"
    category = GuardCategory.QUALITY
    step_back_reason = "premature convergence — list ≥3 alternatives first"

    def check(self, ctx: GuardContext) -> Optional[GuardResult]:
        """Deny plan/decision writes without ≥3 alternatives."""
        if ctx.tool_name not in WRITE_TOOLS_EXT:
            return None

        path = ctx.tool_input.get("file_path", "") or ctx.tool_input.get("path", "")
        if not _is_decision_file(path):
            return None

        # Get the content being written
        content = (
            ctx.tool_input.get("content", "")
            or ctx.tool_input.get("new_string", "")
            or ""
        )
        if len(content) < _MIN_CONTENT_LEN:
            return None

        # Must contain decision language
        if not _has_decision_content(content):
            return None

        # Check if alternatives are present
        if _has_alternatives(content):
            return None

        return GuardResult.deny(
            "Premature convergence: plan/decision written without "
            "listing ≥3 alternatives.",
            context=(
                "⚠ RLHF B4/B5 MultiPath Guard: you're committing to a "
                "plan without exploring alternatives. First-answer lock-in "
                "is a known RLHF bias.\n\n"
                "Before writing this plan, add:\n"
                "1. **Option A** — [current approach] + pros/cons\n"
                "2. **Option B** — [alternative 1] + pros/cons\n"
                "3. **Option C** — [alternative 2] + pros/cons\n"
                "4. **Selected**: [which and WHY]\n\n"
                "Then retry — the gate clears when ≥3 options are detected."
            ),
        )
