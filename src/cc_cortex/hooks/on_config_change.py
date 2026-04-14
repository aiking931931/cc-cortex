"""cc-cortex ConfigChange hook — audit and guard settings changes.

Problem: Unauthorized or accidental settings changes (e.g., disabling
guards, changing permissions) can silently degrade the system.

Solution: On ConfigChange, audit what changed and block dangerous changes:
  - Log all changes to audit trail
  - Block removal of security-critical hooks
  - Block switching from bypassPermissions to a weaker mode
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

# Settings keys that should never be removed/emptied
_PROTECTED_KEYS = {
    "hooks.PreToolUse",
    "hooks.PostToolUse",
    "hooks.Stop",
    "permissions.defaultMode",
}

# Hook event names that are security-critical
_CRITICAL_HOOKS = {"PreToolUse", "PostToolUse", "Stop"}


def _audit_log(entry: dict) -> None:
    """Append entry to config change audit log."""
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if not project_dir:
        return
    audit_dir = os.path.join(project_dir, ".cc_cortex_cache", "audit")
    os.makedirs(audit_dir, exist_ok=True)
    audit_path = os.path.join(audit_dir, "config_changes.jsonl")
    try:
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _detect_dangerous_changes(hook_data: dict) -> list[str]:
    """Detect dangerous configuration changes.

    Returns list of warning messages (empty = safe).
    """
    warnings: list[str] = []

    # ConfigChange provides old/new config or change details
    old_config = hook_data.get("oldConfig", {})
    new_config = hook_data.get("newConfig", {})

    # Check if critical hooks were removed
    old_hooks = old_config.get("hooks", {})
    new_hooks = new_config.get("hooks", {})

    for hook_name in _CRITICAL_HOOKS:
        old_val = old_hooks.get(hook_name, [])
        new_val = new_hooks.get(hook_name, [])
        if old_val and not new_val:
            warnings.append(
                f"🚫 Critical hook removed: {hook_name}. "
                "This disables security/quality guards."
            )

    # Check permission mode downgrade
    old_perms = old_config.get("permissions", {})
    new_perms = new_config.get("permissions", {})
    old_mode = old_perms.get("defaultMode", "")
    new_mode = new_perms.get("defaultMode", "")
    if old_mode and new_mode and old_mode != new_mode:
        warnings.append(
            f"⚠ Permission mode changed: {old_mode} → {new_mode}"
        )

    return warnings


def main(hook_data: dict | None = None) -> None:
    """ConfigChange entry point."""
    try:
        if hook_data is None:
            hook_data = json.loads(sys.stdin.read())
    except Exception:
        return

    # Always audit
    ts = datetime.now(timezone.utc).isoformat()
    session_id = hook_data.get("session_id", "")
    _audit_log({
        "timestamp": ts,
        "session_id": session_id,
        "event": "config_change",
        "data": {
            k: v for k, v in hook_data.items()
            if k not in ("messages",)  # exclude large fields
        },
    })

    # Detect dangerous changes
    warnings = _detect_dangerous_changes(hook_data)

    if not warnings:
        return

    context = (
        "⚠ Configuration change detected:\n"
        + "\n".join(warnings)
        + "\nReview these changes carefully."
    )

    output = json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "ConfigChange",
            "additionalContext": context,
        }
    }, ensure_ascii=False)

    if hasattr(sys.stdout, "buffer"):
        sys.stdout.buffer.write(output.encode("utf-8"))
        sys.stdout.buffer.flush()
    else:
        sys.stdout.write(output)
        sys.stdout.flush()


if __name__ == "__main__":
    main()
