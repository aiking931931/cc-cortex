"""cc_cortex.constants — Shared tool-set constants and gate helpers.

@module constants
@responsibility Single source of truth for tool classifications (WRITE_TOOLS, READ_TOOLS)
    and gate response factories (make_deny, make_allow) used across all guards.
@dependencies (none — leaf module)
@exports WRITE_TOOLS, WRITE_TOOLS_EXT, READ_TOOLS, make_deny, make_allow
"""

from __future__ import annotations

# ── Tool Classifications ────────────────────────────────────────
# Core write tools (Claude Code native)
WRITE_TOOLS = frozenset({"Edit", "Write", "NotebookEdit"})

# Extended write tools (includes MCP tools like MultiEdit)
WRITE_TOOLS_EXT = frozenset({*WRITE_TOOLS, "MultiEdit"})

# Read-only / exploration tools
READ_TOOLS = frozenset({"Read", "Grep", "Glob", "WebSearch", "WebFetch"})


# ── Gate Response Factories ─────────────────────────────────────

def make_deny(reason: str, **extra) -> dict:
    """Build a standard PreToolUse deny response."""
    result = {"permissionDecision": "deny", "reason": reason}
    result.update(extra)
    return result


def make_allow(**extra) -> dict:
    """Build a standard PreToolUse allow response."""
    result: dict = {"permissionDecision": "allow"}
    result.update(extra)
    return result
