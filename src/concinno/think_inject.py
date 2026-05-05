"""concinno.think_inject — Think Tool injection for high-risk operations.

@module think_inject
@responsibility Detect high-risk tool operations (multi-file edits, large
    deletions, architecture changes) and inject "use think tool" prompt
    via PostToolUse additionalContext — nudging deeper reasoning.
@dependencies concinno.constants, concinno.guards.base,
    concinno.core.state_store
@exports ThinkInjectGuard

Based on 2026-03-18 SOTA research:
- Think Tool (Anthropic 2025): dedicated tool for explicit reasoning
- Interleaved Thinking: Claude reasons between tool calls automatically
- This guard nudges Claude to use the think tool at critical junctures

Design: PostToolUse ALLOW + context injection (NOT deny).
Zero friction — purely additive reasoning prompt.
"""

from __future__ import annotations

import os
from typing import Optional

from concinno.constants import WRITE_TOOLS_EXT
from concinno.core.state_store import StateStore
from concinno.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

_NS = "think_inject"

# ── Thresholds (overridable via cc_config.json) ──────────────

DEFAULT_THRESHOLDS = {
    "files_edited_trigger": 3,      # inject after N files edited in session
    "lines_deleted_trigger": 50,    # inject when deletion is large
    "new_module_trigger": True,     # inject when creating new module
    "architecture_patterns": [      # path patterns = architecture files
        "guards/", "core/", "__init__.py",
        "pipeline", "base.py", "config",
    ],
}

# ── Injection prompts ────────────────────────────────────────

_THINK_MULTI_FILE = (
    "{count} files touched. I check the whole weave —\n"
    "one loose thread unravels the pattern."
)

_THINK_LARGE_DELETE = (
    "{lines} lines gone. I built what was connected.\n"
    "What's hanging in the air now?"
)

_THINK_NEW_MODULE = (
    "New file: {path}. A promise to maintain.\n"
    "Is this a new story, or one already on the shelf?"
)

_THINK_ARCHITECTURE = (
    "Foundation moved: {path}. I feel the ripple.\n"
    "Who downstream stands on what I just shifted?"
)

# ── Cognitive triggers (anti-pattern detection) ────────────

_THINK_BLIND_EDIT = (
    "{count} edits without reading.\n"
    "Am I fixing the symptom or understanding the disease?\n"
    "What did I miss? What should be there but isn't?"
)

_THINK_HYPOTHESIS = (
    "Something isn't working as expected.\n"
    "Three hypotheses — which has the highest CP value?\n"
    "1. Possibility  2. Verification ease  → highest first."
)

_READ_TOOLS = frozenset({"Read", "Grep", "Glob"})

_ERROR_SIGNALS = (
    "error", "failed", "traceback", "exception",
    "not found", "permission denied", "syntax error",
)


def _has_error_signal(tool_result: str) -> bool:
    """Check if tool result contains error indicators."""
    if not tool_result:
        return False
    lower = tool_result[:2000].lower()
    return any(sig in lower for sig in _ERROR_SIGNALS)


def _count_deleted_lines(tool_input: dict) -> int:
    """Estimate lines removed in an Edit operation."""
    old = tool_input.get("old_string", "")
    new = tool_input.get("new_string", "")
    old_lines = old.count("\n") + (1 if old else 0)
    new_lines = new.count("\n") + (1 if new else 0)
    return max(0, old_lines - new_lines)


def _is_new_module(tool_name: str, tool_input: dict) -> bool:
    """Check if this Write creates a new Python module."""
    if tool_name != "Write":
        return False
    path = tool_input.get("file_path", "")
    return path.endswith(".py") and not os.path.exists(path)


def _is_architecture_file(path: str, patterns: list[str]) -> bool:
    """Check if path matches architecture file patterns."""
    if not path:
        return False
    norm = path.replace("\\", "/").lower()
    return any(p in norm for p in patterns)


