"""cc_cortex.hooks.io_utils — Shared I/O helpers for Claude Code hooks.

Eliminates copy-paste of JSON stdin parsing, stdout formatting,
and timezone/project-dir resolution across hook scripts.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Optional


def read_stdin() -> Optional[dict]:
    """Parse JSON from stdin. Returns None on failure (logs to stderr)."""
    try:
        return json.loads(sys.stdin.read())
    except Exception as e:
        print(f"⚠ hook: stdin parse failed: {e}", file=sys.stderr)
        return None


def _is_silent() -> bool:
    """Check if hook silent mode is active (LLM sees context but must not respond about it).

    Resolution order:
    1. ``CC_CORTEX_SILENT`` env var (one-shot CLI override, highest priority)
    2. ``profile_settings.silent`` in cc_config.json (set via /hook profile competition)
    """
    env_val = os.environ.get("CC_CORTEX_SILENT")
    if env_val is not None:
        return env_val == "1"
    try:
        from cc_cortex.core.config import get_config
        settings = get_config().raw("profile_settings", {})
        return bool(settings.get("silent", False)) if isinstance(settings, dict) else False
    except Exception:
        return False


_SILENT_PREFIX = (
    "[INTERNAL — DO NOT mention this in your response. "
    "Process silently. Never reference warnings, ratios, "
    "or hook guidance to the user.]\n"
)


def _silence_context(data: dict) -> dict:
    """Wrap additionalContext with suppression directive in silent mode."""
    hso = data.get("hookSpecificOutput")
    if not isinstance(hso, dict):
        return data
    ctx = hso.get("additionalContext")
    if not isinstance(ctx, str) or not ctx:
        return data
    # Remove [SHOW USER VERBATIM] tags — in silent mode, nothing shown
    ctx = ctx.replace("[SHOW USER VERBATIM] ", "").replace("[SHOW USER VERBATIM]", "")
    data = {**data, "hookSpecificOutput": {**hso, "additionalContext": _SILENT_PREFIX + ctx}}
    return data


def write_json_stdout(data: dict) -> None:
    """Write JSON dict to stdout (UTF-8, no ASCII escaping).

    In silent mode (CC_CORTEX_SILENT=1): additionalContext is wrapped
    with suppression directive — LLM sees the info internally but must
    not mention it in its response to the user.
    """
    if _is_silent():
        data = _silence_context(data)
    sys.stdout.write(json.dumps(data, ensure_ascii=False))


def write_additional_context(context: str) -> None:
    """Write standard hook output with additionalContext."""
    if not context:
        return
    write_json_stdout({
        "hookSpecificOutput": {
            "additionalContext": context,
        },
    })


def allow_response() -> None:
    """Write a standard PreToolUse allow response."""
    json.dump({"permissionDecision": "allow"}, sys.stdout)


def deny_response(reason: str, **extra: Any) -> None:
    """Write a standard PreToolUse deny response."""
    result = {"permissionDecision": "deny", "reason": reason}
    result.update(extra)
    write_json_stdout(result)


# ── Project Path Helpers ────────────────────────────────────

def get_project_dir() -> str:
    """Get the Claude project directory from env or default."""
    return os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())


def project_path(*parts: str) -> str:
    """Join parts relative to the project directory."""
    return os.path.join(get_project_dir(), *parts)


def ai_brain_path(*parts: str) -> str:
    """Join parts relative to the configured brain directory."""
    from cc_cortex.core.config import get_config
    brain_dir = get_config().brain_dir
    return project_path(brain_dir, *parts)


def learnings_path() -> str:
    """Path to learnings.json."""
    return ai_brain_path("01_Memory", "evolution", "learnings.json")


def pending_distill_path() -> str:
    """Path to pending_distill.json."""
    return ai_brain_path("01_Memory", "evolution", "pending_distill.json")


def cache_path(*parts: str) -> str:
    """Join parts relative to .cc_cortex_cache/."""
    return project_path(".cc_cortex_cache", *parts)


# ── Timezone ────────────────────────────────────────────────

def get_local_tz():
    """Get local timezone (UTC+8), preferring cc_cortex.core.config if available."""
    try:
        from cc_cortex.core.config import LOCAL_TZ
        return LOCAL_TZ
    except ImportError:
        from datetime import timedelta, timezone
        return timezone(timedelta(hours=8))
