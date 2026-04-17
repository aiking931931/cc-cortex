"""concinno.git_safety — Detect and block dangerous git operations.

@module git_safety
@responsibility PreToolUse gate that catches force push, reset --hard, branch -D,
               clean -fd, checkout -- . Returns deny dict or None (fail-open).
@dependencies concinno.constants, concinno.guards.base
@exports check, GitSafetyGuard
"""

from __future__ import annotations

import re
from typing import Optional

from concinno.constants import make_deny
from concinno.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

# Dangerous git command patterns (order: most dangerous first)
_DANGEROUS_PATTERNS: list[tuple[str, re.Pattern, str]] = [
    (
        "force push",
        re.compile(r"git\s+push\s+.*--force(?!-with-lease)", re.IGNORECASE),
        "Use --force-with-lease instead of --force to avoid overwriting others' work.",
    ),
    (
        "reset --hard",
        re.compile(r"git\s+reset\s+--hard", re.IGNORECASE),
        "This discards ALL uncommitted changes. Consider git stash first.",
    ),
    (
        "clean -f",
        re.compile(r"git\s+clean\s+-[a-z]*f", re.IGNORECASE),
        "This permanently deletes untracked files. Consider git stash -u first.",
    ),
    (
        "checkout -- .",
        re.compile(r"git\s+checkout\s+--\s*\.", re.IGNORECASE),
        "This discards ALL unstaged changes. Consider git stash first.",
    ),
    (
        "branch -D",
        re.compile(r"git\s+branch\s+-D\s"),  # case-sensitive: -D only
        "Force-deletes branch even if unmerged. Use -d for safe delete.",
    ),
    (
        "rebase -i (interactive)",
        re.compile(r"git\s+rebase\s+-i\b", re.IGNORECASE),
        "Interactive rebase requires manual input which is not supported.",
    ),
    (
        "push to main/master",
        re.compile(
            r"git\s+push\s+.*(?:origin\s+)?(?:main|master)\b(?!.*--force-with-lease)",
            re.IGNORECASE,
        ),
        "Direct push to main/master. Consider using a PR workflow.",
    ),
]


def check(
    tool_name: str,
    tool_input: dict,
    *,
    extra_patterns: list[tuple[str, re.Pattern, str]] | None = None,
) -> Optional[dict]:
    """Check if a Bash command contains dangerous git operations.

    Args:
        tool_name: Must be "Bash" to trigger.
        tool_input: Tool input dict with "command" key.
        extra_patterns: Additional (name, regex, reason) patterns.

    Returns:
        Dict {permissionDecision: "deny", cmd, reason} or None.
    """
    if tool_name != "Bash":
        return None
    command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
    if not command:
        return None

    # Quick check: must contain "git" at all
    if "git" not in command.lower():
        return None

    patterns = list(_DANGEROUS_PATTERNS)
    if extra_patterns:
        patterns.extend(extra_patterns)

    for name, pattern, advice in patterns:
        if pattern.search(command):
            return make_deny(
                f"⚠️ Dangerous git operation: {name}. {advice}",
                cmd=name,
            )

    return None


# ── BaseGuard adapter ───────────────────────────────────────────


class GitSafetyGuard(BaseGuard):
    """Detect dangerous git operations."""

    name = "git_safety"
    category = GuardCategory.SECURITY

    def check(self, ctx: GuardContext) -> GuardResult | None:
        """Block dangerous git operations (force push, reset --hard, branch -D).

        Args:
            ctx: Guard context with tool_name and tool_input.

        Returns:
            GuardResult.deny with safer alternative advice, or None if safe.
        """
        result = check(ctx.tool_name, ctx.tool_input)
        if result is None:
            return None
        return GuardResult.deny(
            result.get("reason", self.name),
            context=result.get("additionalContext", ""),
            cmd=result.get("cmd", ""),
        )
