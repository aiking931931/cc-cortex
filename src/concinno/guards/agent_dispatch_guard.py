"""concinno.guards.agent_dispatch_guard — Token-aware agent dispatch strategy.

COGNITIVE guard: inject subagent delegation advice based on current
token usage zone. Long conversations suffer attention decay; subagents
get clean context windows. This guard tells the model WHEN to delegate.

Also injects a lightweight "result quality check" reminder when a
subagent returns (PostToolUse on Agent tool).

@module guards/agent_dispatch_guard
@category COGNITIVE
@priority 315
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

# Token thresholds (absolute) — aligned with token_zone.py
_GREEN_CAP = 80_000    # <80K: main agent handles everything
_YELLOW_CAP = 150_000  # 80-150K: delegate concrete tasks
# >150K: delegate almost everything, main = commander only

_STRATEGY_GREEN = (
    "Token zone GREEN (<80K) — main agent handles directly. "
    "Only use subagents for genuinely parallel/independent work."
)

_STRATEGY_YELLOW = (
    "Token zone YELLOW (80-150K) — context pressure building. "
    "Delegate concrete implementation tasks to subagents "
    "(they get clean 200K context). Main agent: decisions + coordination only. "
    "Write detailed prompts — subagents can't see this conversation."
)

_STRATEGY_RED = (
    "Token zone RED (>150K) — attention decay active. Full mode: stay in session. "
    "Delegate ALL implementation to subagents. Main agent = commander: "
    "decide what → write detailed prompts → dispatch → verify results. "
    "Do NOT read/edit files directly — your context is unreliable. "
    "Subagent prompt MUST include all context (file paths, requirements, constraints) "
    "because it cannot see this conversation."
)

_RESULT_CHECK = (
    "Subagent returned. Before trusting the result: "
    "1) Did it actually complete the task (not just say it did)? "
    "2) Any files it claimed to create — do they exist? "
    "3) Does the result contradict what you know? "
    "If suspicious, verify with Read/Grep before proceeding."
)


def _get_input_tokens() -> int:
    """Read current input token count from zone file."""
    zone_path = os.path.join(
        os.path.expanduser("~"), ".claude", ".token_zone.json",
    )
    try:
        with open(zone_path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("input_tokens", 0)
    except Exception:
        return 0


class AgentDispatchGuard(BaseGuard):
    """COGNITIVE: inject token-aware subagent dispatch strategy.

    PreToolUse on Agent tool: advise delegation level.
    PostToolUse on Agent tool: remind to verify result.
    """

    name = "agent_dispatch"
    category = GuardCategory.COGNITIVE
    priority = 315
    hook_event = "PreToolUse"

    def check(self, ctx: GuardContext) -> GuardResult:
        # PreToolUse: inject strategy when spawning agents
        if ctx.hook_event == "PreToolUse" and ctx.tool_name == "Agent":
            tokens = _get_input_tokens()
            if tokens > _YELLOW_CAP:
                return GuardResult.allow(context=_STRATEGY_RED)
            if tokens > _GREEN_CAP:
                return GuardResult.allow(context=_STRATEGY_YELLOW)
            return GuardResult.allow(context=_STRATEGY_GREEN)

        # PostToolUse: result quality check reminder
        if ctx.hook_event == "PostToolUse" and ctx.tool_name == "Agent":
            return GuardResult.allow(context=_RESULT_CHECK)

        return GuardResult.allow()