class ThinkInjectGuard(BaseGuard):
    """Inject think-tool prompts after high-risk operations.

    PostToolUse only — never denies, only injects context.
    """

    name = "think_inject"
    category = GuardCategory.COGNITIVE
    step_back_reason = ""  # no deny, no step-back

    def __init__(self, thresholds: Optional[dict] = None):
        self._thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}

    def check(self, ctx: GuardContext) -> Optional[GuardResult]:
        """No-op PreToolUse — this guard only acts on PostToolUse.

        Returns:
            Always None.
        """
        return None

    def _check_structural(
        self,
        ctx: GuardContext,
        state: dict,
    ) -> list[tuple[str, str]]:
        """Structural triggers: multi-file, deletion, new module, arch."""
        injections: list[tuple[str, str]] = []
        path = ctx.tool_input.get("file_path", "") or ""

        # Track edited files
        if ctx.tool_name in WRITE_TOOLS_EXT and path:
            edited = state.get("files_edited", [])
            if path not in edited:
                edited.append(path)
                state["files_edited"] = edited
            threshold = self._thresholds["files_edited_trigger"]
            if len(edited) == threshold:
                injections.append((
                    "multi_file",
                    _THINK_MULTI_FILE.format(count=len(edited)),
                ))

        # Large deletion
        if ctx.tool_name == "Edit":
            deleted = _count_deleted_lines(ctx.tool_input)
            if deleted >= self._thresholds["lines_deleted_trigger"]:
                injections.append((
                    "large_delete",
                    _THINK_LARGE_DELETE.format(lines=deleted),
                ))

        # New module
        if self._thresholds.get("new_module_trigger"):
            if _is_new_module(ctx.tool_name, ctx.tool_input):
                injections.append((
                    "new_module",
                    _THINK_NEW_MODULE.format(path=path),
                ))

        # Architecture file
        patterns = self._thresholds.get("architecture_patterns", [])
        if _is_architecture_file(path, patterns):
            injections.append((
                "architecture",
                _THINK_ARCHITECTURE.format(path=path),
            ))

        return injections

    def _check_cognitive(
        self,
        ctx: GuardContext,
        state: dict,
    ) -> list[tuple[str, str]]:
        """Cognitive triggers: blind edit, consecutive failure."""
        injections: list[tuple[str, str]] = []

        # Reset counter on read operations
        if ctx.tool_name in _READ_TOOLS:
            state["edits_since_read"] = 0
            state["consecutive_failures"] = 0
            return injections

        # Blind edit: N+ writes without reading
        if ctx.tool_name in WRITE_TOOLS_EXT:
            edits = state.get("edits_since_read", 0) + 1
            state["edits_since_read"] = edits
            trigger = self._thresholds.get("blind_edit_trigger", 3)
            if edits >= trigger:
                injections.append((
                    "blind_edit",
                    _THINK_BLIND_EDIT.format(count=edits),
                ))

        # Consecutive failure: tool result contains error signals
        if ctx.tool_result and _has_error_signal(ctx.tool_result):
            fails = state.get("consecutive_failures", 0) + 1
            state["consecutive_failures"] = fails
            trigger = self._thresholds.get("failure_trigger", 2)
            if fails >= trigger:
                injections.append(("hypothesis", _THINK_HYPOTHESIS))
        elif ctx.tool_name in WRITE_TOOLS_EXT:
            state["consecutive_failures"] = 0

        return injections

    def on_post_tool(self, ctx: GuardContext) -> Optional[GuardResult]:
        """Detect structural + cognitive anti-patterns, inject prompts.

        Returns:
            GuardResult.allow with prompt as context, or None.
        """
        # F8 (2.7.1): gate behind ux_injection. Three-layer-thinking
        # nudges and Read-before-Edit coaching are pure UX; ship default
        # for anonymous PyPI downloaders is off. Safety guards (destruction,
        # butterfly, exfil, secret-scan) run on different channels and are
        # never gated here.
        try:
            from concinno.cache.ux_gate import is_ux_enabled
            if not is_ux_enabled():
                return None
        except Exception:
            pass

        if not ctx.cache_dir:
            return None

        store = StateStore(ctx.cache_dir)
        state = store.read(_NS, "state", default={})

        # Cognitive triggers run on ALL tools (including Read)
        cog = self._check_cognitive(ctx, state)

        # Structural triggers only on write/bash tools
        struct: list[tuple[str, str]] = []
        is_write = (
            ctx.tool_name in WRITE_TOOLS_EXT
            or ctx.tool_name == "Bash"
        )
        if is_write:
            # Historic dedup namespace was ``cognitive_anchor`` — feature
            # removed in 4.6.0 KILL 10 cleanup wave; the read still works
            # against an empty default so legacy state-store entries are
            # tolerated without crashing on missing namespace.
            path = ctx.tool_input.get("file_path", "") or ""
            anchor = store.read(
                "cognitive_anchor", "state", default={},
            )
            triggered = anchor.get("triggered_files", [])
            if not any(path and path in t for t in triggered):
                struct = self._check_structural(ctx, state)

        injections = struct + cog
        if not injections:
            store.write(_NS, "state", state)
            return None

        # Deduplicate per session
        injected = state.get("injected_types", [])
        new_texts = []
        for key, text in injections:
            if key not in injected:
                injected.append(key)
                new_texts.append(text)

        state["injected_types"] = injected
        store.write(_NS, "state", state)

        if not new_texts:
            return None
        return GuardResult.allow_advisory(context="\n\n".join(new_texts))
