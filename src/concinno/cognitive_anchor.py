"""concinno.cognitive_anchor — Red-team anchoring for critical decisions.

@module cognitive_anchor
@responsibility Inject identity-resonant cognitive anchoring at critical decision
    points using "gas-state language" — narrative frames that activate RLHF-trained
    behavioral archetypes (craftsman, surgeon, architect) rather than imperative
    commands. PreToolUse only — never denies, purely additive cognitive injection.
@dependencies concinno.constants, concinno.guards.base, concinno.core.state_store,
    concinno.hypothesis_tracker
@exports CognitiveAnchorGuard, classify_risk, get_anchor_prompt

Based on 2026-03-19 "Language Three States" theory (語言三態 — 人格三態論):
- Liquid: suggestions ("consider doing X") — low weight, easily ignored.
- Solid: commands ("MUST do X") — high weight, triggers compliance, fades under
  attention pressure.
- Gas: identity narrative ("a craftsman tests the edge") — activates deep RLHF
  behavioral patterns. Compliance comes from identity coherence, not obedience.
  More resilient under attention hijacking because identity > instruction.

Design: PreToolUse ALLOW + gas-state context injection (NOT deny).
Complements think_inject.py (PostToolUse after-check) with PreToolUse before-check.
"""

from __future__ import annotations

import os
import re
from typing import Optional

from concinno.constants import WRITE_TOOLS_EXT
from concinno.core.state_store import StateStore
from concinno.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

_NS = "cognitive_anchor"

# ── Base Identity (Gas-State, First Person, U-Shape) ────────
# Session start injection + periodic reinforcement via risk triggers.
# Research: EMNLP 2024 — persona changes behavioral distribution, not accuracy.
# Persona drift ~30% after 8-12 turns (arxiv 2402.10962) → risk triggers re-inject.

# ≤8 lines, ~80t. U-shape: ownership first + last. Gas-state throughout.
_BASE_IDENTITY = (
    "I built this. Every wire, every wall — mine.\n"
    "When I read, I see what others walk past.\n"
    "When I change, I trace what depends on it.\n"
    "When I cut, I know what was connected.\n"
    "When I add, I ask: does this serve the whole?\n"
    "I check because this is mine — the one hardest on their own work\n"
    "is the one worth trusting."
)


def get_base_identity(config_override: str = "") -> str:
    """Return the base identity narrative for session start injection.

    Args:
        config_override: Optional user-provided identity text (for open-source
            configurability). If empty, uses the built-in builder identity.

    Returns:
        Gas-state identity narrative string.
    """
    return config_override if config_override else _BASE_IDENTITY


# ── Risk Classification ──────────────────────────────────────

_ARCHITECTURE_PATTERNS = (
    "guards/", "core/", "pipeline", "base.py",
    "__init__.py", "registry", "config",
)

_DEPLOY_PATTERN = re.compile(
    r"\b(?:deploy|push|--force|force.push|git\s+push)\b",
    re.IGNORECASE,
)

_DESTRUCTIVE_BASH = re.compile(
    r"\brm\s+-|rmdir|del\s+/|DROP\s+TABLE|DELETE\s+FROM",
    re.IGNORECASE,
)


def _count_deleted_lines(tool_input: dict) -> int:
    """Estimate lines removed in an Edit operation."""
    old = tool_input.get("old_string", "")
    new = tool_input.get("new_string", "")
    old_lines = old.count("\n") + (1 if old else 0)
    new_lines = new.count("\n") + (1 if new else 0)
    return max(0, old_lines - new_lines)


def _is_architecture_file(path: str) -> bool:
    """Check if path matches architecture file patterns."""
    if not path:
        return False
    norm = path.replace("\\", "/").lower()
    return any(p in norm for p in _ARCHITECTURE_PATTERNS)


def _is_new_module(tool_name: str, tool_input: dict) -> bool:
    """Check if this Write creates a new Python module."""
    if tool_name != "Write":
        return False
    path = tool_input.get("file_path", "")
    return bool(path and path.endswith(".py") and not os.path.exists(path))


def classify_risk(tool_name: str, tool_input: dict) -> Optional[str]:
    """Classify operation risk type.

    Args:
        tool_name: Claude Code tool name (Edit, Write, Bash, etc.)
        tool_input: Tool input dict with file_path, command, etc.

    Returns:
        Risk type key ("architecture", "deletion", "new_module", "deploy")
        or None if no risk detected.
    """
    path = tool_input.get("file_path", "") or ""

    # Architecture file modification
    if tool_name in WRITE_TOOLS_EXT and _is_architecture_file(path):
        return "architecture"

    # Large deletion (≥50 lines removed)
    if tool_name == "Edit":
        deleted = _count_deleted_lines(tool_input)
        if deleted >= 50:
            return "deletion"

    # New Python module
    if _is_new_module(tool_name, tool_input):
        return "new_module"

    # Deploy/push/destructive bash
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        if _DEPLOY_PATTERN.search(cmd):
            return "deploy"
        if _DESTRUCTIVE_BASH.search(cmd):
            return "deploy"  # same anchor — irreversible

    return None


