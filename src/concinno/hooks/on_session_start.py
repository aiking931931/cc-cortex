#!/usr/bin/env python3
"""concinno SessionStart hook — session init, zombie cleanup.

Fail-open: any crash -> exit silently.
"""

from __future__ import annotations

import json
import sys


def main(hook_data: dict | None = None) -> None:
    """Entry point."""
    try:
        if hook_data is None:
            hook_data = json.loads(sys.stdin.read())
    except Exception:
        return

    # --- Module: multi_instance (acquire lock + zombie GC) ---
    try:
        from concinno.multi_instance import acquire_lock

        acquire_lock()
    except (ImportError, Exception):
        pass

    # --- Module: cognitive (start session profiling) ---
    try:
        from concinno.cognitive import on_session_start

        on_session_start(hook_data)
    except (ImportError, Exception):
        pass

    # --- Module: git_assist (ensure git repo exists) ---
    try:
        from concinno.git_assist import ensure_git_repo

        result = ensure_git_repo()
        if result.get("initialized"):
            print(
                "🔀 Git repo initialized by CC Cortex. "
                "Auto-commit enabled for session backups.",
                file=sys.stderr,
            )
        elif result.get("error"):
            print(
                f"⚠ Git init skipped: {result['error']}",
                file=sys.stderr,
            )
    except (ImportError, Exception):
        pass

    # --- Module: notify_health (reset Win11 AUMID counter) ---
    # Win11 suppresses toast banners after ~3 notifications per 24h per
    # AUMID (PeriodicNotificationCount threshold). Reset on every session
    # start so banners always work when the user actually starts working.
    # Root-causes "通知又沒了" regression (kb_notify_health Mode 1).
    try:
        from concinno.notify_health import auto_reset_on_session_start

        auto_reset_on_session_start(verbose=True)
    except (ImportError, Exception):
        pass

    # --- narrower-scope v4: inject active preset into agent context ---
    _emit_active_preset()


def _emit_active_preset() -> None:
    """Emit active v4 preset name as hookSpecificOutput.additionalContext.

    The agent reads this on its first turn so primacy-bias can't drop
    the user's cascade choice (MEMORY #71 Switch-First directive).
    Fail-soft: any error -> silent, the hook contract stays intact.
    """
    try:
        from concinno.preset_cascade import get_active_preset

        name = get_active_preset()
    except Exception:
        return
    try:
        payload = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": f"concinno: active_preset={name}\n",
            },
        }
        sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    except Exception:
        pass


if __name__ == "__main__":
    main()
