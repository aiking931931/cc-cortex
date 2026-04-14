"""cc_cortex.window_guard — Prevent console window flashing on Windows.

@module window_guard
@responsibility PreToolUse gate that detects Bash commands spawning visible console
    windows (schtasks, reg, wmic, etc.) and blocks unless wrapped with a hidden executor.
@dependencies cc_cortex.constants, cc_cortex.guards.base
@exports check, WindowGuard
"""

from __future__ import annotations

import re
from typing import Optional

from cc_cortex.constants import make_deny
from cc_cortex.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

# Commands that flash a console window on Windows
_FLASH_CMDS = re.compile(
    r"\b(?:schtasks|reg\b|msiexec|wmic|netsh|sc\s"
    r"|runas|certutil|bitsadmin|mshta)\b",
    re.IGNORECASE,
)

# Hidden wrappers that suppress the window
_HIDDEN_WRAPPERS = re.compile(
    r"run-hidden\.pyw|run-hidden-utf8\.ps1|Run-Hidden\b",
    re.IGNORECASE,
)

DEFAULT_MESSAGE = (
    "Window Flash Guard: `{cmd}` spawns a visible console window. "
    "Wrap with a hidden executor to prevent flashing."
)


def check(
    tool_name: str,
    tool_input: dict,
    *,
    message: Optional[str] = None,
) -> Optional[dict]:
    """Check if a Bash command would flash a console window.

    Args:
        tool_name: Must be "Bash" to trigger.
        tool_input: Tool input dict with "command" key.
        message: Override message template ({cmd} placeholder).

    Returns:
        Dict {permissionDecision: "deny", cmd, message} or None.
    """
    if tool_name != "Bash":
        return None
    command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
    if not command:
        return None
    if _HIDDEN_WRAPPERS.search(command):
        return None
    match = _FLASH_CMDS.search(command)
    if not match:
        return None

    cmd_found = match.group(0)
    msg = (message or DEFAULT_MESSAGE).format(cmd=cmd_found)
    return make_deny(msg, cmd=cmd_found)


# ── BaseGuard adapter ───────────────────────────────────────────


class WindowGuard(BaseGuard):
    """Prevent console window flashing on Windows."""

    name = "window_guard"
    category = GuardCategory.QUALITY

    def check(self, ctx: GuardContext) -> GuardResult | None:
        """Block Bash commands that spawn visible console windows on Windows.

        Args:
            ctx: Guard context with tool_name and tool_input.

        Returns:
            GuardResult.deny suggesting a hidden wrapper, or None if safe.
        """
        result = check(ctx.tool_name, ctx.tool_input)
        if result is None:
            return None
        return GuardResult.deny(
            result.get("reason", self.name),
            context=result.get("additionalContext", ""),
            cmd=result.get("cmd", ""),
        )
