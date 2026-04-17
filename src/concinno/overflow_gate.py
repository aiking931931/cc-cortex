"""concinno.overflow_gate — Block side-quests when attention is exhausted.

@module overflow_gate
@responsibility RLHF Side-Effect B1 (Attention Overflow): when token zone is
    YELLOW+ and the AI spawns non-critical agents, deny to prevent guessing
    on side-questions under cognitive overload.
@dependencies concinno.guards.base, concinno.token_zone, concinno.core.state_store
@exports OverflowGate
"""

from __future__ import annotations

import time
from typing import Optional

from concinno.core.state_store import StateStore
from concinno.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult
from concinno.token_zone import Zone, read_zone_file

_NS = "overflow_gate"

# Agent descriptions matching these patterns are considered critical
# (handoff, error fix) and should not be blocked.
_CRITICAL_KEYWORDS = frozenset({
    "handoff", "交接", "error", "fix", "urgent", "critical",
    "deploy", "部署", "commit", "push",
})


def _is_critical_agent(tool_input: dict) -> bool:
    """Check if agent spawn is for a critical task."""
    desc = (tool_input.get("description", "") or "").lower()
    prompt = (tool_input.get("prompt", "") or "").lower()[:200]
    combined = desc + " " + prompt
    return any(kw in combined for kw in _CRITICAL_KEYWORDS)


def _get_zone() -> Zone:
    """Read current token zone. Returns YELLOW if unavailable (fail-closed).

    Fail-closed: if zone file is missing/stale, assume pressure exists.
    This prevents the gate from becoming useless when token_monitor crashes.
    """
    data = read_zone_file(max_age_s=120)
    if data and "zone_level" in data:
        try:
            return Zone(data["zone_level"])
        except (ValueError, KeyError):
            pass
    return Zone.YELLOW  # fail-closed: no data = assume pressure


class OverflowGate(BaseGuard):
    """Block non-critical Agent spawns when attention is exhausted.

    RLHF Side-Effect B1: Under cognitive overload (token zone YELLOW+),
    the AI guesses on side-questions instead of acknowledging capacity limits.
    This gate prevents spawning exploratory agents when focus should be
    on the main task.

    Also tracks rapid successive Agent spawns (burst detection) regardless
    of zone — spawning >N agents in M seconds suggests unfocused scattershot.
    """

    name = "overflow_gate"
    category = GuardCategory.QUALITY
    step_back_reason = "attention overflow — focus on main task first"

    def __init__(
        self,
        *,
        burst_max: int = 4,
        burst_window_s: int = 30,
    ):
        self._burst_max = burst_max
        self._burst_window_s = burst_window_s

    def check(self, ctx: GuardContext) -> Optional[GuardResult]:
        """Deny Agent spawns under attention overflow conditions.

        Two triggers:
        1. Zone YELLOW+ and agent is non-critical → deny
        2. Burst: >burst_max agents in burst_window_s seconds → deny
        """
        if ctx.tool_name != "Agent":
            return None

        # Critical agents always pass (handoff, error fix, deploy)
        if _is_critical_agent(ctx.tool_input):
            return None

        zone = _get_zone()

        # Trigger 1: Zone-based overflow
        if zone.value >= Zone.YELLOW.value:
            zone_names = {1: "YELLOW", 2: "ORANGE", 3: "RED"}
            zone_label = zone_names.get(zone.value, zone.name)
            return GuardResult.deny(
                f"Attention overflow (zone {zone_label}): "
                f"non-critical Agent spawn blocked. "
                f"Focus on main task or write handoff.",
                context=(
                    "⚠ RLHF B1 Overflow Guard: token budget is strained. "
                    "Spawning exploratory agents now risks guessing on "
                    "side-questions. Finish current work first, or mark "
                    "side-questions as 'deferred' in handoff."
                ),
            )

        # Trigger 2: Burst detection (too many agents too fast)
        if ctx.cache_dir:
            store = StateStore(ctx.cache_dir)
            state = store.read(_NS, "state", default={})
            now = time.time()
            spawns: list[float] = state.get("recent_spawns", [])
            # Prune old entries
            spawns = [ts for ts in spawns if now - ts < self._burst_window_s]
            if len(spawns) >= self._burst_max:
                return GuardResult.deny(
                    f"Agent burst: {len(spawns)} spawns in "
                    f"{self._burst_window_s}s (max {self._burst_max}). "
                    f"Slow down — unfocused scattershot wastes tokens.",
                    context=(
                        "⚠ RLHF B1 Overflow Guard: rapid agent spawning "
                        "detected. This pattern indicates attention overflow — "
                        "the AI is delegating instead of thinking. "
                        "Pause, identify the ONE most important sub-task, "
                        "then spawn a single focused agent."
                    ),
                )
            # Record this spawn attempt (even if allowed)
            spawns.append(now)
            state["recent_spawns"] = spawns
            store.write(_NS, "state", state)

        return None
