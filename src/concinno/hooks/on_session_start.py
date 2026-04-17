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


if __name__ == "__main__":
    main()
