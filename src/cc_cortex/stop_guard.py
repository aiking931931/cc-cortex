"""cc_cortex.stop_guard — Detect premature or suspicious session stops.

@module stop_guard
@responsibility Classify session stop events (clean/question/continuation/pending/unknown)
    and detect premature stops for on-stop hook integration.
@dependencies none (stdlib only)
@exports StopResult, classify_stop, on_stop

Analyzes the last assistant message to classify how the session ended:
  - clean: completion keywords present, no pending work
  - question: assistant asked a question (may be waiting for user)
  - continuation: assistant expressed intent to continue (premature stop)
  - pending: unfinished tasks detected without completion signal
  - unknown: insufficient data to classify

Usage:
    from cc_cortex.stop_guard import classify_stop
    result = classify_stop(hook_data)
    if result.premature:
        print(result.warning)
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field


@dataclass
class StopResult:
    """Classification result for a session stop event."""

    category: str  # clean | question | continuation | pending | unknown
    premature: bool  # True if stop looks unintentional
    signals: list[str] = field(default_factory=list)  # matched keywords/patterns
    warning: str = ""  # human-readable warning (empty if clean)


def _get_stop_config() -> dict:
    """Load stop_guard config from Config singleton."""
    try:
        from cc_cortex.core.config import get_config

        return get_config().raw("stop_guard", {})
    except Exception:
        return {}


def _extract_last_assistant_text(hook_data: dict) -> str:
    """Extract the last assistant message text from hook_data.

    Claude Code Stop hook provides ``stop_hook_active`` and the transcript
    in ``messages`` (list of {role, content}).  We grab the last assistant turn.
    """
    messages = hook_data.get("messages", [])
    # Walk backwards to find last assistant message
    for msg in reversed(messages):
        role = msg.get("role", "")
        if role != "assistant":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    parts.append(part.get("text", ""))
            return "\n".join(parts)
    return ""


def _match_any(text: str, patterns: list[str]) -> list[str]:
    """Return all patterns found in *text* (case-insensitive)."""
    lower = text.lower()
    return [p for p in patterns if p.lower() in lower]


def _msg_has_tool_use(content: object) -> bool:
    """Check if a message content list contains tool_use blocks."""
    if not isinstance(content, list):
        return False
    return any(
        isinstance(p, dict) and p.get("type") == "tool_use"
        for p in content
    )


def _has_pending_tool_use(hook_data: dict) -> bool:
    """Check if recent assistant messages contain tool calls (mid-task).

    Heuristic: scan last 8 messages for tool_use blocks.
    """
    messages = hook_data.get("messages", [])
    recent = messages[-8:] if len(messages) > 8 else messages
    return any(
        msg.get("role") == "assistant"
        and _msg_has_tool_use(msg.get("content", ""))
        for msg in recent
    )


def _is_declaration(text: str) -> bool:
    """Detect short declarative output — not a question, not a completion.

    Pattern: AI outputs a brief explanation/comment then stops, breaking
    autonomous flow. E.g. explaining a hook warning in 1-2 sentences.

    Criteria:
    - Short (< 300 chars of meaningful text, ignoring whitespace)
    - Does NOT end with a question mark
    - Does NOT contain interactive phrases (要繼續嗎, should I, 請問)
    """
    stripped = text.strip()
    if not stripped:
        return False

    # Too long to be a stray declaration
    if len(stripped) > 300:
        return False

    # Ends with question mark = genuine question, not declaration
    last_line = stripped.rstrip().split("\n")[-1].strip()
    if last_line.endswith("?") or last_line.endswith("？"):
        return False

    # Contains interactive intent = genuine stop point
    interactive = ["要繼續嗎", "should i", "shall i", "would you", "是否", "需要我"]
    lower = stripped.lower()
    if any(p in lower for p in interactive):
        return False

    return True


def _load_patterns(cfg: dict) -> dict[str, list[str]]:
    """Load keyword/pattern lists from config or i18n fallback."""
    from cc_cortex.i18n import patterns as i18n_patterns

    return {
        "completion": cfg.get("completion_keywords", [])
        or i18n_patterns("stop_guard.completion"),
        "question": cfg.get("question_keywords", [])
        or i18n_patterns("stop_guard.question"),
        "continuation": cfg.get("continuation_patterns", [])
        or i18n_patterns("stop_guard.continuation"),
        "pending": cfg.get("pending_patterns", [])
        or i18n_patterns("stop_guard.pending"),
    }


def _resolve_category(
    text: str,
    matches: dict[str, list[str]],
    hook_data: dict,
) -> StopResult:
    """Resolve stop category from matched patterns.

    Priority: continuation > declaration > pending > question > clean.
    """
    c = matches["completion"]
    q = matches["question"]
    cont = matches["continuation"]
    pend = matches["pending"]

    if cont and not c:
        top = ", ".join(cont[:3])
        return StopResult(
            category="continuation",
            premature=True,
            signals=cont,
            warning=f"Stop while intended to continue: {top}",
        )

    # Declaration: short non-question mid-task output → premature.
    # Catches "explain hook warning then stop" inertia.
    # Circuit breaker in on_stop() prevents infinite loops.
    if (
        not c
        and not q
        and _is_declaration(text)
        and _has_pending_tool_use(hook_data)
    ):
        return StopResult(
            category="declaration",
            premature=True,
            signals=["short_declarative_output", "mid_task"],
            warning="Stop after declarative output while tasks"
            " pending — autonomous mode should continue",
        )

    if pend and not c:
        top = ", ".join(pend[:3])
        return StopResult(
            category="pending",
            premature=True,
            signals=pend,
            warning=f"Stop with pending tasks: {top}",
        )

    if q and not c:
        return StopResult(
            category="question",
            premature=False,
            signals=q,
            warning="",
        )

    if c:
        return StopResult(
            category="clean",
            premature=False,
            signals=c,
            warning="",
        )

    return StopResult(category="unknown", premature=False)


def classify_stop(hook_data: dict | None = None) -> StopResult:
    """Classify a Stop event and return a :class:`StopResult`.

    Args:
        hook_data: The raw hook JSON from Claude Code's Stop event.
                   If *None* or missing ``messages``, returns ``unknown``.
    """
    if not hook_data:
        return StopResult(category="unknown", premature=False)

    text = _extract_last_assistant_text(hook_data)
    if not text:
        return StopResult(category="unknown", premature=False)

    cfg = _get_stop_config()
    if not cfg.get("enabled", True):
        return StopResult(category="clean", premature=False)

    pats = _load_patterns(cfg)
    matches = {k: _match_any(text, v) for k, v in pats.items()}
    return _resolve_category(text, matches, hook_data)


# Circuit breaker: max 1 block per session to prevent infinite loops.
# File tracks session_id + timestamp of last block.
_BLOCK_STATE_PATH = os.path.join(
    os.path.expanduser("~"), ".claude", "stop_guard_block.json",
)
_BLOCK_COOLDOWN_S = 300.0  # 5 min — don't re-block same session


def _already_blocked_this_session(session_id: str) -> bool:
    """Check if we already blocked this session (circuit breaker)."""
    if not session_id or not os.path.isfile(_BLOCK_STATE_PATH):
        return False
    try:
        import json
        with open(_BLOCK_STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
        if state.get("session_id") != session_id:
            return False
        return (time.time() - state.get("ts", 0)) < _BLOCK_COOLDOWN_S
    except Exception:
        return False


def _record_block(session_id: str) -> None:
    """Record that we blocked this session."""
    try:
        import json
        os.makedirs(os.path.dirname(_BLOCK_STATE_PATH), exist_ok=True)
        with open(_BLOCK_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"session_id": session_id, "ts": time.time()}, f)
    except Exception:
        pass


def on_stop(hook_data: dict) -> str | None:
    """Hook entry point.

    Returns:
        - ``STOP_BLOCK:<reason>`` for continuation/declaration (max 1x/session)
        - warning string for pending stops (stderr only)
        - None for clean/question/unknown
    """
    result = classify_stop(hook_data)
    if not result.premature:
        return None

    session_id = hook_data.get("session_id", "")

    # continuation/declaration = premature → block (circuit breaker)
    if result.category in ("continuation", "declaration"):
        if _already_blocked_this_session(session_id):
            return result.warning  # Downgrade on 2nd occurrence
        _record_block(session_id)
        return f"STOP_BLOCK:{result.warning}"

    # pending = ambiguous → warn only (no block, could be false positive)
    return result.warning
