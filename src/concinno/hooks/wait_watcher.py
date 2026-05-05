"""concinno.hooks.wait_watcher — PostToolUse polling-register hook.

Inspects the just-completed tool call. If it implies a wait state
(see :mod:`concinno.polling.classifier`), records the wait via
:func:`concinno.polling.wait_queue.register_wait` and starts the
polling daemon if it isn't already running.

Returns an ``additionalContext`` fragment so the agent sees:

    📡 polling: Wait registered ({kind}, ETA {n}s).
    Daemon will status-check every {interval}s; alerts surface on
    your next prompt. Per polling 鐵律, you may also call
    ScheduleWakeup({delay}s) for self-poll.

This file is imported lazily by :mod:`on_post_tool` so the
PostToolUse fast path stays cheap when the feature is off.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("concinno.hooks.wait_watcher")


def _feature_enabled() -> bool:
    try:
        from concinno.core.config import get_config
        return bool(get_config().feature("polling_watcher", "enabled"))
    except Exception:
        return True  # productivity feature — fail-open


def maybe_register_wait(
    tool_name: str,
    tool_input: dict,
) -> Optional[str]:
    """Detect + register. Returns an ``additionalContext`` fragment
    to surface to the agent, or ``None`` when no wait was detected
    (or the feature is off)."""
    if not _feature_enabled():
        return None
    try:
        from concinno.polling import (
            classify_wait,
            register_wait,
            start_daemon,
        )
    except Exception:
        return None
    try:
        cls = classify_wait(tool_name, tool_input)
    except Exception:
        logger.exception("classify_wait failed")
        return None
    if cls is None:
        return None
    try:
        record = register_wait(
            tool_name=tool_name,
            tool_input=tool_input,
            kind=cls.kind,
            check_cmd=cls.check_cmd,
            eta_seconds=cls.eta_seconds,
            pid=cls.pid,
            extra={
                "tool_name": tool_name,
                "preview": _preview(tool_name, tool_input),
            },
        )
    except Exception:
        logger.exception("register_wait failed")
        return None
    try:
        start_daemon()
    except Exception:
        logger.exception("start_daemon failed")

    return _build_hint(record.id, cls.kind, cls.eta_seconds)


def _preview(tool_name: str, tool_input: dict) -> str:
    """Tiny human-readable label so the inject hook can show
    'Bash: twine upload dist/...' rather than 'wait #abcd1234'."""
    if tool_name == "Bash":
        cmd = str(tool_input.get("command", ""))[:80]
        return f"Bash: {cmd}"
    if tool_name == "Agent":
        descr = str(tool_input.get("description", ""))[:60] or "subagent"
        return f"Agent: {descr}"
    return tool_name


def _build_hint(task_id: str, kind: str, eta: int) -> str:
    delay = max(60, min(eta, 1800))
    return (
        "📡 polling: Wait registered "
        f"(id={task_id} kind={kind} eta={eta}s). "
        "Daemon polls every 60s; alerts on next user prompt. "
        f"Self-wake recommended: ScheduleWakeup(delaySeconds={delay}, "
        "prompt='check pending waits')."
    )
