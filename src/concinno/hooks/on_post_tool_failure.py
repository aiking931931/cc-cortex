"""concinno PostToolUseFailure hook — failure pattern analysis.

Problem: Tool failures are early signals of systemic issues. Without
tracking, the same failures repeat across sessions.

Solution: Classify failure type and delegate burst tracking to
``ErrorRecovery.record_burst`` — which owns the 10-minute consecutive
window and history persistence via ``StateStore``. This module owns only
the presentation layer: six-category classifier, prescription strings,
and the Chinese B0 escalation message.

Migration note (2026-04-11): the burst mechanism moved from a project-
scoped JSONL at ``.concinno_cache/tool_failures.jsonl`` to a session-
scoped ``StateStore`` entry under the ``tool_failure_burst`` namespace.
Hook invocations that lack ``session_id`` fall back to a ``"default"``
bucket so behavior degrades gracefully for runtimes that don't inject it.
"""

from __future__ import annotations

import json
import os
import sys


def _classify_error(error: str) -> str:
    """Classify error into categories for pattern tracking."""
    err_lower = error.lower()
    if "permission" in err_lower or "access denied" in err_lower:
        return "permission"
    if "not found" in err_lower or "no such file" in err_lower:
        return "path_not_found"
    if "timeout" in err_lower or "timed out" in err_lower:
        return "timeout"
    if "syntax" in err_lower or "parse" in err_lower:
        return "syntax"
    if "import" in err_lower or "module" in err_lower:
        return "import"
    if "conflict" in err_lower or "merge" in err_lower:
        return "conflict"
    return "other"


_PRESCRIPTIONS = {
    "path_not_found": (
        "File not found failures recurring. Use Glob to verify "
        "paths exist before writing/reading."
    ),
    "permission": (
        "Permission failures recurring. Check file ownership "
        "and working directory."
    ),
    "timeout": (
        "Timeout failures recurring. Use run_in_background for "
        "long-running commands."
    ),
    "syntax": (
        "Syntax errors recurring. Validate code structure before "
        "writing files."
    ),
    "import": (
        "Import failures recurring. Verify module paths and "
        "virtual environment activation."
    ),
}


def _build_escalation_context(
    tool: str, category: str, consecutive: int,
) -> str:
    """Build B0 patch-loop escalation message via cbua_format."""
    try:
        from concinno.cbua_ux import CbuaCode, cbua_format

        header = cbua_format(
            CbuaCode.B0,
            f"PATCH LOOP \u00d7 {consecutive}\uff08{tool}/{category}\uff09",
        )
    except ImportError:
        header = (
            f"\U0001f534 B0 PATCH LOOP \u00d7 {consecutive}"
            f"\uff08{tool}/{category}\uff09"
        )
    body = (
        "\u2014 STOP patching. "
        "\u5f37\u5236\u5347\u7d1a B1 \u4e09\u5c64\u601d\u8003\uff1a\n"
        "1. \u756b\u51fa\u5b8c\u6574\u56e0\u679c\u93c8"
        "\uff08\u8f38\u5165\u2192\u4e2d\u9593\u614b\u2192\u8f38\u51fa\uff09\n"
        "2. \u5217\u51fa\u6240\u6709\u5fc5\u8981\u689d\u4ef6"
        "\uff0c\u9010\u4e00\u9a57\u8b49\n"
        "3. \u4e00\u6b21\u89e3\u5b8c"
        "\uff0c\u4e0d\u8981\u518d patch\n"
        "\u300c\u6d88\u9664 error message \u2260 "
        "\u89e3\u6c7a\u554f\u984c\u3002"
        "\u5148\u554f\uff1a\u9019\u500b\u7d44\u4ef6\u9700\u8981"
        "\u4ec0\u9ebc\u624d\u80fd\u6b63\u78ba\u904b\u4f5c\uff1f\u300d"
    )
    return header + "\n" + body


def _build_context(
    tool: str, category: str, count: int,
) -> str:
    """Build corrective context for recurring failures."""
    prescription = _PRESCRIPTIONS.get(
        category,
        f"Pattern: {tool} fails with {category} errors.",
    )
    return f"⚠ {tool} failure #{count} ({category}): {prescription}"


