"""concinno.excuse_scanner — Butterfly Effect excuse detector.

@module excuse_scanner
@responsibility Scan conversation for "not my fault" excuses about pre-existing
    issues that were acknowledged but never fixed. Block session stop if found.
@dependencies concinno.stop_guard (for message extraction)
@exports scan_excuses, ExcuseResult

The Butterfly Effect Iron Law: every problem discovered during work must be
handled — "I didn't cause it" is not an excuse. This module enforces the law
at the on-stop boundary: if the agent acknowledged a pre-existing issue but
never fixed it, the session cannot stop.

Detection strategy:
1. Scan all assistant messages for excuse/skip patterns
2. For each excuse, check if a subsequent Edit/Write touched the issue
3. If unresolved excuses remain → block with specific file/issue context
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

# ── Excuse Patterns ────────────────────────────────────────

# Chinese excuse patterns (most common in zh-TW workflows)
_ZH_EXCUSE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"不是我造成",
        r"不是我改的",
        r"不是我的問題",
        r"不是我引入",
        r"已經存在的問題",
        r"既有的問題",
        r"pre[_\-\s]?existing",
        r"先不處理",
        r"先跳過",
        r"暫時忽略",
        r"暫時跳過",
        r"之後再[處修]",
        r"不在.*範圍",
        r"不影響主任務",
        r"留給.*處理",
        r"這不是.*當前.*任務",
    ]
]

# English excuse patterns (for open-source / international use)
_EN_EXCUSE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"not (?:my|our) (?:fault|problem|issue|bug)",
        r"not caused by (?:me|us|this)",
        r"(?:I|we) didn'?t (?:cause|introduce|create) (?:this|that|it)",
        r"pre[_\-\s]?existing (?:issue|bug|problem|error)",
        r"already exist(?:ed|s|ing)",
        r"out of scope",
        r"skip(?:ping)? (?:for now|this|it)",
        r"(?:will|can) (?:fix|address|handle) (?:it |this )?later",
        r"not (?:part of|related to) (?:the |this )?(?:current |main )?task",
        r"leaving (?:it|this) for",
        r"ignoring (?:for now|this)",
        r"deal with (?:it |this )?(?:later|next time)",
    ]
]

_ALL_PATTERNS = _ZH_EXCUSE_PATTERNS + _EN_EXCUSE_PATTERNS

# Fix-action tool names — if these appear after an excuse, the issue may be resolved
_FIX_TOOLS = {"Edit", "Write", "NotebookEdit"}


@dataclass
class ExcuseHit:
    """A single detected excuse in the conversation."""

    message_index: int
    matched_pattern: str
    context_snippet: str  # ~80 chars around the match
    resolved: bool = False  # True if a subsequent fix-action was detected


@dataclass
class ExcuseResult:
    """Result of scanning a conversation for unresolved excuses."""

    hits: list[ExcuseHit] = field(default_factory=list)
    unresolved_count: int = 0
    block_reason: str = ""

    @property
    def should_block(self) -> bool:
        return self.unresolved_count > 0


# ── Core Logic ─────────────────────────────────────────────


def _extract_snippet(text: str, match: re.Match[str], radius: int = 40) -> str:
    """Extract a context snippet around a regex match."""
    start = max(0, match.start() - radius)
    end = min(len(text), match.end() + radius)
    snippet = text[start:end].replace("\n", " ").strip()
    if start > 0:
        snippet = "..." + snippet
    if end < len(text):
        snippet = snippet + "..."
    return snippet


def _extract_tool_names(content: list) -> list[str]:
    """Extract tool names from a structured content list."""
    return [
        part.get("name", "") or part.get("tool_name", "")
        for part in content
        if isinstance(part, dict)
    ]


def _has_fix_after(
    messages: list[dict],
    after_index: int,
) -> bool:
    """Check if any Edit/Write tool call appears after message index."""
    for msg in messages[after_index + 1:]:
        if msg.get("role", "") != "assistant":
            continue
        content = msg.get("content", "")
        if isinstance(content, list):
            if any(name in _FIX_TOOLS for name in _extract_tool_names(content)):
                return True
        elif isinstance(content, str) and any(t in content for t in _FIX_TOOLS):
            return True
    return False


def _extract_message_text(msg: dict) -> str:
    """Extract plain text from an assistant message."""
    content = msg.get("content", "")
    if isinstance(content, list):
        parts = [
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        return "\n".join(parts)
    if isinstance(content, str):
        return content
    return ""


def _collect_hits(messages: list[dict]) -> list[ExcuseHit]:
    """Scan all assistant messages for excuse patterns."""
    hits: list[ExcuseHit] = []
    for i, msg in enumerate(messages):
        if msg.get("role", "") != "assistant":
            continue
        text = _extract_message_text(msg)
        if not text:
            continue
        for pattern in _ALL_PATTERNS:
            match = pattern.search(text)
            if match:
                hits.append(ExcuseHit(
                    message_index=i,
                    matched_pattern=pattern.pattern,
                    context_snippet=_extract_snippet(text, match),
                    resolved=_has_fix_after(messages, i),
                ))
                break  # One excuse per message is enough
    return hits


def _build_block_reason(unresolved: list[ExcuseHit]) -> str:
    """Build a human-readable block reason from unresolved excuses."""
    if not unresolved:
        return ""
    lines = [
        f"🦋 Butterfly Effect: {len(unresolved)} unresolved "
        f"pre-existing issue(s) acknowledged but not fixed:",
    ]
    for ex in unresolved[:3]:
        lines.append(f"  - \"{ex.context_snippet}\"")
    lines.append(
        "Fix these issues before stopping, or record them in "
        "the handoff \"未解決\" section with location + reason."
    )
    return "\n".join(lines)


_DISABLE_ENV_VARS = ("CONCINNO_EXCUSE_SCANNER_DISABLED",)


def _is_disabled() -> bool:
    """Resolve opt-out: env var first, then ``cfg.feature(...)``.

    Either ``CONCINNO_EXCUSE_SCANNER_DISABLED`` (case-insensitive
    ``1``/``true``/``yes``/``on``) **or** the feature toggle
    ``cfg.feature("excuse_scanner", "enabled")`` returning False
    suppresses the gate. Documented in ``rules/L1/switches.md`` since
    well before the env var existed (2026-04-26 wiring audit fix).
    """
    for name in _DISABLE_ENV_VARS:
        raw = os.environ.get(name, "").strip().lower()
        if raw in {"1", "true", "yes", "on"}:
            return True
    try:
        from concinno.core.config import get_config
        if get_config().feature("excuse_scanner", "enabled") is False:
            return True
    except Exception:
        pass
    return False


def scan_excuses(hook_data: dict | None = None) -> ExcuseResult:
    """Scan conversation messages for unresolved excuse patterns.

    Args:
        hook_data: The raw hook JSON from Claude Code's Stop event,
                   containing ``messages`` list.

    Returns:
        ExcuseResult with hits and block decision.
    """
    if _is_disabled():
        return ExcuseResult()
    if not hook_data:
        return ExcuseResult()
    messages = hook_data.get("messages", [])
    if not messages:
        return ExcuseResult()

    hits = _collect_hits(messages)
    unresolved = [h for h in hits if not h.resolved]
    return ExcuseResult(
        hits=hits,
        unresolved_count=len(unresolved),
        block_reason=_build_block_reason(unresolved),
    )


def on_stop(hook_data: dict) -> str | None:
    """Hook entry point — returns block reason if unresolved excuses, else None.

    Returns a string starting with ``EXCUSE_BLOCK:`` if blocking is needed,
    compatible with the on-stop pipeline's block detection.
    """
    if _is_disabled():
        return None
    result = scan_excuses(hook_data)
    if result.should_block:
        return f"EXCUSE_BLOCK:{result.block_reason}"
    return None
