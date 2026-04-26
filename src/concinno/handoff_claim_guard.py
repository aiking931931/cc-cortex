"""concinno.handoff_claim_guard — Detect "claimed handoff but didn't write it."

@module handoff_claim_guard
@responsibility Block session stop when the assistant claims to have written
               a handoff but no handoff file was actually modified in git.
@dependencies concinno.i18n (handoff_prefixes)
@exports on_stop

Pattern: session says "已寫入交接" / "交接已更新" / "wrote handoff" etc.
but `git diff --name-only` shows zero handoff file changes → block stop.

Circuit breaker: max 1 block per session (same as stop_guard).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from typing import Optional

# ── Claim detection patterns ─────────────────────────────────

# Matches assistant text that claims a handoff was written/updated.
# Two language groups: CJK (zh) and English.
_CLAIM_PATTERNS: list[re.Pattern[str]] = [
    # Chinese — "已寫入交接" "交接已寫" "交接已更新" "寫了交接" "更新交接" etc.
    re.compile(r"(?:已|完成).*(?:寫入?|更新|儲存).*交接", re.IGNORECASE),
    re.compile(r"交接.*(?:已|完成).*(?:寫|更新|儲存)", re.IGNORECASE),
    re.compile(r"(?:寫了|更新了|儲存了).*交接", re.IGNORECASE),
    re.compile(r"→\s*已寫入交接"),
    # English
    re.compile(r"(?:wrote|written|updated|saved)\s+(?:the\s+)?handoff", re.IGNORECASE),
    re.compile(r"handoff\s+(?:has been\s+)?(?:written|updated|saved)", re.IGNORECASE),
]

# ── Circuit breaker ──────────────────────────────────────────

_BLOCK_STATE_PATH = os.path.join(
    os.path.expanduser("~"), ".claude", "handoff_claim_block.json",
)
_BLOCK_COOLDOWN_S = 300.0


def _already_blocked(session_id: str) -> bool:
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
    try:
        import json
        os.makedirs(os.path.dirname(_BLOCK_STATE_PATH), exist_ok=True)
        with open(_BLOCK_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"session_id": session_id, "ts": time.time()}, f)
    except Exception:
        pass


# ── Core logic ───────────────────────────────────────────────

def _handoff_prefixes() -> tuple[str, ...]:
    """Load handoff file prefixes from i18n."""
    try:
        from concinno.i18n import patterns as i18n_patterns
        result = i18n_patterns("handoff_prefixes")
        return tuple(result) if result else ("交接_", "handoff_")
    except Exception:
        return ("交接_", "handoff_")


def _extract_last_assistant_text(hook_data: dict) -> str:
    """Extract last assistant message text."""
    messages = hook_data.get("messages", [])
    for msg in reversed(messages):
        if msg.get("role") != "assistant":
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


def _has_handoff_claim(text: str) -> bool:
    """Check if text contains a claim that handoff was written."""
    for pat in _CLAIM_PATTERNS:
        if pat.search(text):
            return True
    return False


def _git_changed_handoff_files(project_dir: str) -> list[str]:
    """Get handoff files changed in git (staged + unstaged + untracked)."""
    _NO_WIN = 0x08000000 if sys.platform == "win32" else 0
    prefixes = _handoff_prefixes()
    handoff_files: list[str] = []

    for cmd in [
        ["git", "diff", "--name-only", "HEAD"],
        ["git", "diff", "--name-only", "--cached"],
    ]:
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                cwd=project_dir, timeout=5,
                creationflags=_NO_WIN,
            )
            for f in result.stdout.strip().splitlines():
                basename = os.path.basename(f)
                if any(basename.startswith(p) for p in prefixes) and basename.endswith(".md"):
                    handoff_files.append(f)
        except Exception:
            pass

    return handoff_files


def _is_disabled() -> bool:
    """Resolve opt-out: env var or ``cfg.feature(...)``. 2026-04-26 wiring fix."""
    raw = os.environ.get("CONCINNO_HANDOFF_CLAIM_GUARD_DISABLED", "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    try:
        from concinno.core.config import get_config
        if get_config().feature("handoff_claim_guard", "enabled") is False:
            return True
    except Exception:
        pass
    return False


def on_stop(hook_data: dict) -> Optional[str]:
    """Stop hook entry point.

    Returns:
        - "HANDOFF_CLAIM_BLOCK:<reason>" if handoff claimed but not written
        - None otherwise
    """
    if _is_disabled():
        return None

    session_id = hook_data.get("session_id", "")

    # Circuit breaker
    if _already_blocked(session_id):
        return None

    text = _extract_last_assistant_text(hook_data)
    if not text:
        return None

    if not _has_handoff_claim(text):
        return None

    # Claimed handoff — verify git has actual handoff changes
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", ".")
    changed = _git_changed_handoff_files(project_dir)

    if changed:
        return None  # Handoff file actually modified — all good

    # Claimed but not written → block
    reason = (
        "交接聲稱已寫但 git 中無交接檔變動。"
        "請實際寫入交接檔再停止。"
        " (Handoff claimed but no handoff file modified in git.)"
    )
    _record_block(session_id)
    return f"HANDOFF_CLAIM_BLOCK:{reason}"
