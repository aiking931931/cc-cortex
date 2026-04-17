"""concinno.initial_intent_probe — Probe user's root purpose on first write.

@module initial_intent_probe
@responsibility CBUA C1 初衷探索: for Complicated+ tasks, inject a cognitive
    prompt asking the AI to consider the user's ROOT purpose (not just the
    literal task) before the first Write/Edit in a session. Prevents RLHF
    people-pleasing by reminding the AI to challenge the question itself.
@dependencies concinno.guards.base, concinno.core.state_store
@exports InitialIntentProbe

Cognitive injection guard (ALLOW only, never deny). Fires once per session
on the first write-tool call for Complicated+ tasks.
"""

from __future__ import annotations

from typing import Optional

from concinno.constants import WRITE_TOOLS_EXT
from concinno.core.state_store import StateStore
from concinno.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

_NS = "intent_probe"

# Complexity levels that trigger the probe (Complicated and above)
_PROBE_COMPLEXITIES = frozenset({"complicated", "complex", "chaotic"})

# Complexity levels that get the extra "不迎合" reminder
_HONESTY_COMPLEXITIES = frozenset({"complex", "chaotic"})


class InitialIntentProbe(BaseGuard):
    """CBUA C1 初衷探索: probe user's root purpose, not just task.

    PreToolUse on first Write/Edit in session:
    1. Check C0 complexity — Simple tasks skip
    2. Check if session has had any "probe" interaction (stored in StateStore)
    3. If Complicated+ and no probe done -> inject context asking AI to consider:
       - "What is the user's ROOT purpose behind this request?"
       - "Is the user asking the right question?"
       - "Is there a better approach the user hasn't considered?"
    4. Set probe_done flag after first injection

    Category: COGNITIVE (inject, never deny)
    Frequency: Once per session (not per tool call)
    """

    name = "initial_intent_probe"
    category = GuardCategory.COGNITIVE

    def check(self, ctx: GuardContext) -> Optional[GuardResult]:
        """Inject root-purpose probe on first write for Complicated+ tasks."""
        # Only trigger on write tools (including Bash as it can write)
        if ctx.tool_name not in WRITE_TOOLS_EXT and ctx.tool_name != "Bash":
            return None

        if not ctx.cache_dir:
            return None

        store = StateStore(ctx.cache_dir)
        state = store.read(_NS, ctx.session_id, default={})

        # Already probed this session — skip
        if state.get("probe_done"):
            return None

        # Check complexity from C0 router
        c0_state = store.read("c0_route", ctx.session_id, default={})
        complexity = c0_state.get("complexity", "complicated").lower()

        # Simple tasks skip
        if complexity not in _PROBE_COMPLEXITIES:
            state["probe_done"] = True
            state["skip_reason"] = "simple_task"
            store.write(_NS, ctx.session_id, state)
            return None

        # Mark probe as done (once per session)
        state["probe_done"] = True
        state["complexity"] = complexity
        store.write(_NS, ctx.session_id, state)

        # Build injection context
        probe_text = (
            "🔍 CBUA C1 初衷探索（首次寫入前）\n"
            "在動手之前，先想清楚：\n"
            "  1. 用戶的 ROOT PURPOSE 是什麼？（不只是字面任務）\n"
            "  2. 用戶問的是對的問題嗎？\n"
            "  3. 有沒有用戶沒想到的更好做法？\n"
        )

        # Complex+ tasks get the anti-RLHF honesty reminder
        if complexity in _HONESTY_COMPLEXITIES:
            probe_text += (
                "\n⚠ 不迎合提醒：\n"
                "  - 有一說一。如果用戶的方向有問題，必須指出，不要迎合。\n"
                "  - 問錯問題也是答案：矯正問題比回答問題更有價值。"
            )

        return GuardResult.allow(context=probe_text)

    def on_post_tool(self, ctx: GuardContext) -> Optional[GuardResult]:
        """No post-tool logic needed."""
        return None
