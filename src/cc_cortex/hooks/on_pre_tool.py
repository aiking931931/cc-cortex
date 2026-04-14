#!/usr/bin/env python3
"""cc-cortex PreToolUse hook — Guard Pipeline entry point.

All guards registered via ``create_default_pipeline()`` in ``guards/registry.py``.
Three-layer architecture: Security → Quality → Cognitive.
First DENY short-circuits. ALLOW collects additionalContext.

This file is a thin wrapper:
  stdin → GuardContext → Pipeline.run_pre_tool() → stdout
"""

from __future__ import annotations

import json
import sys

from cc_cortex.hooks.io_utils import cache_path, get_project_dir

# ── Paths ─────────────────────────────────────────────────────

_WORKSPACE = get_project_dir()

_CACHE_DIR = cache_path() if _WORKSPACE else ""

_HEALTH_PATH = cache_path("guard_health.json") if _CACHE_DIR else ""

_STEP_BACK_DIR = _CACHE_DIR  # step-back state lives alongside other cache


# ── Helpers ───────────────────────────────────────────────────


def _allow() -> None:
    json.dump({"permissionDecision": "allow"}, sys.stdout, ensure_ascii=False)


# ── Main ──────────────────────────────────────────────────────


def main(hook_data: dict | None = None) -> None:
    """Entry point. Accepts hook_data dict or reads from stdin."""
    try:
        if hook_data is None:
            hook_data = json.loads(sys.stdin.read())
    except Exception:
        _allow()
        return

    if not hook_data:
        _allow()
        return

    try:
        from cc_cortex.guards.base import GuardContext
        from cc_cortex.guards.registry import create_default_pipeline

        ctx = GuardContext.from_hook_data(hook_data)
        pipe = create_default_pipeline(step_back_state_dir=_STEP_BACK_DIR)

        if _HEALTH_PATH:
            pipe.load_health(_HEALTH_PATH)

        result = pipe.run_pre_tool(ctx)

        if _HEALTH_PATH:
            pipe.save_health(_HEALTH_PATH)

        # Note: Read/Glob/Grep recording is now handled inside
        # Pipeline.run_pre_tool() so every caller (including project-level
        # wrappers that bypass this entry point) gets consistent tracking.

        json.dump(result, sys.stdout, ensure_ascii=False)
    except Exception:
        # Fail-open: never block user due to pipeline crash
        _allow()


if __name__ == "__main__":
    main()
