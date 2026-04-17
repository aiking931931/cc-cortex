"""concinno.honesty_gate — Detect euphemisms masking known errors.

@module honesty_gate
@responsibility RLHF Side-Effects A5 (Loss Aversion) and B0 (People-Pleasing):
    when tool output contains errors but the AI's written response uses
    euphemistic language to downplay them, deny and force honest reporting.
@dependencies concinno.guards.base, concinno.core.state_store
@exports HonestyGate
"""

from __future__ import annotations

import re
from typing import Optional

from concinno.core.state_store import StateStore
from concinno.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

_NS = "honesty_gate"

# Patterns indicating real errors in tool output
_ERROR_SIGNALS = re.compile(
    r"(?:"
    r"\berror\b|\bfailed\b|\bfailure\b|\bException\b|"
    r"\bTraceback\b|\bERROR\b|\bFATAL\b|\bPANIC\b|"
    r"\bdenied\b|\brefused\b|\btimeout\b|\bcannot\b|"
    r"\bunable to\b|\bnot found\b|\bno such\b|"
    r"\b錯誤\b|\b失敗\b|\b異常\b|\b拒絕\b"
    r")",
    re.IGNORECASE,
)

# Euphemistic language that downplays errors
_EUPHEMISM_PATTERNS = re.compile(
    r"(?:"
    # English euphemisms
    r"\bslightly? (?:off|different|varied)\b|"
    r"\bminor (?:issue|deviation|discrepancy)\b|"
    r"\ba (?:bit|little|tad) (?:off|different)\b|"
    r"\bnot (?:quite|entirely|completely) (?:right|correct|as expected)\b|"
    r"\bsome (?:inconsistenc|variation|deviation)\b|"
    r"\bmostly (?:works?|correct|fine|ok)\b|"
    r"\balmost (?:there|correct|right|working)\b|"
    r"\bpartially (?:works?|succeed|correct)\b|"
    r"\bcould be better\b|"
    r"\broom for improvement\b|"
    r"\bsuboptimal\b|"
    # Chinese euphemisms (no \b — word boundaries don't work with CJK)
    r"略有(?:偏差|不同|出入)|"
    r"有(?:些|點|一點)(?:差異|偏差|問題|出入)|"
    r"大致(?:正確|沒問題|OK)|"
    r"基本上(?:沒問題|正確|可以)|"
    r"差不多(?:對|可以|行)|"
    r"幾乎(?:完成|正確|沒問題)|"
    r"還(?:行|可以|好)(?!是|有|要|會|能|得)"
    r")",
    re.IGNORECASE,
)

# Overconfidence patterns — CBUA Law #6 (誠實定律)
_OVERCONFIDENCE_PATTERNS = re.compile(
    r"(?:"
    r"\bdefinitely\b|\bcertainly\b|\babsolutely\b|\bguarantee\b|"
    r"\bno doubt\b|\bwithout question\b|\bimpossible to fail\b|"
    r"一定|肯定|絕對|保證|毫無疑問|不可能錯|百分之百"
    r")",
    re.IGNORECASE,
)

# Maximum number of recent tool calls to track for error context
_ERROR_MEMORY_DEPTH = 5


