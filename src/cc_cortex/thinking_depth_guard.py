"""cc_cortex.thinking_depth_guard — Detect reasoning degradation.

@module thinking_depth_guard
@responsibility Monitor Read:Edit ratio as a proxy for reasoning depth.
    When ratio drops below threshold, warn that quality is degrading.
    Based on anthropics/claude-code#42796 quantitative evidence:
    - Healthy: Read:Edit ≥ 6.6:1
    - Degraded: Read:Edit ≤ 2.0:1 (33.7% edits without reading)
    - Our threshold: Read:Edit < 3:1 triggers warning
@dependencies cc_cortex.constants, cc_cortex.core.state_store,
    cc_cortex.guards.base
@exports ThinkingDepthGuard, check_read_edit_ratio

Evidence (GitHub #42796, Stella Laurenzo, 6,852 sessions):
  When thinking depth dropped 73%, Read:Edit ratio dropped from
  6.6:1 to 2.0:1. This is a leading indicator of quality collapse.
  "Extended thinking is the load-bearing structure."
"""

from __future__ import annotations

import time
from typing import Optional

from cc_cortex.constants import READ_TOOLS
from cc_cortex.core.state_store import StateStore
from cc_cortex.guards.base import (
    BaseGuard,
    GuardCategory,
    GuardContext,
    GuardResult,
)

_NS = "thinking_depth"

# Thresholds based on #42796 data, relaxed 2026-04-13 for batch-phase tolerance.
# Batch sedimentation phase (write many files after reading a few) naturally
# produces a 0.5-1.5 ratio without being a thinking-depth degradation.
# See feedback_cbua_hook_jargon_stacking.md.
READ_EDIT_WARN = 2.0  # Warn when ratio drops below this (was 3.0)
WINDOW_SIZE = 20  # Look at last N tool calls
MIN_EDITS = 3  # Need at least N edits to judge

# Stable session_id key length. All callers MUST go through `_normalize_sid`
# before reading or writing the thinking_depth namespace, otherwise full-UUID
# callers (`guards/pipeline.py` recording Read/Glob/Grep with `ctx.session_id`)
# and truncated-sid callers (`hooks/on_post_tool.py` calling
# `_resolve_session_id()[:12]` for Edit/Write) hash to different blake2b
# filenames and split the ratio window. The result is a permanent 0R/NE
# false positive on every Edit. This was the root cause of the noisy
# "Read:Edit = 0.0:1 reasoning shallow" hook spam.
_SID_KEY_LEN = 12

# Tools that count as "read" (understanding before acting)
# R1 fix: Agent removed — subagent may do writes internally
_READ_SET = frozenset(READ_TOOLS) | frozenset({"Grep", "Glob"})
# Tools that count as "edit" (modifying without understanding)
_EDIT_SET = frozenset({"Edit", "Write", "NotebookEdit"})


def _normalize_sid(session_id: str) -> str:
    """Collapse any session_id to a stable per-session store key.

    Both full UUIDs and pre-truncated ids land on the same key so the
    record window is shared regardless of which caller is writing.
    """
    return (session_id or "unknown")[:_SID_KEY_LEN]


def _record_tool(
    store: StateStore, session_id: str, tool_name: str,
) -> None:
    """Record a tool call in the sliding window."""
    sid = _normalize_sid(session_id)
    state = store.read(_NS, sid, default={"calls": []})
    calls = state.get("calls", [])
    calls.append({
        "tool": tool_name,
        "ts": time.time(),
    })
    # Keep only last WINDOW_SIZE * 3 (we look at WINDOW_SIZE but keep buffer)
    if len(calls) > WINDOW_SIZE * 3:
        calls = calls[-(WINDOW_SIZE * 3):]
    state["calls"] = calls
    store.write(_NS, sid, state)


def check_read_edit_ratio(
    store: StateStore, session_id: str, window: int = WINDOW_SIZE,
) -> tuple[float, int, int]:
    """Check Read:Edit ratio in recent tool calls.

    Returns:
        (ratio, read_count, edit_count)
        ratio = float('inf') if no edits, 0.0 if no reads.
    """
    sid = _normalize_sid(session_id)
    state = store.read(_NS, sid, default={"calls": []})
    calls = state.get("calls", [])
    recent = calls[-window:] if len(calls) >= window else calls

    reads = sum(1 for c in recent if c.get("tool") in _READ_SET)
    edits = sum(1 for c in recent if c.get("tool") in _EDIT_SET)

    if edits == 0:
        return float("inf"), reads, edits
    return reads / edits, reads, edits


class ThinkingDepthGuard(BaseGuard):
    """Warn when Read:Edit ratio indicates reasoning degradation.

    PostToolUse guard — records tool calls and checks ratio.
    Does NOT deny (quality metric, not safety gate).
    Injects warning as additionalContext when ratio drops.
    """

    name = "thinking_depth"
    category = GuardCategory.QUALITY

    def record(self, ctx: GuardContext) -> None:
        """Record a tool call without checking the ratio.

        Use this when you only need to track tool usage (e.g. non-Edit tools
        in the hook) without triggering ratio warnings.
        """
        if not ctx.cache_dir:
            return

        store = StateStore(ctx.cache_dir)
        session_id = ctx.session_id or "unknown"
        _record_tool(store, session_id, ctx.tool_name)

    def check(self, ctx: GuardContext) -> Optional[GuardResult]:
        """Record tool call and check Read:Edit ratio.

        Returns GuardResult with warning context if degraded, None if ok.
        """
        # Always record first
        self.record(ctx)

        if not ctx.cache_dir:
            return None

        store = StateStore(ctx.cache_dir)
        session_id = ctx.session_id or "unknown"

        # Only check on edit operations (that's when it matters)
        if ctx.tool_name not in _EDIT_SET:
            return None

        ratio, reads, edits = check_read_edit_ratio(store, session_id)

        # Need enough edits to judge
        if edits < MIN_EDITS:
            return None

        # CRITICAL branch removed 2026-04-13: the 1.5:1 threshold fires
        # reliably during legitimate batch-sedimentation phases, turning
        # into permanent false positives. See
        # feedback_cbua_hook_jargon_stacking.md. WARN remains as a soft
        # signal below.

        if ratio < READ_EDIT_WARN:
            return GuardResult.allow_advisory(
                reason="thinking_depth_warn",
                context=(
                    f"⚠ Read:Edit = {ratio:.1f}:1 "
                    f"({reads}R/{edits}E) — reasoning shallow. "
                    "Read more before editing."
                ),
            )

        return None
