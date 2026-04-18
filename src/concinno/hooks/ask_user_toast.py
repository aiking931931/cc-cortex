"""concinno PreToolUse hook — AskUserQuestion toast notifier.

@module hooks.ask_user_toast
@responsibility Detect ``AskUserQuestion`` PreToolUse events and surface
    a Windows toast via :func:`concinno.core.notify.show_toast` so the
    operator sees "Claude is asking you something" even when the
    terminal is backgrounded. User filed two complaints in one session
    (2026-04-18) about silently-waiting AskUser dialogs eating 10+
    minutes of their time before they noticed.

@dependencies concinno.core.notify (lazy-imported so non-Windows / no-
    pywintypes builds don't hard-fail at module load)
@exports maybe_show_ask_user_toast, main

Failure contract:
    Every code path catches :class:`Exception` and degrades to ALLOW.
    A broken notifier MUST NOT deny an AskUserQuestion — the question
    still has to reach the user, even without the toast.

Wiring:
    Registered as a PreToolUse hook matching ``AskUserQuestion`` in
    ``settings.json``. Runs alongside the guard pipeline; the toast is
    side-effect only (no deny / no rewrite), so ordering relative to
    pipeline guards doesn't matter.
"""

from __future__ import annotations

import json
import sys
import threading
from typing import Any

__all__ = ["main", "maybe_show_ask_user_toast"]

#: How much of the question body we embed in the toast. Keep it
#: short — Windows toasts truncate silently around 200 chars and
#: readability drops off fast past 60.
_BODY_PREVIEW_CHARS = 60

#: Toast title. Fixed string so the reputation counter (see
#: :func:`concinno.core.notify.show_toast`) stays anchored to a
#: single "kind of event".
_TOAST_TITLE = "Claude 在問你 — 請回答問題"

#: Tag / group for replace-prev behaviour: a burst of three questions
#: should collapse to ONE toast, not stack.
_TOAST_TAG = "concinno-ask-user"
_TOAST_GROUP = "concinno-ask-user"


def _extract_question_preview(tool_input: Any) -> str:
    """Return the leading :data:`_BODY_PREVIEW_CHARS` of the question.

    Claude Code's AskUserQuestion schema varies: sometimes the first
    field is ``question``, sometimes ``prompt``, sometimes a nested
    ``questions`` list. We try the obvious keys in order, then fall
    back to ``str(tool_input)`` so something always renders.
    """
    if not isinstance(tool_input, dict):
        return ""
    for key in ("question", "prompt", "text", "message"):
        val = tool_input.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()[:_BODY_PREVIEW_CHARS]
    nested = tool_input.get("questions")
    if isinstance(nested, list) and nested:
        first = nested[0]
        if isinstance(first, dict):
            for key in ("question", "prompt", "text"):
                val = first.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()[:_BODY_PREVIEW_CHARS]
        elif isinstance(first, str) and first.strip():
            return first.strip()[:_BODY_PREVIEW_CHARS]
    # Last-resort stringification — truncate aggressively.
    return str(tool_input)[:_BODY_PREVIEW_CHARS]


def maybe_show_ask_user_toast(hook_data: dict) -> bool:
    """Surface a toast when ``hook_data`` describes an AskUserQuestion.

    Args:
        hook_data: Raw PreToolUse payload from Claude Code.

    Returns:
        ``True`` when a toast was emitted, ``False`` otherwise (wrong
        tool, toast disabled, or notifier errored out). Callers use
        the return only for testing — the hook protocol cares only
        about the JSON decision, not the boolean.
    """
    try:
        if not isinstance(hook_data, dict):
            return False
        if hook_data.get("tool_name") != "AskUserQuestion":
            return False

        preview = _extract_question_preview(hook_data.get("tool_input"))
        message = (
            f"{preview}..."
            if preview and not preview.endswith(".")
            else preview or "Claude 正在等待你的回答。"
        )

        try:
            from concinno.core.notify import show_toast
        except Exception:  # noqa: BLE001 — fail-open
            return False

        # F7 (2.7.1): detach toast emission onto a daemon thread so
        # the hook's 3s settings.json timeout cannot fire during a
        # cold Windows COM / WinRT initialisation (2-5s on first
        # call in a fresh session). The AskUserQuestion still reaches
        # the user synchronously via the ALLOW decision below; the
        # toast is pure notification side-effect and can arrive a
        # beat later. Returning ``True`` before the thread finishes
        # matches the hook protocol — the boolean is for test
        # instrumentation only.
        def _fire() -> None:
            try:
                show_toast(
                    title=_TOAST_TITLE,
                    message=message,
                    tag=_TOAST_TAG,
                    group=_TOAST_GROUP,
                )
            except Exception:
                # Daemon thread swallows errors — toast failure must
                # never tombstone the interpreter.
                pass

        threading.Thread(
            target=_fire,
            name="concinno-ask-user-toast",
            daemon=True,
        ).start()
        return True
    except Exception:  # noqa: BLE001 — fail-open
        return False


def _allow() -> None:
    """Emit the default ALLOW decision to stdout."""
    json.dump(
        {"permissionDecision": "allow"},
        sys.stdout,
        ensure_ascii=False,
    )


def main(hook_data: dict | None = None) -> None:
    """Entry point used by ``pyproject.toml`` / ``settings.json``.

    Reads the PreToolUse payload (or accepts one passed in for tests),
    fires the toast if applicable, and ALWAYS emits an ALLOW decision.
    This hook never denies — the guard pipeline remains the only place
    that refuses a tool call.
    """
    try:
        if hook_data is None:
            hook_data = json.loads(sys.stdin.read())
    except Exception:  # noqa: BLE001 — fail-open
        _allow()
        return
    if not hook_data:
        _allow()
        return

    maybe_show_ask_user_toast(hook_data)
    _allow()


if __name__ == "__main__":  # pragma: no cover — CLI entry point
    main()
