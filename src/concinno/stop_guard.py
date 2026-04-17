"""concinno.stop_guard — Detect premature or suspicious session stops.

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
    from concinno.stop_guard import classify_stop
    result = classify_stop(hook_data)
    if result.premature:
        print(result.warning)
"""

from __future__ import annotations

import os
import re
import sys
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
        from concinno.core.config import get_config

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
    from concinno.i18n import patterns as i18n_patterns

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


# ---------------------------------------------------------------------------
# Silent-turn-end detector (feedback_silent_turn_end_after_tool_chain)
# ---------------------------------------------------------------------------
#
# Root cause: MEMORY #15 handoff-desync + WIREDO-D violation. When a turn
# mutates state (Write / git commit / push / twine upload / ...) and ends
# with zero assistant text after the last mutation, the user has no idea
# what happened without grep-diffing the repo.
#
# Sweet spot: a pure function over the transcript that (a) classifies
# every tool_use as mutating vs inspective, (b) finds the last mutating
# index, (c) inspects anything after it for an assistant text block above
# a minimum char threshold. Warn-only (CC L6 ceiling — PostToolUse can't
# deny).

_INSPECTIVE_TOOLS = frozenset({
    "Read", "Grep", "Glob", "LS", "TodoWrite",
    "WebFetch", "WebSearch", "NotebookRead",
    "Task", "ExitPlanMode",
})

_MUTATING_TOOLS = frozenset({
    "Write", "Edit", "MultiEdit", "NotebookEdit",
})

# Bash command classification. Read-only allowlist matched as first token
# (or `git <subcommand>` as first two tokens). Anything not on the
# allowlist and fitting a mutation signature is flagged.
_BASH_INSPECTIVE_PREFIXES = (
    "ls", "cat", "find", "wc", "head", "tail", "grep", "rg",
    "echo", "pwd", "which", "whereis", "stat", "file",
    "pytest", "npm test", "yarn test", "python -m pytest",
    "python -c", "python3 -c", "node -e",
    "git status", "git log", "git diff", "git show",
    "git branch", "git config --get", "git rev-parse",
    "git ls-files", "git remote -v", "git remote show",
    "git blame", "git describe", "git stash list",
)

_BASH_MUTATING_PATTERNS = (
    # git write operations
    re.compile(r"\bgit\s+(commit|push|tag|merge|rebase|reset|"
               r"cherry-pick|revert|checkout\s+-b|branch\s+-[Dd]|"
               r"add|rm|restore\s+--staged|mv|clean|"
               r"stash\s+(push|pop|drop)|am|apply)\b"),
    # publishing / installing
    re.compile(r"\btwine\s+upload\b"),
    re.compile(r"\bnpm\s+publish\b"),
    re.compile(r"\byarn\s+publish\b"),
    re.compile(r"\bpip\s+install\b"),
    re.compile(r"\bpipx\s+install\b"),
    re.compile(r"\bpnpm\s+publish\b"),
    re.compile(r"\bcargo\s+publish\b"),
    # gh write operations
    re.compile(r"\bgh\s+(pr|issue|release)\s+(create|comment|"
               r"edit|merge|close|delete)\b"),
    # docker write operations
    re.compile(r"\bdocker\s+(build|push|run|rm|rmi|"
               r"tag|compose\s+(up|down))\b"),
    # filesystem mutation
    re.compile(r"\brm\s+(-[rRfF]+\s+)?\S"),
    re.compile(r"(^|[^\w/])mv\s+\S"),
    re.compile(r"(^|[^\w/])cp\s+\S+\s+\S"),
    re.compile(r"\bchmod\s+\S"),
    re.compile(r"\bchown\s+\S"),
    re.compile(r"\bmkdir\s+\S"),
    re.compile(r"\btouch\s+\S"),
    re.compile(r"\bln\s+-s\b"),
    # network write
    re.compile(r"\bcurl\s+(-[a-zA-Z]*X\s*|--request\s+)"
               r"(POST|PUT|DELETE|PATCH)\b"),
    re.compile(r"\bwget\s+.*--method=(POST|PUT|DELETE)\b"),
    # shell redirects (write)
    re.compile(r"(^|[^<>])>\s*\S"),
    re.compile(r">>\s*\S"),
    re.compile(r"\btee\s+(-a\s+)?\S"),
    # python/node that may mutate
    re.compile(r"\bpython[0-9]*\s+\S+\.py\b"),  # running scripts
    re.compile(r"\bnode\s+\S+\.js\b"),
)


def _classify_bash(command: str) -> str:
    """Classify a Bash command as 'mutating' or 'inspective'.

    Default bias: **mutating**. A command is inspective only if it starts
    with a known read-only prefix (and no dangerous operator is seen).
    """
    if not command:
        return "inspective"

    # Take the first meaningful segment — split on && / ; / | which could
    # chain an inspective preamble into a mutation.
    segments = re.split(r"\s*(?:&&|;|\|\|)\s*", command)
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        # Strip leading env assignments (FOO=bar cmd ...).
        while re.match(r"^[A-Z_][A-Z0-9_]*=\S+\s+", seg):
            seg = re.sub(r"^[A-Z_][A-Z0-9_]*=\S+\s+", "", seg)
        if _segment_is_mutating(seg):
            return "mutating"
    return "inspective"