def _write_output(context: str) -> None:
    """Write hook JSON output to stdout."""
    output = json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUseFailure",
            "additionalContext": context,
        },
    }, ensure_ascii=False)
    if hasattr(sys.stdout, "buffer"):
        sys.stdout.buffer.write(output.encode("utf-8"))
        sys.stdout.buffer.flush()
    else:
        sys.stdout.write(output)
        sys.stdout.flush()


def _is_user_denial(error: str) -> bool:
    """Return True only for genuinely user-initiated aborts.

    Red team #1 found that the original substring match ate system-level
    failures: ``permission denied`` / ``connection denied`` / timeouts
    labeled ``cancelled`` / ``EINTR interrupted system call`` / etc.
    Those are exactly the failures burst tracking must count — they are
    the patch-loop fuel. So:

    - ``user rejected`` / ``user denied`` / ``user cancelled`` → user
      action (skip)
    - ``denied`` when preceded by ``permission``/``access`` → SYSTEM
      failure (count)
    - ``cancelled``/``interrupted``/``aborted`` alone → ambiguous; we
      require a ``by user`` / ``by the user`` marker to treat as user
      action, otherwise count
    """
    lo = error.lower()
    # Unambiguous user action markers — skip the hook
    user_markers = (
        "user rejected",
        "user denied",
        "user cancelled",
        "user canceled",
        "denied by user",
        "cancelled by user",
        "canceled by user",
        "interrupted by user",
        "aborted by user",
        "rejected by user",
    )
    if any(m in lo for m in user_markers):
        return True
    # Bare "interrupted"/"cancelled" ONLY if no system-error context.
    # If the error mentions permission/access/timeout/connection/etc.,
    # it is a system failure that must be counted.
    system_context = (
        "permission", "access", "timeout", "timed out",
        "connection", "network", "socket", "errno",
        "broken pipe", "refused", "no such", "eintr",
    )
    if any(s in lo for s in system_context):
        return False
    # Last-ditch heuristic: a lone "interrupted"/"aborted" with no
    # other context is more likely a user Ctrl-C than a real failure.
    lonely_user_hints = ("^c", "keyboardinterrupt", "sigint")
    return any(h in lo for h in lonely_user_hints)


def main(hook_data: dict | None = None) -> None:
    """PostToolUseFailure entry point.

    Wrapped in a best-effort ``try/except`` — hooks must never crash the
    host; red team #1-H2 found that lock contention (`RuntimeError` from
    `StateStore.read_modify_write`) or corrupt state could bubble up and
    break the PostToolUseFailure event.
    """
    try:
        if hook_data is None:
            hook_data = json.loads(sys.stdin.read())
    except Exception:
        return

    try:
        _dispatch(hook_data)
    except Exception:
        # Best-effort: any unexpected failure inside burst tracking,
        # confidence update, or stdout write must not propagate.
        return


def _dispatch(hook_data: dict) -> None:
    tool = hook_data.get("tool_name", "unknown")
    error = str(hook_data.get("error", ""))

    # Skip user-initiated denials
    if _is_user_denial(error):
        return

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if not project_dir:
        return

    category = _classify_error(error)
    # Burst tracking is intentionally NOT session-scoped: patch loops
    # often span session restarts (red team #1-H3). We pin the bucket
    # to a stable project-level key so consecutive failure counts
    # survive across Claude Code session boundaries.
    session_id = "tool-failure-burst"
    cache_dir = os.path.join(project_dir, ".concinno_cache")

    from concinno.error_recovery import ErrorRecovery

    recovery = ErrorRecovery(cache_dir, session_id)
    total, consecutive = recovery.record_burst(tool, category)

    # Update confidence calibration (overwrite-style)
    try:
        from concinno.confidence_record import update_confidence
        update_confidence(
            cache_dir,
            domain=tool,
            success=False,
            error_pattern=category,
        )
    except ImportError:
        pass  # CCC not installed — graceful degrade

    verdict = ErrorRecovery.classify_burst(consecutive, total)
    if verdict == "escalate":
        _write_output(
            _build_escalation_context(tool, category, consecutive),
        )
    elif verdict == "prescribe":
        _write_output(_build_context(tool, category, total))


if __name__ == "__main__":
    main()
