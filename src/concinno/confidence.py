"""concinno.confidence — Confidence management: gate + calibration record.

Merged from confidence_gate + confidence_record (module consolidation Phase 1).

@module confidence
@responsibility
    1. Detect uncertainty markers in AI output + deny irreversible operations
       (ConfidenceGate — PreToolUse DENY).
    2. Overwrite-style confidence calibration tracking
       (read_confidence, update_confidence, confidence_context).
@dependencies concinno.constants, concinno.guards.base, concinno.core.state_store
@exports ConfidenceGate, detect_uncertainty, is_irreversible,
    read_confidence, update_confidence, confidence_context
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Optional

from concinno.constants import READ_TOOLS
from concinno.core.state_store import StateStore
from concinno.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

# ══════════════════════════════════════════════════════════════════
# Part 1: Confidence Gate (deny uncertain + irreversible operations)
# ══════════════════════════════════════════════════════════════════

_NS = "confidence_gate"

# ── Irreversible tool patterns ───────────────────────────────

_IRREVERSIBLE_BASH = re.compile(
    r"\brm\s+-|git\s+push|git\s+reset\s+--hard|DROP\s+TABLE|"
    r"DELETE\s+FROM|docker\s+rm|kill\s+-9|rmdir|del\s+/",
    re.IGNORECASE,
)


def _build_uncertainty_patterns() -> list[re.Pattern[str]]:
    """Build compiled uncertainty patterns from all active locales."""
    from concinno.i18n import patterns as i18n_patterns

    raw = i18n_patterns("uncertainty")
    compiled: list[re.Pattern[str]] = []
    for p in raw:
        try:
            compiled.append(re.compile(p, re.IGNORECASE))
        except re.error:
            compiled.append(re.compile(re.escape(p), re.IGNORECASE))
    return compiled


def detect_uncertainty(text: str) -> list[str]:
    """Find uncertainty markers in text. Returns matched phrases."""
    if not text:
        return []
    matches: list[str] = []
    for pat in _build_uncertainty_patterns():
        for m in pat.finditer(text):
            matches.append(m.group())
    return matches


def is_irreversible(tool_name: str, tool_input: dict) -> bool:
    """Check if tool call is irreversible/destructive."""
    if tool_name in READ_TOOLS:
        return False

    # Edit is reversible (can undo), Write to existing is overwrite
    if tool_name == "Edit":
        return False

    # Bash with destructive commands
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        return bool(_IRREVERSIBLE_BASH.search(cmd))

    # Write to new file — generally safe but check context
    # For now, Write is considered low-risk (file can be deleted)
    return False


def _build_deny_message(markers: list[str]) -> str:
    """Build deny message with verification guidance."""
    from concinno.i18n import msg

    marker_str = ", ".join(markers[:3])
    return msg("confidence_gate.deny", markers=marker_str)


def _get_verify_context() -> str:
    """Get verification guidance from i18n."""
    from concinno.i18n import msg

    return msg("confidence_gate.verify_guide")


class ConfidenceGate(BaseGuard):
    """Deny irreversible operations when uncertainty is detected.

    Tracks recent AI output uncertainty via PostToolUse,
    gates irreversible PreToolUse calls when uncertainty is high.
    """

    name = "confidence_gate"
    category = GuardCategory.QUALITY
    step_back_reason = "uncertain — verify before proceeding"

    def __init__(self, decay_calls: int = 5):
        """Args:
            decay_calls: uncertainty signal decays after N tool calls.
        """
        self._decay_calls = decay_calls

    def check(self, ctx: GuardContext) -> Optional[GuardResult]:
        """Deny irreversible operations when recent uncertainty is detected."""
        if not ctx.cache_dir:
            return None

        if not is_irreversible(ctx.tool_name, ctx.tool_input):
            return None

        store = StateStore(ctx.cache_dir)
        state = store.read(_NS, "state", default={})

        markers = state.get("uncertainty_markers", [])
        calls_since = state.get("calls_since_detection", 0)

        if not markers or calls_since >= self._decay_calls:
            return None

        # Increment call counter
        state["calls_since_detection"] = calls_since + 1
        store.write(_NS, "state", state)

        return GuardResult.deny(
            _build_deny_message(markers),
            context=_get_verify_context(),
        )

    def on_post_tool(self, ctx: GuardContext) -> Optional[GuardResult]:
        """Scan tool results for uncertainty markers and record state."""
        if not ctx.cache_dir or not ctx.tool_result:
            return None

        store = StateStore(ctx.cache_dir)
        state = store.read(_NS, "state", default={})

        markers = detect_uncertainty(ctx.tool_result)
        if markers:
            state["uncertainty_markers"] = markers
            state["calls_since_detection"] = 0
            store.write(_NS, "state", state)

        return None


# ══════════════════════════════════════════════════════════════════
# Part 2: Confidence Record (overwrite-style calibration)
# ══════════════════════════════════════════════════════════════════


def _record_path(cache_dir: str) -> str:
    return os.path.join(cache_dir, "confidence_record.json")


def read_confidence(cache_dir: str) -> dict:
    """Read current confidence record. Returns empty dict if none."""
    p = _record_path(cache_dir)
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def update_confidence(
    cache_dir: str,
    domain: str,
    success: bool,
    error_pattern: str = "",
    note: str = "",
) -> dict:
    """Update confidence record (overwrite, not append).

    Confidence auto-adjusts:
    - Each success: confidence += 0.1 (cap 0.95)
    - Each failure: confidence -= 0.15 (floor 0.10)
    - Same error_pattern as last: confidence -= 0.25 (B0 loop penalty)
    """
    rec = read_confidence(cache_dir)

    # Reset if domain changed
    if rec.get("domain") != domain:
        rec = {"domain": domain, "confidence": 0.80, "failures": 0, "successes": 0}

    conf = rec.get("confidence", 0.80)
    last_pattern = rec.get("last_error_pattern", "")

    if success:
        rec["successes"] = rec.get("successes", 0) + 1
        conf = min(0.95, conf + 0.10)
        rec["last_outcome"] = "success"
    else:
        rec["failures"] = rec.get("failures", 0) + 1
        if error_pattern and error_pattern == last_pattern:
            conf = max(0.10, conf - 0.25)  # B0 loop penalty
            rec["last_error_pattern"] = error_pattern
            rec["calibration"] = (
                f"同類錯誤重複。B0 patch loop。信心降至 {conf:.0%}。{note}"
            )
        else:
            conf = max(0.10, conf - 0.15)
            rec["last_error_pattern"] = error_pattern
            rec["calibration"] = (
                f"新類型失敗。{note}" if note else f"失敗次數 {rec['failures']}"
            )
        rec["last_outcome"] = "fail"

    rec["confidence"] = round(conf, 2)
    rec["updated"] = datetime.now(timezone.utc).isoformat()

    # Atomic write
    p = _record_path(cache_dir)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)

    return rec


def confidence_context(cache_dir: str) -> str:
    """Generate compact context string for LLM injection."""
    rec = read_confidence(cache_dir)
    if not rec:
        return ""

    domain = rec.get("domain", "?")
    conf = rec.get("confidence", 0.80)
    fails = rec.get("failures", 0)
    succs = rec.get("successes", 0)
    cal = rec.get("calibration", "")

    if fails == 0:
        return ""

    return (
        f"\U0001f4ca \u4fe1\u5fc3\u6821\u6e96\uff08{domain}\uff09\uff1a{conf:.0%} "
        f"| {succs}\u2705 {fails}\u274c "
        f"| {cal}"
    )