def _segment_is_mutating(seg: str) -> bool:
    """Decide if a single shell segment is mutating."""
    seg_stripped = seg.strip()
    if not seg_stripped:
        return False
    # Explicit mutation patterns win outright.
    for pat in _BASH_MUTATING_PATTERNS:
        if pat.search(seg_stripped):
            return True
    # Otherwise check if it starts with an inspective prefix.
    lower = seg_stripped.lower()
    for prefix in _BASH_INSPECTIVE_PREFIXES:
        if lower == prefix or lower.startswith(prefix + " "):
            return False
    # Unknown command — default mutating (safe-failure bias: better to
    # warn spuriously than miss a real silent turn end).
    return True


def _classify_tool_call(tool_name: str, tool_input: dict) -> str:
    """Classify a single tool_use block as 'mutating' or 'inspective'."""
    if tool_name in _MUTATING_TOOLS:
        return "mutating"
    if tool_name in _INSPECTIVE_TOOLS:
        return "inspective"
    if tool_name == "Bash":
        command = ""
        if isinstance(tool_input, dict):
            command = str(tool_input.get("command", ""))
        return _classify_bash(command)
    # Unknown tools default inspective (conservative — only known
    # mutations should drive the warning, not new/unfamiliar tools).
    return "inspective"


def _iter_transcript_blocks(hook_data: dict):
    """Yield ``(msg_index, role, block)`` tuples for every content block.

    Handles both string-content and list-content messages. Non-assistant
    roles yield a synthetic single-block representation.
    """
    for i, msg in enumerate(hook_data.get("messages", [])):
        role = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, str):
            yield i, role, {"type": "text", "text": content}
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    yield i, role, block


def _find_last_mutation(hook_data: dict) -> tuple[int, str, dict] | None:
    """Return (msg_index, tool_name, tool_input) for the last mutating
    tool_use in the transcript, or None if none seen.
    """
    last: tuple[int, str, dict] | None = None
    for idx, role, block in _iter_transcript_blocks(hook_data):
        if role != "assistant":
            continue
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_use":
            continue
        tool_name = str(block.get("name", ""))
        tool_input = block.get("input", {}) if isinstance(
            block.get("input", {}), dict,
        ) else {}
        if _classify_tool_call(tool_name, tool_input) == "mutating":
            last = (idx, tool_name, tool_input)
    return last


def _final_text_after(hook_data: dict, after_idx: int) -> str:
    """Concatenate assistant text blocks appearing at or after message
    index ``after_idx``. Tool results / user replies do not count.
    """
    chunks: list[str] = []
    for idx, role, block in _iter_transcript_blocks(hook_data):
        if idx < after_idx:
            continue
        if role != "assistant":
            continue
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            chunks.append(str(block.get("text", "")))
    return "\n".join(chunks).strip()


def _silent_turn_min_chars() -> int:
    """Read minimum final-text threshold from env (default 30)."""
    raw = os.environ.get("CCC_SILENT_TURN_MIN_CHARS", "")
    try:
        v = int(raw)
        return v if v >= 0 else 30
    except (TypeError, ValueError):
        return 30


def _silent_turn_guard_enabled() -> bool:
    """Power-user escape: ``CCC_SILENT_TURN_GUARD=0`` disables entirely."""
    return os.environ.get("CCC_SILENT_TURN_GUARD", "1") != "0"


def detect_silent_turn_end(
    hook_data: dict,
) -> tuple[bool, str]:
    """Detect the 'silent turn end after mutating tool chain' antipattern.

    Returns ``(fired, message)``. ``message`` is a human-readable warn
    string when ``fired`` is True; empty otherwise.

    Rules:
        * Disabled entirely if ``CCC_SILENT_TURN_GUARD=0``.
        * Passes when no mutating tool fired in the turn.
        * Passes when final assistant text (strip) length >= threshold.
        * Otherwise fires with a message naming the last mutating tool.
    """
    if not _silent_turn_guard_enabled():
        return False, ""
    if not hook_data:
        return False, ""

    last = _find_last_mutation(hook_data)
    if last is None:
        return False, ""

    mut_idx, tool_name, tool_input = last
    final_text = _final_text_after(hook_data, mut_idx + 1)
    threshold = _silent_turn_min_chars()
    if len(final_text) >= threshold:
        return False, ""

    # Build a hint describing what mutated
    hint = tool_name
    if tool_name == "Bash":
        cmd = ""
        if isinstance(tool_input, dict):
            cmd = str(tool_input.get("command", ""))
        # First 60 chars of the command — enough to be actionable.
        hint = f"Bash: {cmd[:60]}" + ("…" if len(cmd) > 60 else "")

    msg = (
        f"[silent_turn_guard] mutating tool fired ({hint}) but final "
        f"assistant text was {len(final_text)} chars (< {threshold}). "
        f"Next turn add a WIREDO-D summary: what ran / pass-fail / "
        f"next ⬜. Disable via CCC_SILENT_TURN_GUARD=0."
    )
    return True, msg


def _emit_silent_turn_warning(hook_data: dict) -> None:
    """Side-effect: write the silent-turn warning to stderr if fired."""
    try:
        fired, msg = detect_silent_turn_end(hook_data)
    except Exception:
        return  # Fail-open: never block stop on detector crash.
    if fired and msg:
        try:
            print(msg, file=sys.stderr)
        except Exception:
            pass


def on_stop(hook_data: dict) -> str | None:
    """Hook entry point.

    Returns:
        - ``STOP_BLOCK:<reason>`` for continuation/declaration (max 1x/session)
        - warning string for pending stops (stderr only)
        - None for clean/question/unknown

    Side-effect: silent-turn-end detector writes a warn to stderr
    whenever the turn mutated state without a final text summary. The
    warning is emitted regardless of the classify_stop() category so
    that clean/question paths still catch silent-upload scenarios.
    """
    _emit_silent_turn_warning(hook_data)

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
