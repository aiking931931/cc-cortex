"""cc_cortex.identity_guard — Prevent Agent from modifying identity configuration.

@module identity_guard
@responsibility PreToolUse gate protecting CLAUDE.md, .claude/settings, rules,
               and hook configs from unauthorized modification. Fail-open.
@dependencies cc_cortex.constants, cc_cortex.guards.base
@exports check, is_identity_file, classify_bash_identity, IdentityGuard
"""

from __future__ import annotations

import os
import re
from typing import Optional

from cc_cortex.constants import make_deny
from cc_cortex.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

# ── Default Protected Patterns ────────────────────────────────────

# Basename exact matches (case-insensitive)
_PROTECTED_BASENAMES = frozenset([
    "claude.md",
    "settings.json",
    "settings.local.json",
])

# Path component patterns (matched against normalized path)
_PROTECTED_PATH_PATTERNS: list[re.Pattern] = [
    # .claude/rules/*.md (supports both absolute and relative paths)
    re.compile(r"(?:^|[/\\])\.claude[/\\]rules[/\\].*\.md$", re.IGNORECASE),
    # .claude/settings*.json
    re.compile(r"(?:^|[/\\])\.claude[/\\]settings[^/\\]*\.json$", re.IGNORECASE),
    # .claude/hooks/ config files (json/yaml/yml)
    re.compile(
        r"(?:^|[/\\])\.claude[/\\]hooks[/\\][^/\\]*\.(?:json|ya?ml)$",
        re.IGNORECASE,
    ),
]

# Bash commands that could modify identity files
_BASH_IDENTITY_PATTERNS: list[tuple[re.Pattern, str]] = [
    (
        re.compile(
            r"""(?:cat|echo|printf)\s+.*>+\s*['"]*[^\s'"]*"""
            r"""(?:CLAUDE\.md|settings\.json|settings\.local\.json)""",
            re.IGNORECASE,
        ),
        "redirect to identity config",
    ),
    (
        re.compile(
            r"""tee\s+[^\s|]*(?:CLAUDE\.md|settings\.json|settings\.local\.json)""",
            re.IGNORECASE,
        ),
        "redirect to identity config",
    ),
    (
        re.compile(
            r"(?:sed|awk)\s+.*-i[^\s]*\s+.*"
            r"(?:CLAUDE\.md|settings\.json|settings\.local\.json)",
            re.IGNORECASE,
        ),
        "in-place edit of identity config",
    ),
    (
        re.compile(
            r"(?:rm|del|Remove-Item)\s+.*"
            r"(?:CLAUDE\.md|settings\.json|\.claude[/\\]rules[/\\])",
            re.IGNORECASE,
        ),
        "delete identity config",
    ),
    (
        re.compile(
            r"(?:mv|move|ren|Rename-Item)\s+.*"
            r"(?:CLAUDE\.md|settings\.json|\.claude[/\\]rules[/\\])",
            re.IGNORECASE,
        ),
        "rename/move identity config",
    ),
]


# ── Classification ────────────────────────────────────────────────


def _normalize_path(path: str) -> str:
    """Normalize path for comparison."""
    return os.path.normpath(path).replace("\\", "/")


def is_identity_file(
    file_path: str,
    *,
    extra_basenames: frozenset[str] | None = None,
    extra_patterns: list[re.Pattern] | None = None,
) -> tuple[bool, str]:
    """Check if a file path is an identity configuration file.

    Returns:
        (is_protected, reason) tuple.
    """
    if not file_path:
        return False, ""

    normalized = _normalize_path(file_path)
    basename = os.path.basename(normalized).lower()

    # Check basenames
    check_basenames = _PROTECTED_BASENAMES
    if extra_basenames:
        check_basenames = check_basenames | extra_basenames

    if basename in check_basenames:
        return True, f"identity config: {basename}"

    # Check path patterns
    patterns = list(_PROTECTED_PATH_PATTERNS)
    if extra_patterns:
        patterns.extend(extra_patterns)

    for pattern in patterns:
        if pattern.search(normalized):
            return True, f"identity config: {os.path.basename(file_path)}"

    return False, ""


def classify_bash_identity(command: str) -> tuple[bool, str]:
    """Check if a Bash command targets identity files.

    Returns:
        (is_dangerous, reason) tuple.
    """
    if not command:
        return False, ""

    for pattern, desc in _BASH_IDENTITY_PATTERNS:
        if pattern.search(command):
            return True, f"Bash {desc}"

    return False, ""


# ── Public API ────────────────────────────────────────────────────


def check(
    tool_name: str,
    tool_input: dict,
    *,
    extra_basenames: frozenset[str] | None = None,
    extra_patterns: list[re.Pattern] | None = None,
) -> Optional[dict]:
    """Check if a tool call attempts to modify identity configuration.

    Args:
        tool_name: Claude Code tool name (Edit/Write/Bash/etc.)
        tool_input: Tool input dict.
        extra_basenames: Additional protected basenames (lowercase).
        extra_patterns: Additional compiled regex patterns for paths.

    Returns:
        Dict {permissionDecision: "deny", reason, additionalContext} or None.
    """
    if not isinstance(tool_input, dict):
        return None

    # Edit / Write — check file_path
    if tool_name in ("Edit", "Write", "NotebookEdit"):
        file_path = (
            tool_input.get("file_path")
            or tool_input.get("notebook_path")
            or tool_input.get("path")
            or ""
        )
        protected, reason = is_identity_file(
            file_path,
            extra_basenames=extra_basenames,
            extra_patterns=extra_patterns,
        )
        if protected:
            short_path = os.path.basename(file_path)
            return make_deny(
                f"🛡️ Identity Guard: {tool_name} blocked on {short_path}",
                additionalContext=(
                    "Protected identity config. "
                    "User must make this change directly or explicitly instruct agent."
                ),
            )

    # Bash — check command
    if tool_name == "Bash":
        command = tool_input.get("command", "")
        dangerous, reason = classify_bash_identity(command)
        if dangerous:
            return make_deny(
                f"🛡️ Identity Guard: Bash blocked — {reason}",
                additionalContext=(
                    "Bash modifies identity config. "
                    "User must run this directly or explicitly instruct agent."
                ),
            )

    return None


# ── BaseGuard adapter ───────────────────────────────────────────


class IdentityGuard(BaseGuard):
    """Prevent Agent from modifying identity configuration."""

    name = "identity_guard"
    category = GuardCategory.SECURITY

    def check(self, ctx: GuardContext) -> GuardResult | None:
        """Block Edit/Write/Bash targeting CLAUDE.md, rules, settings, or hook configs.

        Args:
            ctx: Guard context with tool_name and tool_input.

        Returns:
            GuardResult.deny explaining the protected file, or None if safe.
        """
        result = check(ctx.tool_name, ctx.tool_input)
        if result is None:
            return None
        return GuardResult.deny(
            result.get("reason", self.name),
            context=result.get("additionalContext", ""),
        )
