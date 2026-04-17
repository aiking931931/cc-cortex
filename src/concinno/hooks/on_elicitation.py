"""concinno Elicitation hook — MCP interaction security audit.

Problem: MCP servers can request user input (API keys, credentials,
URLs) via Elicitation events. Without auditing, sensitive data flows
through unmonitored channels.

Solution: Audit all MCP elicitation requests. Flag suspicious patterns
(credential requests from unknown servers, unusual schemas).
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


_SENSITIVE_KEYWORDS = {
    "key", "token", "secret", "password", "credential",
    "api_key", "apikey", "auth",
}


def _is_credential_request(hook_data: dict) -> bool:
    """Check if the elicitation is requesting credentials."""
    message = str(hook_data.get("message", "")).lower()
    schema = hook_data.get("requested_schema", {})
    schema_str = json.dumps(schema).lower() if schema else ""

    for kw in _SENSITIVE_KEYWORDS:
        if kw in message or kw in schema_str:
            return True
    return False


def _write_output(context: str) -> None:
    """Write hook JSON output to stdout."""
    output = json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "Elicitation",
            "additionalContext": context,
        },
    }, ensure_ascii=False)
    if hasattr(sys.stdout, "buffer"):
        sys.stdout.buffer.write(output.encode("utf-8"))
        sys.stdout.buffer.flush()
    else:
        sys.stdout.write(output)
        sys.stdout.flush()


def main(hook_data: dict | None = None) -> None:
    """Elicitation entry point."""
    try:
        if hook_data is None:
            hook_data = json.loads(sys.stdin.read())
    except Exception:
        return

    server_name = hook_data.get("mcp_server_name", "unknown")
    message = hook_data.get("message", "")
    mode = hook_data.get("mode", "")
    ts = datetime.now(timezone.utc).isoformat()

    # Always audit
    _audit_log({
        "ts": ts,
        "event": "elicitation_request",
        "server": server_name,
        "mode": mode,
        "message_preview": str(message)[:200],
        "is_credential": _is_credential_request(hook_data),
    })

    # Flag credential requests
    if _is_credential_request(hook_data):
        _write_output(
            f"🔑 MCP server '{server_name}' requesting credentials. "
            "Verify this is a trusted server before providing secrets."
        )


if __name__ == "__main__":
    main()
