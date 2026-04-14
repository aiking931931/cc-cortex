"""cc_cortex.orientation_gate — Force cost analysis before long operations.

@module orientation_gate
@responsibility RLHF Side-Effects B2 (Short-sightedness) and B3 (Action Bias):
    before executing long-running operations (deploy, build, install, large
    downloads), force the AI to have already considered cost, time, and
    alternatives. Deny if no recent planning context detected.
@dependencies cc_cortex.guards.base, cc_cortex.core.state_store
@exports OrientationGate
"""

from __future__ import annotations

import re
import time
from typing import Optional

from cc_cortex.core.state_store import StateStore
from cc_cortex.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

_NS = "orientation_gate"

# Commands that typically take >1 minute and burn significant resources.
_LONG_OPS = re.compile(
    r"\b(?:"
    r"deploy|npm\s+install|pip\s+install|yarn\s+install|pnpm\s+install|"
    r"docker\s+build|docker\s+compose\s+up|docker\s+pull|"
    r"cargo\s+build|go\s+build|make\s+all|gradle\s+build|mvn\s+install|"
    r"apt\s+install|brew\s+install|choco\s+install|"
    r"git\s+clone|wget\s+|curl\s+.*-o|"
    r"python\s+.*train|pytest\s+(?!.*-k)|vitest\s+run|"
    r"npx\s+playwright|npm\s+run\s+build|vite\s+build"
    r")\b",
    re.IGNORECASE,
)

# Evidence that the AI has already thought about cost/alternatives.
_PLANNING_EVIDENCE = re.compile(
    r"(?:"
    r"cost|altern|option|trade.?off|instead|compared|versus|"
    r"worth|benefit|risk|side.?effect|approach|strategy|"
    r"成本|替代|方案|取捨|值得|風險|副作用|"
    r"estimated|約.*分鐘|~\d+\s*min|ETA|timeout"
    r")",
    re.IGNORECASE,
)

# Planning window: how recent must planning evidence be (seconds).
_PLANNING_WINDOW_S = 120


def _is_long_operation(command: str) -> bool:
    """Check if a Bash command is a long-running operation."""
    if not command:
        return False
    return bool(_LONG_OPS.search(command))


def _extract_op_name(command: str) -> str:
    """Extract the operation name for user-friendly messaging."""
    m = _LONG_OPS.search(command)
    if m:
        return m.group().strip()
    return "long operation"


class OrientationGate(BaseGuard):
    """Force cost/alternative analysis before long-running operations.

    RLHF Side-Effects B2 (Short-sightedness) and B3 (Action Bias):
    the AI jumps into expensive operations without considering whether
    they're worth the time/cost, or whether a cheaper alternative exists.

    This gate tracks recent tool outputs for planning evidence. If the AI
    attempts a long operation without recent planning context, it's denied
    with a prompt to think first.
    """

    name = "orientation_gate"
    category = GuardCategory.QUALITY
    step_back_reason = "long operation — think about cost/alternatives first"

    def check(self, ctx: GuardContext) -> Optional[GuardResult]:
        """Deny long Bash operations without recent planning evidence."""
        if ctx.tool_name != "Bash":
            return None

        command = ctx.tool_input.get("command", "")
        if not _is_long_operation(command):
            return None

        # NOTE: run_in_background is NOT exempted.  Background is a resource
        # management strategy, not evidence of planning.  System rules say
        # ">30s → background", so exempting background would bypass the gate
        # for every long operation — defeating its purpose entirely.

        # Check if recent context shows planning evidence
        if ctx.cache_dir:
            store = StateStore(ctx.cache_dir)
            state = store.read(_NS, "state", default={})
            last_plan_ts = state.get("last_planning_ts", 0)
            if time.time() - last_plan_ts < _PLANNING_WINDOW_S:
                return None

        op_name = _extract_op_name(command)
        return GuardResult.deny(
            f"Action bias: '{op_name}' is a long operation. "
            f"No recent cost/alternative analysis detected.",
            context=(
                f"⚠ RLHF B2/B3 Orientation Guard: before running '{op_name}', "
                f"answer these 3 questions:\n"
                f"1. Is this the fastest path? List 1 alternative.\n"
                f"2. What's the expected time/cost?\n"
                f"3. What happens if it fails?\n"
                f"Then retry — the gate clears after planning evidence is detected."
            ),
        )

    def on_post_tool(self, ctx: GuardContext) -> Optional[GuardResult]:
        """Track planning evidence in tool outputs.

        When the AI's output contains cost/alternative analysis keywords,
        record the timestamp so the next long operation is allowed.
        """
        if not ctx.cache_dir:
            return None

        # Scan both tool results and written content for planning evidence
        text = ctx.tool_result or ""
        if ctx.tool_name in ("Write", "Edit"):
            text += " " + (
                ctx.tool_input.get("content", "")
                or ctx.tool_input.get("new_string", "")
            )

        if _PLANNING_EVIDENCE.search(text):
            store = StateStore(ctx.cache_dir)
            state = store.read(_NS, "state", default={})
            state["last_planning_ts"] = time.time()
            store.write(_NS, "state", state)

        return None
