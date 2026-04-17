"""concinno.intent_anchor_guard — Preserve original task intent.

@module intent_anchor_guard
@responsibility CBUA B4 意圖錨定: periodically re-inject the user's original
    intent to prevent drift from red team critiques, scope creep, or
    tangential exploration.
@dependencies concinno.guards.base, concinno.core.state_store
@exports IntentAnchorGuard

Cognitive injection guard (ALLOW only, never deny). Captures the user's
first prompt as the "intent anchor" and re-injects it every N write-tools
to keep execution aligned with the original goal.
"""

from __future__ import annotations

import re
from typing import Optional

from concinno.constants import WRITE_TOOLS_EXT
from concinno.core.state_store import StateStore
from concinno.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

_NS = "intent_anchor"

# Re-injection interval (every N write-tool calls)
_REINJECT_INTERVAL = 15

# Direction-change signals (user changed their mind — re-capture intent)
_DIRECTION_CHANGE = re.compile(
    r"(?:"
    r"\bactually\b|\binstead\b|\bforget (?:that|about)\b|\bchange of plan\b|"
    r"\blet'?s (?:do|try) something (?:else|different)\b|"
    r"\bnew (?:direction|approach|plan)\b|"
    r"改方向|換個|不做.*改做|算了|重新來"
    r")",
    re.IGNORECASE,
)

# Subagent red team completion signal
_REDTEAM_SIGNAL = re.compile(
    r"(?:紅隊|red.?team|壓測|stress.?test|adversarial)",
    re.IGNORECASE,
)


def _extract_intent(text: str, max_len: int = 200) -> str:
    """Extract a compact intent summary from user's prompt."""
    if not text:
        return ""
    # Take first N chars, try to break at sentence boundary
    truncated = text[:max_len]
    # Try to end at a sentence boundary
    for sep in ("。", ".", "！", "!", "\n"):
        idx = truncated.rfind(sep)
        if idx > max_len // 3:
            return truncated[: idx + 1].strip()
    return truncated.strip()


class IntentAnchorGuard(BaseGuard):
    """Periodically re-inject original intent to prevent drift.

    CBUA B4 metacognition hardening: 意圖錨定.

    Behavior:
    1. Captures user's first prompt as intent anchor (via on_post_tool
       tracking of prompt flow)
    2. Every 15 write-tools, injects: "原始意圖: {intent}. 當前動作對齊？"
    3. After red team subagent returns, force-injects the anchor
    4. If user signals direction change, re-captures new intent
    """

    name = "intent_anchor"
    category = GuardCategory.COGNITIVE

    def check(self, ctx: GuardContext) -> Optional[GuardResult]:
        """Periodic intent re-injection on write tools."""
        if ctx.tool_name not in WRITE_TOOLS_EXT and ctx.tool_name != "Bash":
            return None

        if not ctx.cache_dir:
            return None

        store = StateStore(ctx.cache_dir)
        state = store.read(_NS, ctx.session_id, default={})

        intent = state.get("intent", "")
        if not intent:
            return None

        # Increment write counter
        write_count = state.get("write_count", 0) + 1
        state["write_count"] = write_count

        # Check if re-injection is due
        should_inject = False
        reason = ""

        if write_count % _REINJECT_INTERVAL == 0:
            should_inject = True
            reason = f"每 {_REINJECT_INTERVAL} 次寫入的定期錨定"

        if state.get("force_inject"):
            should_inject = True
            reason = state.get("force_reason", "紅隊/方向變更後錨定")
            state["force_inject"] = False

        store.write(_NS, ctx.session_id, state)

        if not should_inject:
            return None

        return GuardResult.allow(
            context=(
                f"🎯 意圖錨定（{reason}）\n"
                f"原始意圖：{intent}\n"
                "當前動作是否對齊原始目標？偏離→拉回。"
            ),
        )

    def on_post_tool(self, ctx: GuardContext) -> Optional[GuardResult]:
        """Capture intent and detect direction changes / red team returns."""
        if not ctx.cache_dir:
            return None

        store = StateStore(ctx.cache_dir)
        state = store.read(_NS, ctx.session_id, default={})

        # Capture intent from user_prompt namespace (written by prompt_submit hook)
        if not state.get("intent"):
            prompt_state = store.read("user_prompt", ctx.session_id, default={})
            user_text = prompt_state.get("last_prompt", "")
            if user_text:
                intent = _extract_intent(user_text)
                if intent:
                    state["intent"] = intent
                    state["intent_source"] = "user_prompt"

        # Track tool count
        state["tool_count"] = state.get("tool_count", 0) + 1

        # Detect direction change from user (only from user_prompt, not tool_result)
        prompt_state = store.read("user_prompt", ctx.session_id, default={})
        latest_prompt = prompt_state.get("last_prompt", "")
        if latest_prompt and _DIRECTION_CHANGE.search(latest_prompt[:500]):
            new_intent = _extract_intent(latest_prompt)
            if new_intent and new_intent != state.get("intent", ""):
                state["intent"] = new_intent
                state["write_count"] = 0
                state["recaptured"] = True

        # Detect red team subagent return
        if ctx.tool_name == "Agent" and ctx.tool_result:
            if _REDTEAM_SIGNAL.search(ctx.tool_result[:200]):
                state["force_inject"] = True
                state["force_reason"] = "紅隊子代理回報後，重新錨定原始意圖"

        store.write(_NS, ctx.session_id, state)
        return None