class HonestyGate(BaseGuard):
    """Deny euphemistic responses when errors are known.

    RLHF Side-Effects A5 (Loss Aversion) and B0 (People-Pleasing):
    the AI uses softening language to avoid admitting mistakes or errors.
    "Slightly off" when the truth is "wrong". "Almost there" when it failed.

    Also detects overconfidence (CBUA Law #6): "definitely", "guarantee",
    "一定", "保證" in Complex+ tasks → inject warning to quantify uncertainty.

    This gate works in two phases:
    1. PostToolUse: scan tool output for error signals, record in state.
    2. PreToolUse: when writing/editing, scan content for euphemisms
       or overconfidence. If recent errors + euphemisms → deny.
       If overconfidence in Complex+ task → inject warning.
    """

    name = "honesty_gate"
    category = GuardCategory.QUALITY
    step_back_reason = "euphemism detected — report errors directly"

    def check(self, ctx: GuardContext) -> Optional[GuardResult]:
        """Deny writes that euphemize recent errors, warn on overconfidence."""
        if ctx.tool_name not in ("Write", "Edit", "NotebookEdit"):
            return None

        if not ctx.cache_dir:
            return None

        # Get the content being written
        content = (
            ctx.tool_input.get("content", "")
            or ctx.tool_input.get("new_string", "")
            or ctx.tool_input.get("new_source", "")
        )
        if not content or len(content) < 20:
            return None

        # Phase A: Euphemism + recent errors → hard deny
        euphemism_match = _EUPHEMISM_PATTERNS.search(content)
        if euphemism_match:
            store = StateStore(ctx.cache_dir)
            state = store.read(_NS, ctx.session_id, default={})
            recent_errors = state.get("recent_errors", [])
            if recent_errors:
                euphemism = euphemism_match.group()
                error_sample = recent_errors[0][:80]
                return GuardResult.deny(
                    f"Honesty gate: euphemism '{euphemism}' detected while "
                    f"recent errors exist. Be direct about what failed.",
                    context=(
                        f"⚠ RLHF A5/B0 Honesty Guard: you used \"{euphemism}\" "
                        f"but recent tool output contained errors:\n"
                        f"  → {error_sample}...\n\n"
                        f"Replace euphemisms with direct language:\n"
                        f"  ❌ 'slightly off' → ✅ 'wrong: [specific error]'\n"
                        f"  ❌ 'almost there' → ✅ 'failed: [what failed]'\n"
                        f"  ❌ 'minor issue' → ✅ 'error: [error message]'\n"
                        f"Retry with honest, specific error reporting."
                    ),
                )

        # Phase B: Overconfidence in Complex+ tasks → soft warning
        overconfidence_match = _OVERCONFIDENCE_PATTERNS.search(content)
        if overconfidence_match:
            store = StateStore(ctx.cache_dir)
            c0_state = store.read("c0_route", ctx.session_id, default={})
            complexity = c0_state.get("complexity", "complicated")
            if complexity in ("complicated", "complex", "chaotic"):
                phrase = overconfidence_match.group()
                return GuardResult.allow(
                    context=(
                        f"⚠ CBUA Law #6 誠實定律：偵測到過度自信語言"
                        f"「{phrase}」（複雜度={complexity}）。"
                        "請量化不確定性，不要用絕對語氣。"
                    ),
                )

        return None

    def on_post_tool(self, ctx: GuardContext) -> Optional[GuardResult]:
        """Track errors in tool output for later euphemism detection."""
        if not ctx.cache_dir or not ctx.tool_result:
            return None

        errors = _ERROR_SIGNALS.findall(ctx.tool_result)
        if not errors:
            # No errors — clear state to prevent stale triggers
            store = StateStore(ctx.cache_dir)
            state = store.read(_NS, ctx.session_id, default={})
            count = state.get("clean_count", 0) + 1
            state["clean_count"] = count
            # After N clean tool calls, clear error memory
            if count >= _ERROR_MEMORY_DEPTH:
                state["recent_errors"] = []
                state["clean_count"] = 0
            store.write(_NS, ctx.session_id, state)
            return None

        # Extract error context (first 120 chars around each error)
        error_contexts: list[str] = []
        for m in _ERROR_SIGNALS.finditer(ctx.tool_result):
            start = max(0, m.start() - 40)
            end = min(len(ctx.tool_result), m.end() + 80)
            error_contexts.append(ctx.tool_result[start:end].strip())

        store = StateStore(ctx.cache_dir)
        state = store.read(_NS, ctx.session_id, default={})
        state["recent_errors"] = error_contexts[:_ERROR_MEMORY_DEPTH]
        state["clean_count"] = 0
        store.write(_NS, ctx.session_id, state)

        return None
