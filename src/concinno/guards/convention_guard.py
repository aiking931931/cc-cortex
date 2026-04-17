"""Convention enforcement guard — checks naming and placement on Write.

Registered in QUALITY layer. Fires on Write tool calls.
Mode: advisory (ALLOW + context) or strict (DENY).
Controlled by cc_config.json ``convention_strict`` flag (default: false).
"""

from __future__ import annotations

import json
import os

from concinno.guards.base import (
    BaseGuard,
    GuardCategory,
    GuardContext,
    GuardResult,
)


def _is_strict() -> bool:
    """Check if convention enforcement is strict (DENY) or advisory (ALLOW)."""
    cfg_path = os.path.join(
        os.path.expanduser("~"), ".claude", "hooks", "cc_config.json",
    )
    try:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
        return bool(cfg.get("convention_strict", False))
    except (OSError, ValueError):
        return False


class ConventionGuard(BaseGuard):
    """Check file naming and placement conventions on Write.

    Default: advisory (ALLOW with context suggestion).
    Set ``convention_strict: true`` in cc_config.json → DENY on violations.
    """

    name = "convention"
    category = GuardCategory.QUALITY

    def check(self, ctx: GuardContext) -> GuardResult | None:
        if ctx.tool_name != "Write":
            return None

        file_path = ctx.tool_input.get("file_path", "")
        if not file_path:
            return None

        from concinno.convention_engine import ConventionEngine

        engine = ConventionEngine()
        filename = os.path.basename(file_path)

        naming = engine.check_naming(filename)
        placement = engine.check_placement(file_path)

        issues: list[str] = []
        if not naming.passed:
            try:
                from concinno.cbua_ux import CbuaCode, cbua_format
                issues.append(cbua_format(CbuaCode.A3_NAME, naming.suggestion))
            except ImportError:
                issues.append(f"A3.Naming: {naming.suggestion}")

        if not placement.passed:
            try:
                from concinno.cbua_ux import CbuaCode, cbua_format
                issues.append(cbua_format(CbuaCode.A3_PLACE, placement.suggestion))
            except ImportError:
                issues.append(f"A3.Placement: {placement.suggestion}")

        if not issues:
            return None

        reason = "\n".join(issues)
        if _is_strict():
            return GuardResult.deny(reason)
        return GuardResult.allow(reason=reason)
