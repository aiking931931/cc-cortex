"""cc_cortex.proposal_guard — Force side-effect analysis on proposals.

@module proposal_guard
@responsibility DENY new proposals lacking side-effect analysis
@dependencies cc_cortex.constants, cc_cortex.guards.base
@exports ProposalGuard

Poka-Yoke: Writing a new proposal to a planning file
without side-effect analysis → DENY.

This guard enforces L2 sweet-spot thinking on every new task proposal.
"""

from __future__ import annotations

import re
from typing import Optional

from cc_cortex.constants import WRITE_TOOLS_EXT
from cc_cortex.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult
from cc_cortex.i18n import msg as i18n_msg
from cc_cortex.i18n import patterns as i18n_patterns

# ── Lazy-loaded patterns from i18n ───────────────────────

_planning: tuple[str, ...] | None = None
_proposal_re: re.Pattern | None = None
_side_effect_re: re.Pattern | None = None


def _get_planning() -> tuple[str, ...]:
    global _planning
    if _planning is None:
        _planning = tuple(i18n_patterns("proposal_guard.planning_patterns"))
    return _planning


def _get_proposal_re() -> re.Pattern:
    global _proposal_re
    if _proposal_re is None:
        parts = i18n_patterns("proposal_guard.proposal_markers")
        # Always include universal markers
        parts = list(parts) + [r"⬜", r"Phase\s+\d"]
        _proposal_re = re.compile("|".join(parts), re.IGNORECASE)
    return _proposal_re


def _get_side_effect_re() -> re.Pattern:
    global _side_effect_re
    if _side_effect_re is None:
        parts = i18n_patterns("proposal_guard.side_effect_kw")
        _side_effect_re = re.compile("|".join(parts), re.IGNORECASE)
    return _side_effect_re


def _is_planning_file(path: str) -> bool:
    """Check if path is a planning/task file."""
    if not path:
        return False
    norm = path.replace("\\", "/").lower()
    return any(p in norm for p in _get_planning())


def _get_written_content(tool_name: str, tool_input: dict) -> str:
    """Extract the content being written/edited."""
    if tool_name == "Write":
        return tool_input.get("content", "")
    if tool_name == "Edit":
        return tool_input.get("new_string", "")
    if tool_name == "NotebookEdit":
        return tool_input.get("new_source", "")
    return ""


def check_proposal(tool_name: str, tool_input: dict) -> Optional[str]:
    """Check if a proposal edit lacks side-effect analysis.

    Returns deny reason string, or None if OK.
    """
    if tool_name not in WRITE_TOOLS_EXT:
        return None

    path = tool_input.get("file_path", "") or tool_input.get("path", "")
    if not _is_planning_file(path):
        return None

    content = _get_written_content(tool_name, tool_input)
    if not content or len(content) < 10:
        return None

    if not _get_proposal_re().search(content):
        return None

    if _get_side_effect_re().search(content):
        return None

    return i18n_msg("proposal_guard.missing_analysis")


class ProposalGuard(BaseGuard):
    """Force side-effect analysis on new proposals (Poka-Yoke)."""

    name = "proposal_guard"
    category = GuardCategory.QUALITY
    step_back_reason = "new proposal missing side-effect analysis"

    def check(self, ctx: GuardContext) -> Optional[GuardResult]:
        """Deny new task proposals in planning files lacking side-effect analysis.

        Args:
            ctx: Guard context with tool_name and tool_input.

        Returns:
            GuardResult.deny requiring risk/impact section, or None if clean.
        """
        reason = check_proposal(ctx.tool_name, ctx.tool_input)
        if reason is None:
            return None
        return GuardResult.deny(
            reason,
            context=i18n_msg("proposal_guard.l2_requirement"),
        )
