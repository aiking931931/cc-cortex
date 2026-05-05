"""concinno.guards.action_phase_signal_guard — Behavioural OODA / PDCA / ReAct.

@module action_phase_signal_guard
@responsibility Close the OODA / PDCA / ReAct ANCHOR-ONLY gaps. The CBUA
    enum (``cbua_ux.CbuaCode.A0``..``A4``) named the action phases but no
    pipeline guard *detects* whether the model actually moves through them.
    This guard counts tool-call cadence using purely behavioural signals
    (no text regex, per MEMORY #27 anti-stuffing rule) and emits an
    advisory phase-distribution summary every ``summary_interval`` calls.
@dependencies concinno.guards.base, concinno.core.state_store
@exports ActionPhaseSignalGuard, ActionPhase, classify_phase

Phase classification (behavioural only)
---------------------------------------

* **A0 Orient** — Read / Glob / Grep before any Edit/Write/Bash this session
  (≤ 3 reads). Detects "look before you leap" hygiene at session start.
* **A1 Plan** — TodoWrite invocation. Concrete plan artefact.
* **A2 Execute (ReAct)** — Edit / Write / NotebookEdit. The reason-act
  loop's "act" half. ReAct compliance is detected over time as a R→E→V
  cadence ratio rather than per-call.
* **A3 Verify** — Bash whose leading segment is ``pytest`` / ``ruff`` /
  ``tsc`` / ``mypy`` / ``go test`` / ``cargo test`` / ``npm test``.
* **A4 Adapt** — Bash whose leading segment is ``git commit`` (sediment
  signal, not generic Bash). PDCA Check-Adjust handoff.
* **A5 Guard** — implicit; always-on guards (destruction / butterfly /
  premise / sentinel) cover this. Not counted here to avoid double-count.

Why no text regex
-----------------
MEMORY #27 ("CBUA hook 術語堆疊") proved that scanning for human-readable
phase markers in tool args is gameable: models learn to stuff
"OODA Orient: ..." / "PDCA Plan: ..." headers into Bash command bodies
without actually doing the phase. Behavioural counters cannot be faked
by keyword stuffing because the *kind* of tool used is the signal.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Optional

from concinno.core.state_store import StateStore
from concinno.guards.base import (
    BaseGuard,
    GuardCategory,
    GuardContext,
    GuardResult,
)

_NS = "action_phase"

# Default ZIQ-tunable param (mirrored in ziq_autotune_registry.py).
_DEFAULT_SUMMARY_INTERVAL = 10


class ActionPhase(str, Enum):
    """Behavioural action-phase tags."""

    A0_ORIENT = "a0_orient"
    A1_PLAN = "a1_plan"
    A2_EXECUTE = "a2_execute"
    A3_VERIFY = "a3_verify"
    A4_ADAPT = "a4_adapt"
    UNCLASSIFIED = "unclassified"


# Verify-leading commands (segment-leading, not anywhere in the command).
# Mirrors the spirit of cbua_pipeline_guard's _DELIVERY_VERB pattern but
# scoped to verification verbs. Kept narrow on purpose.
_VERIFY_VERB = re.compile(
    r"^\s*(?:"
    r"pytest"
    r"|ruff(?!\s+format)"
    r"|tsc(?:\s|$)"
    r"|mypy"
    r"|go\s+test"
    r"|cargo\s+test"
    r"|npm\s+test"
    r"|jest"
    r"|vitest"
    r")",
    re.IGNORECASE,
)

# Adapt = git commit only. ``git push`` is delivery (handled elsewhere),
# not the PDCA Check-Adjust signal.
_ADAPT_VERB = re.compile(r"^\s*git\s+commit(?!\s+--dry-run)", re.IGNORECASE)

# Re-use cbua_pipeline_guard's separator constant locally to keep this
# guard self-contained (no inter-guard import cycle).
_SHELL_SEPARATORS = re.compile(r"&&|\|\||;|\||\n")


def _segments_match(cmd: str, pattern: re.Pattern) -> bool:
    if not cmd:
        return False
    for seg in _SHELL_SEPARATORS.split(cmd):
        if pattern.match(seg):
            return True
    return False


def classify_phase(ctx: GuardContext, state: dict) -> ActionPhase:
    """Classify a tool call into one of the action phases.

    Args:
        ctx: Guard context for the current PostToolUse tick.
        state: Current state dict — read-only here (mutation happens in
            ``on_post_tool``). The session-start ``early_calls`` count is
            consulted to decide between A0 and A2 for Read/Glob/Grep.

    Returns:
        The detected ``ActionPhase``. ``UNCLASSIFIED`` when the call does
        not fit any phase (e.g. Agent dispatch, WebFetch, miscellaneous
        Bash that is neither verify nor adapt).
    """
    name = ctx.tool_name

    # A1: TodoWrite is the canonical plan artefact.
    if name == "TodoWrite":
        return ActionPhase.A1_PLAN

    # A2: any structural mutation. Edit / Write / NotebookEdit.
    if name in ("Edit", "Write", "NotebookEdit"):
        return ActionPhase.A2_EXECUTE

    # A3 / A4: Bash subdivides on the leading segment verb.
    if name == "Bash" and isinstance(ctx.tool_input, dict):
        cmd = str(ctx.tool_input.get("command", "") or "")
        if _segments_match(cmd, _ADAPT_VERB):
            return ActionPhase.A4_ADAPT
        if _segments_match(cmd, _VERIFY_VERB):
            return ActionPhase.A3_VERIFY
        return ActionPhase.UNCLASSIFIED

    # A0: Read / Glob / Grep, but only as long as the session is still
    # in its opening orient window (no Edit/Write/Bash yet).
    if name in ("Read", "Glob", "Grep"):
        execute_count = int(state.get("counts", {}).get(
            ActionPhase.A2_EXECUTE.value, 0,
        ))
        bash_count = int(state.get("bash_count", 0))
        if execute_count == 0 and bash_count == 0:
            orient_count = int(state.get("counts", {}).get(
                ActionPhase.A0_ORIENT.value, 0,
            ))
            if orient_count < 3:
                return ActionPhase.A0_ORIENT
        # Past the orient window: still useful for ReAct cadence but
        # not a phase signal on its own.
        return ActionPhase.UNCLASSIFIED

    return ActionPhase.UNCLASSIFIED


def _format_summary(state: dict) -> str:
    """Render the phase-distribution summary line."""
    counts = state.get("counts", {})
    parts = []
    for phase in (
        ActionPhase.A0_ORIENT,
        ActionPhase.A1_PLAN,
        ActionPhase.A2_EXECUTE,
        ActionPhase.A3_VERIFY,
        ActionPhase.A4_ADAPT,
    ):
        n = int(counts.get(phase.value, 0))
        # Tag uses the short A-code only to keep the line scannable.
        tag = phase.value.split("_")[0].upper()
        parts.append(f"{tag}={n}")
    total = int(state.get("total", 0))
    return (
        f"Action phases @ {total} calls — " + " ".join(parts)
        + ". A2 without A3/A4 = ReAct without verification."
    )


class ActionPhaseSignalGuard(BaseGuard):
    """PostToolUse: count action-phase distribution + emit periodic summary.

    Pure-advisory: never denies. Surfaces a one-line summary every
    ``summary_interval`` total tool calls so the model can self-monitor
    its action-phase distribution and notice cadence imbalances
    (e.g. "lots of A2 Execute, zero A3 Verify").
    """

    name = "action_phase_signal"
    feature_name = "action_phase_signal"
    category = GuardCategory.COGNITIVE

    def __init__(
        self,
        *,
        summary_interval: int = _DEFAULT_SUMMARY_INTERVAL,
    ) -> None:
        self._interval = max(5, min(30, int(summary_interval)))

    def check(self, ctx: GuardContext) -> Optional[GuardResult]:
        """PreToolUse: no-op (PostToolUse-driven guard)."""
        return None

    def on_post_tool(self, ctx: GuardContext) -> Optional[GuardResult]:
        if not ctx.session_id or not ctx.cache_dir:
            return None

        store = StateStore(ctx.cache_dir)

        def _mutate(state: dict) -> dict:
            counts = dict(state.get("counts", {}))
            phase = classify_phase(ctx, state)
            counts[phase.value] = int(counts.get(phase.value, 0)) + 1
            state["counts"] = counts
            state["total"] = int(state.get("total", 0)) + 1
            if ctx.tool_name == "Bash":
                state["bash_count"] = int(state.get("bash_count", 0)) + 1
            state["last_phase"] = phase.value
            return state

        new_state = store.read_modify_write(_NS, ctx.session_id, _mutate)

        total = int(new_state.get("total", 0))
        if total > 0 and total % self._interval == 0:
            # ZIQ outcome wire (4.4.0 — sub-agent K wave-2). The
            # interval controls advisory cadence: low = chatty (more
            # noise but earlier feedback), high = quiet (less noise
            # but slower self-monitoring loop). We model this as an
            # iteration outcome where succeeded=True signals "the
            # interval fired" — the FTRL learner then balances
            # firing rate against downstream phase-balance health.
            try:
                from concinno.ziq_emit_helpers import emit_iteration_outcome

                emit_iteration_outcome(
                    "action_phase.summary_interval",
                    value=int(self._interval),
                    iterations_used=int(self._interval),
                    succeeded=True,
                    source=(
                        "concinno.guards.action_phase_signal_guard."
                        "ActionPhaseSignalGuard"
                    ),
                    metadata={
                        "total_calls": total,
                        "last_phase": new_state.get("last_phase", ""),
                    },
                )
            except Exception:
                pass
            return GuardResult.allow_advisory(
                context=_format_summary(new_state),
                reason="action_phase_summary",
            )
        return None


__all__ = [
    "ActionPhase",
    "ActionPhaseSignalGuard",
    "classify_phase",
]
