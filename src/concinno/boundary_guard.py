"""concinno.boundary_guard — CC/CCC boundary violation detection.

@module boundary_guard
@responsibility Detect and deny writes that violate CC/CCC boundary: hook files
    with too much business logic, or library files with personal paths/hardcoded CJK.
@dependencies concinno.constants, concinno.guards.base
@exports gen_boundary, BoundaryGuard
"""

from __future__ import annotations

from typing import Optional

from concinno.constants import WRITE_TOOLS_EXT as _WRITE_TOOLS
from concinno.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

# ── Personal/hardcoded path patterns ─────────────────────────

def _personal_path_patterns() -> tuple[str, ...]:
    """Personal path patterns: configured brain dir + legacy names + OS roots."""
    from concinno.core.config import get_config
    brain_dir = get_config().brain_dir
    return (
        brain_dir,
        "_AI_BRAIN",  # Legacy brain dir name (pre-2fcf1a1b); never in CCC library
        ":\\",
        "/home/",
        "~/.claude/hooks",
    )

# CJK ranges that indicate hardcoded Chinese text (not i18n-safe)
_CJK_RANGES = (
    ("\u4e00", "\u9fff"),   # CJK Unified Ideographs
    ("\u3400", "\u4dbf"),   # CJK Extension A
)

# Hook file path patterns
_HOOK_PATH_PATTERNS = (
    ".claude/hooks/",
    ".claude\\hooks\\",
    "hooks/on-pre-tool",
    "hooks/on-post-tool",
    "hooks/on-stop",
    "hooks/session_cleanup",
    "hooks/session_start",
)

# Lines that are clearly stdin/stdout/import boilerplate in hooks
_HOOK_BOILERPLATE_MARKERS = (
    "import ",
    "from ",
    "sys.stdin",
    "sys.stdout",
    "sys.stderr",
    "json.load",
    "json.dump",
    "print(",
    "if __name__",
    "def main",
    "#",
    '"""',
    "'''",
)


# ── Helpers ──────────────────────────────────────────────────


def _is_hook_path(file_path: str) -> bool:
    """Check if file_path looks like a CC hook file."""
    normalized = file_path.replace("\\", "/").lower()
    return any(p.replace("\\", "/").lower() in normalized for p in _HOOK_PATH_PATTERNS)


def _is_cortex_path(file_path: str) -> bool:
    """Check if file_path is inside the concinno library."""
    normalized = file_path.replace("\\", "/").lower()
    return "concinno/" in normalized or "concinno/" in normalized


def _count_business_lines(content: str) -> int:
    """Count non-boilerplate lines in hook content."""
    count = 0
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if any(stripped.startswith(m) for m in _HOOK_BOILERPLATE_MARKERS):
            continue
        count += 1
    return count


def _has_personal_paths(content: str) -> bool:
    """Check if content contains hardcoded personal paths."""
    return any(p in content for p in _personal_path_patterns())


def _has_hardcoded_cjk(content: str) -> bool:
    """Check if content contains CJK characters (hardcoded Chinese)."""
    for char in content:
        for lo, hi in _CJK_RANGES:
            if lo <= char <= hi:
                return True
    return False


# ── Main check function ─────────────────────────────────────


def gen_boundary(
    tool_name: str,
    tool_input: dict,
    *,
    hook_business_threshold: int = 20,
) -> Optional[tuple[str, str]]:
    """Detect CC/CCC boundary violations.

    Two directions:
    1. Hook file with too much business logic → should be a CCC module
    2. CCC library file with personal paths/hardcoded CJK → should be CC config

    Args:
        tool_name: Tool name (Write/Edit).
        tool_input: Tool input dict.
        hook_business_threshold: Max non-boilerplate lines before warning.

    Returns:
        ("boundary", message) or None.
    """
    if tool_name not in _WRITE_TOOLS:
        return None

    file_path = (
        tool_input.get("file_path")
        or tool_input.get("path")
        or ""
    )
    if not file_path:
        return None

    content = tool_input.get("content") or tool_input.get("new_string") or ""
    if not content:
        return None

    # Direction 1: Hook file with too much business logic
    if _is_hook_path(file_path):
        biz_lines = _count_business_lines(content)
        if biz_lines > hook_business_threshold:
            return (
                "boundary",
                f"[BoundaryGuard] Hook file has {biz_lines} business logic lines "
                f"(threshold: {hook_business_threshold}). "
                f"Consider extracting to a concinno module — "
                f"hooks should be thin wrappers (stdin→CCC API→stdout).",
            )

    # Direction 2: CCC library with personal paths or hardcoded CJK
    if _is_cortex_path(file_path):
        violations: list[str] = []
        if _has_personal_paths(content):
            violations.append("personal/hardcoded paths")
        if _has_hardcoded_cjk(content):
            violations.append("hardcoded CJK text")

        if violations:
            detail = " + ".join(violations)
            return (
                "boundary",
                f"[BoundaryGuard] CCC library file contains {detail}. "
                f"These belong in the CC application layer (config/hooks), "
                f"not in the reusable library.",
            )

    return None


# ── BaseGuard adapter ───────────────────────────────────────


class BoundaryGuard(BaseGuard):
    """PreToolUse: deny writes that violate CC/CCC boundary."""

    name = "boundary_guard"
    category = GuardCategory.QUALITY

    def check(self, ctx: GuardContext) -> GuardResult | None:
        """Deny writes violating CC/CCC boundary (fat hooks or personal paths in lib).

        Args:
            ctx: Guard context with tool_name and tool_input.

        Returns:
            GuardResult.deny with boundary violation details, or None if clean.
        """
        result = gen_boundary(ctx.tool_name, ctx.tool_input)
        if result is None:
            return None
        _name, msg = result
        return GuardResult.deny(reason=msg)

    def on_post_tool(self, ctx: GuardContext) -> GuardResult | None:
        """No-op PostToolUse placeholder.

        Args:
            ctx: Guard context (unused).

        Returns:
            Always None.
        """
        return None