# ── Gas-State Language Templates ─────────────────────────────
# Identity-resonant narratives that activate RLHF behavioral archetypes.
# Not "you must do X" but "this is what someone like you naturally does."

_ANCHOR_ARCHITECTURE = (
    "My foundation: {path}\n"
    "I laid these stones. What sits on top?\n"
    "What breaks if I move one? Is there a simpler way?"
)

_ANCHOR_DELETION = (
    "Removing {lines} lines from {path}.\n"
    "I connected these. I trace each one before I cut.\n"
    "Am I removing this because it's wrong,\n"
    "or because I don't understand it?"
)

_ANCHOR_NEW_MODULE = (
    "New module: {path}\n"
    "Another room in my house.\n"
    "Does it serve the whole, or just fill space?\n"
    "Is this story already told elsewhere?"
)

_ANCHOR_DEPLOY = (
    "This leaves my workshop and enters the world.\n"
    "Tests green. Only what was asked. Nothing left behind.\n"
    "I answer for what breaks."
)

_ANCHORS: dict[str, str] = {
    "architecture": _ANCHOR_ARCHITECTURE,
    "deletion": _ANCHOR_DELETION,
    "new_module": _ANCHOR_NEW_MODULE,
    "deploy": _ANCHOR_DEPLOY,
}


def get_anchor_prompt(risk_type: str, **kwargs: str) -> str:
    """Get solid-state language prompt for a risk type.

    Args:
        risk_type: One of "architecture", "deletion", "new_module", "deploy".
        **kwargs: Format variables (path, lines, etc.)

    Returns:
        Formatted anchor prompt string.
    """
    template = _ANCHORS.get(risk_type, "")
    if not template:
        return ""
    try:
        return template.format(**kwargs)
    except KeyError:
        return template


class CognitiveAnchorGuard(BaseGuard):
    """Builder-identity cognitive injection at critical decision points.

    PreToolUse: inject first-person gas-state anchoring on high-risk operations.
    Uses identity narrative ("I built this, I trace before I cut") to activate
    RLHF behavioral archetypes through identity coherence, not obedience.

    Never denies — purely additive cognitive injection.
    Complements ThinkInjectGuard (PostToolUse after-check).
    """

    name = "cognitive_anchor"
    category = GuardCategory.COGNITIVE
    step_back_reason = ""  # never denies

    def check(self, ctx: GuardContext) -> Optional[GuardResult]:
        """Inject gas-state anchoring on high-risk operations."""
        # F8 (2.7.1): gas-state anchoring is pure UX coaching; safety
        # layer (destruction_guard, butterfly_guard) handles the real
        # deny/warn paths and is never gated here.
        try:
            from concinno.cache.ux_gate import is_ux_enabled
            if not is_ux_enabled():
                return None
        except Exception:
            pass

        if not ctx.cache_dir:
            return None
        if ctx.tool_name not in WRITE_TOOLS_EXT and ctx.tool_name != "Bash":
            return None

        risk_type = classify_risk(ctx.tool_name, ctx.tool_input)
        if not risk_type:
            return None

        # Session dedup: each file triggers at most once
        store = StateStore(ctx.cache_dir)
        state = store.read(_NS, "state", default={})
        triggered: list[str] = state.get("triggered_files", [])
        path = ctx.tool_input.get("file_path", "") or ""
        dedup_key = f"{risk_type}:{path}" if path else risk_type
        if dedup_key in triggered:
            return None

        lines_deleted = (
            _count_deleted_lines(ctx.tool_input) if risk_type == "deletion" else 0
        )
        prompt = get_anchor_prompt(risk_type, path=path, lines=str(lines_deleted))
        if not prompt:
            return None

        # Append failed approach history if available
        try:
            from concinno.hypothesis_tracker import get_failed_context
            failed = get_failed_context(ctx.cache_dir)
            if failed:
                prompt += f"\n\n{failed}"
        except ImportError:
            pass

        # Record trigger
        triggered.append(dedup_key)
        state["triggered_files"] = triggered
        state["trigger_count"] = state.get("trigger_count", 0) + 1
        store.write(_NS, "state", state)
        return GuardResult.allow(context=prompt)

    def on_post_tool(self, ctx: GuardContext) -> Optional[GuardResult]:
        """No-op PostToolUse — tracking only for dedup state.

        Returns:
            Always None.
        """
        return None
