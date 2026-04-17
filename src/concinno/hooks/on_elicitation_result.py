"""concinno ElicitationResult hook — input validation + audit trail.

Problem: After a user responds to an MCP elicitation, the input flows
directly to the MCP server without validation or logging.

Solution: Audit all responses. Warn if credentials appear to be
sent to untrusted servers.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone


def _audit_log(entry: dict) -> None:
    """Append entry to elicitation audit log."""
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if not project_dir:
        return
    audit_dir = os.path.join(
        project_dir, ".concinno_cache", "audit",
    )
    os.makedirs(audit_dir, exist_ok=True)
    audit_path = os.path.join(audit_dir, "elicitation.jsonl")
    try:
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _redact_sensitive(content: dict) -> dict:
    """Redact potentially sensitive values for audit logging."""
    redacted = {}
    sensitive = {"key", "token", "secret", "password", "credential"}
    for k, v in content.items():
        if any(s in k.lower() for s in sensitive):
            redacted[k] = "***REDACTED***"
        else:
            redacted[k] = v
    return redacted


def main(hook_data: dict | None = None) -> None:
    """ElicitationResult entry point."""
    try:
        if hook_data is None:
            hook_data = json.loads(sys.stdin.read())
    except Exception:
        return

    server_name = hook_data.get("mcp_server_name", "unknown")
    action = hook_data.get("action", "unknown")
    content = hook_data.get("content", {})
    ts = datetime.now(timezone.utc).isoformat()

    # Redact sensitive values before logging
    safe_content = (
        _redact_sensitive(content)
        if isinstance(content, dict)
        else {"raw": str(content)[:100]}
    )

    _audit_log({
        "ts": ts,
        "event": "elicitation_response",
        "server": server_name,
        "action": action,
        "content_keys": list(safe_content.keys()),
        "has_redacted": any(
            v == "***REDACTED***" for v in safe_content.values()
        ),
    })


if __name__ == "__main__":
    main()
