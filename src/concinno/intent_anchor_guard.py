"""concinno.intent_anchor_guard — Preserve original task intent.

@module intent_anchor_guard
@responsibility CBUA B4 意圖錨定: periodically re-inject the user's original
    intent to prevent drift from red team critiques, scope creep, or
    tangential exploration.
@dependencies concinno.guards.base, concinno.core.state_store,
    concinno.intent_anchor (v2.10+ dataclass)
@exports IntentAnchorGuard

Cognitive injection guard (ALLOW only, never deny). Captures the user's
first prompt as the "intent anchor" and re-injects it every N write-tools
to keep execution aligned with the original goal.

v2.10 (2026-04-25): re-injection now uses the structured
:class:`concinno.intent_anchor.IntentAnchor` dataclass so the message
includes ``done_spec`` and ``constraints`` when populated (set by the
prompt-submit Stage -1 hook). State is fully back-compatible with v2.9
— missing keys render as blank lines, the legacy ``intent`` key is still
read on the way in.
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
        # F8 (2.7.1): intent-anchor coaching is pure UX; ship default
        # off for anonymous PyPI users. on_post_tool (state capture)
        # stays unguarded so the invariant "if UX flips on mid-session
        # we still have intent to anchor" holds.
        try:
            from concinno.cache.ux_gate import is_ux_enabled
            if not is_ux_enabled():
                return None
        except Exception:
            pass

        if ctx.tool_name not in WRITE_TOOLS_EXT and ctx.tool_name != "Bash":
            return None

        if not ctx.cache_dir:
            return None

        store = StateStore(ctx.cache_dir)
        state = store.read(_NS, ctx.session_id, default={})

        # v2.10: load structured anchor (back-compat with v2.9 'intent' key)
        from concinno.intent_anchor import IntentAnchor, render_anchor_block

        anchor = IntentAnchor.from_dict(state)
        if anchor.is_empty():
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
            context=render_anchor_block(
                anchor,
                title=f"🎯 意圖錨定（{reason}）",
            ),
        )

    def on_post_tool(self, ctx: GuardContext) -> Optional[GuardResult]:
        """Capture intent and detect direction changes / red team returns."""
        if not ctx.cache_dir:
            return None

        store = StateStore(ctx.cache_dir)
        state = store.read(_NS, ctx.session_id, default={})

        # Capture intent from user_prompt namespace (written by prompt_submit hook)
        if not state.get("intent") and not state.get("summary"):
            prompt_state = store.read("user_prompt", ctx.session_id, default={})
            user_text = prompt_state.get("last_prompt", "")
            if user_text:
                # v2.10: use structured extractor — populates done_spec /
                # constraints heuristically when the prompt-submit Stage -1
                # hook didn't run (e.g. older harness, downgrade path).
                from concinno.intent_anchor import extract_anchor

                anchor = extract_anchor(user_text)
                if not anchor.is_empty():
                    state.update(anchor.to_dict())
                    state["intent"] = anchor.summary  # v2.9 back-compat alias
                    state["intent_source"] = "user_prompt"

        # Track tool count
        state["tool_count"] = state.get("tool_count", 0) + 1

        # Detect direction change from user (only from user_prompt, not tool_result)
        prompt_state = store.read("user_prompt", ctx.session_id, default={})
        latest_prompt = prompt_state.get("last_prompt", "")
        if latest_prompt and _DIRECTION_CHANGE.search(latest_prompt[:500]):
            from concinno.intent_anchor import extract_anchor

            new_anchor = extract_anchor(latest_prompt)
            if (
                not new_anchor.is_empty()
                and new_anchor.summary != state.get("intent", "")
            ):
                # v2.10: refresh all structured fields, not just the summary,
                # so a direction change also reshapes done_spec / constraints
                # rather than carrying stale ones from the original prompt.
                state.update(new_anchor.to_dict())
                state["intent"] = new_anchor.summary  # v2.9 alias
                state["write_count"] = 0
                state["recaptured"] = True

        # Detect red team subagent return
        if ctx.tool_name == "Agent" and ctx.tool_result:
            if _REDTEAM_SIGNAL.search(ctx.tool_result[:200]):
                state["force_inject"] = True
                state["force_reason"] = "紅隊子代理回報後，重新錨定原始意圖"

        store.write(_NS, ctx.session_id, state)
        return None
