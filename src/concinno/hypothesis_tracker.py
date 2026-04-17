"""concinno.hypothesis_tracker — Track failed approaches to prevent loops.

@module hypothesis_tracker
@responsibility Record attempted approaches and their outcomes, inject
    context about past failures to prevent repeating the same strategy.
    Simplified Reflexion (NeurIPS 2023) for single-agent use.
@dependencies concinno.guards.base, concinno.core.state_store
@exports HypothesisTrackerGuard

Design: PostToolUse records failed attempts. PreToolUse injects
"already tried X, Y — try something different" context.
Never denies — purely additive knowledge injection.
"""

from __future__ import annotations

import time
from typing import Optional

from concinno.core.state_store import StateStore
from concinno.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult
from concinno.i18n import msg as i18n_msg

_NS = "hypothesis_tracker"
_MAX_HISTORY = 10  # keep last N failed approaches


def _approach_signature(tool_name: str, tool_input: dict) -> str:
    """Generate a short signature for the current approach."""
    path = tool_input.get("file_path", "") or tool_input.get("command", "")
    # Normalize to just filename + tool
    if "/" in path or "\\" in path:
        path = path.replace("\\", "/").split("/")[-1]
    raw = f"{tool_name}:{path}"
    return raw[:80]


def _is_failure(tool_result: str) -> bool:
    """Heuristic: does the tool result look like a failure?"""
    if not tool_result:
        return False
    lower = tool_result.lower()
    fail_signals = (
        "error", "traceback", "failed", "not found",
        "permission denied", "no such file", "command not found",
        "syntaxerror", "typeerror", "import error",
        "exit code 1", "exit code 2",
    )
    return any(s in lower for s in fail_signals)


def record_attempt(
    cache_dir: str,
    tool_name: str,
    tool_input: dict,
    tool_result: str,
) -> None:
    """Record a failed approach in the tracker."""
    if not cache_dir or not _is_failure(tool_result):
        return

    store = StateStore(cache_dir)
    state = store.read(_NS, "state", default={})
    history: list[dict] = state.get("failed_approaches", [])

    sig = _approach_signature(tool_name, tool_input)

    # Dedup by signature
    if any(h["sig"] == sig for h in history):
        return

    history.append({
        "sig": sig,
        "tool": tool_name,
        "ts": time.time(),
        "error_hint": tool_result[:120],
    })

    # Keep bounded
    if len(history) > _MAX_HISTORY:
        history = history[-_MAX_HISTORY:]

    state["failed_approaches"] = history
    store.write(_NS, "state", state)


def get_failed_context(cache_dir: str) -> str:
    """Build context string from failed approaches."""
    if not cache_dir:
        return ""

    store = StateStore(cache_dir)
    state = store.read(_NS, "state", default={})
    history: list[dict] = state.get("failed_approaches", [])

    if not history:
        return ""

    lines = [i18n_msg("hypothesis_tracker.failed_header")]
    for h in history[-5:]:  # show last 5
        lines.append(f"  - {h['sig']}: {h.get('error_hint', '')[:60]}")

    lines.append(i18n_msg("hypothesis_tracker.try_different"))
    return "\n".join(lines)


class HypothesisTrackerGuard(BaseGuard):
    """Track failed approaches and inject avoidance context.

    PostToolUse: records failures.
    PreToolUse: injects "already tried X" context on write tools.
    Never denies.
    """

    name = "hypothesis_tracker"
    category = GuardCategory.COGNITIVE
    step_back_reason = ""  # no deny

    def check(self, ctx: GuardContext) -> Optional[GuardResult]:
        """Inject context about previously failed approaches on write tools.

        Args:
            ctx: Guard context with tool_name and cache_dir.

        Returns:
            GuardResult.allow with failure history as context, or None.
        """
        if not ctx.cache_dir:
            return None

        # Only inject on write tools (when AI is about to act)
        if ctx.tool_name not in ("Edit", "Write", "Bash"):
            return None

        context = get_failed_context(ctx.cache_dir)
        if not context:
            return None

        return GuardResult.allow(context=context)

    def on_post_tool(self, ctx: GuardContext) -> Optional[GuardResult]:
        """Record failed tool attempts to prevent repeating the same strategy.

        Args:
            ctx: Guard context with tool_name, tool_input, tool_result, cache_dir.

        Returns:
            Always None (recording only, no action).
        """
        record_attempt(
            ctx.cache_dir,
            ctx.tool_name,
            ctx.tool_input,
            ctx.tool_result,
        )
        return None
