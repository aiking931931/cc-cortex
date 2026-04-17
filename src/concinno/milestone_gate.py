"""concinno.milestone_gate — SOP milestone specific-guidance injection.

@module milestone_gate
@responsibility RLHF D3 (SOP Drift): after N tool calls, inject SPECIFIC
    guidance (not vague reminders) about what to do next. Frequency adapts
    to token zone (GREEN=20, YELLOW=10, ORANGE+=5).
@dependencies concinno.guards.base, concinno.core.state_store
@exports MilestoneGate

v2 (2026-03-26): Rewritten from vague "are you on track?" to specific
    guidance with task name + concrete next action. Validated by experiment:
    specific guidance works even under attention hijacking; vague reminders
    are negative ROI (soft-warning law v2).
"""

from __future__ import annotations

import os
from typing import Optional

from concinno.core.state_store import StateStore
from concinno.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

_NS = "milestone_gate"

# ── Frequency by token zone (tighter when resources scarce) ──
_FREQ_BY_ZONE = {
    "GREEN": 20,
    "YELLOW": 10,
    "ORANGE": 5,
    "RED": 5,
}
_DEFAULT_FREQ = 20


def _get_token_zone() -> str:
    """Read current token zone from environment."""
    return os.environ.get("CC_TOKEN_ZONE", "GREEN").upper()


def _build_specific_guidance(ctx: GuardContext) -> str:
    """Build specific, actionable guidance — not vague reminders.

    Soft-warning law v2: specific guidance = positive ROI.
    Format: ⚠ [what's happening] → [what to do] (≤3 lines, executable)
    """
    task_name = os.environ.get("CC_SESSION_TASK", "")

    lines: list[str] = []

    if task_name:
        lines.append(f"⚠ D3 漂移防護：當前任務是「{task_name}」")
        lines.append(f"  → 下一步必須直接服務於「{task_name}」，否則停下來寫交接")
    else:
        lines.append("⚠ D3 漂移防護：無法偵測當前任務名稱")
        lines.append("  → 用一句話確認你在做什麼，然後繼續")

    lines.append("  → 如果已偏離：停止當前動作，回到最後一個 ✅ 的步驟")

    return "\n".join(lines)


class MilestoneGate(BaseGuard):
    """Inject specific SOP guidance every N steps to prevent D3 drift.

    RLHF D3 (SOP Drift): as context grows, the AI gradually drops
    procedural rules. This gate injects periodic SPECIFIC guidance
    (not vague "are you on track?" which is negative ROI).

    Experiment (2026-03-26): specific guidance with task name + concrete
    action works even under attention hijacking (5-file read + 5 questions).
    Vague reminders are ignored.

    Frequency adapts to token zone (equilibrium dynamic balance):
    - GREEN (plenty of room): every 20 steps
    - YELLOW (getting tight): every 10 steps
    - ORANGE/RED (critical): every 5 steps
    """

    name = "milestone_gate"
    category = GuardCategory.COGNITIVE

    def check(self, ctx: GuardContext) -> Optional[GuardResult]:
        """Inject specific guidance at milestone intervals."""
        if not ctx.cache_dir:
            return None

        store = StateStore(ctx.cache_dir)
        state = store.read(_NS, ctx.session_id, default={"step": 0})

        step = state.get("step", 0) + 1
        state["step"] = step

        zone = _get_token_zone()
        freq = _FREQ_BY_ZONE.get(zone, _DEFAULT_FREQ)

        if step % freq != 0:
            store.write(_NS, ctx.session_id, state)
            return None

        # Milestone hit — inject specific guidance
        guidance = _build_specific_guidance(ctx)
        store.write(_NS, ctx.session_id, state)

        return GuardResult.allow(context=guidance)
