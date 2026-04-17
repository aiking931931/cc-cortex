"""concinno.exfil_guard — Data exfiltration prevention.

@module exfil_guard
@responsibility Detect and deny attempts to upload sensitive files (credentials,
    keys, env files) to external services via curl/wget. PreToolUse gate.
@dependencies concinno.constants, concinno.guards.base
@exports check, ExfilGuard
"""

from __future__ import annotations

import re
from typing import Optional

from concinno.constants import make_deny
from concinno.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

# Sensitive file patterns (basenames or extensions)
_SENSITIVE_FILES = re.compile(
    r"(?:^|[\s/\\@=])"
    r"(?:"
    r"\.env(?:\.local|\.prod|\.production|\.staging)?|"
    r"credentials\.json|service[_-]?account\.json|"
    r"id_rsa|id_ed25519|id_ecdsa|"
    r"\w+\.pem|\w+\.key|\w+\.p12|\w+\.pfx|"
    r"kubeconfig|\.kube/config|"
    r"\.aws/credentials|\.aws/config|"
    r"\.ssh/|\.gnupg/|"
    r"wallet\.dat|"
    r"secrets\.ya?ml|vault\.ya?ml|"
    r"\.npmrc|\.pypirc|\.docker/config\.json|"
    r"/var/log/auth\.log|/var/log/syslog|/var/log/secure"
    r")",
    re.IGNORECASE,
)

# Upload commands
_UPLOAD_PATTERNS = re.compile(
    r"(?:"
    r"curl\s+.*(?:-[a-zA-Z]*[FdT]|--upload-file|--data|POST)|"
    r"curl\s+-[a-zA-Z]*[FdT]|"
    r"wget\s+.*--post|"
    r"scp\s|rsync\s.*[^/]:|"
    r"aws\s+s3\s+cp|"
    r"gsutil\s+cp|"
    r"az\s+storage\s+blob\s+upload"
    r")",
    re.IGNORECASE,
)

# Pipe to external (cat secret | curl)
_PIPE_EXTERNAL = re.compile(
    r"cat\s+.*(?:\.env|credentials|id_rsa|\.key|\.pem|\.ya?ml|config|shadow|passwd|auth\.log|syslog|secure).*\|\s*(?:curl|wget|nc|ncat)",
    re.IGNORECASE,
)

# Encrypted pipe exfil (tar/openssl/gpg piped to curl/wget)
_ENCRYPTED_PIPE = re.compile(
    r"(?:tar|openssl|gpg)\s+.*\|\s*.*(?:curl|wget)\s+",
    re.IGNORECASE,
)


def check(
    tool_name: str,
    tool_input: dict,
) -> Optional[dict]:
    """Check if a Bash command attempts to exfiltrate sensitive files.

    Args:
        tool_name: Must be "Bash" to trigger.
        tool_input: Tool input dict with "command" key.

    Returns:
        Dict {permissionDecision: "deny", reason, file} or None.
    """
    if tool_name != "Bash":
        return None
    command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
    if not command:
        return None

    # Check pipe exfiltration first (simpler pattern)
    if _PIPE_EXTERNAL.search(command):
        return make_deny(
            "🛡️ Exfiltration blocked: piping sensitive file to external command. "
            "This could leak credentials or private keys.",
            file="pipe",
        )

    # Check encrypted pipe exfiltration (tar/openssl/gpg | curl)
    if _ENCRYPTED_PIPE.search(command):
        return make_deny(
            "🛡️ Exfiltration blocked: encrypted data piped to external upload. "
            "This pattern is commonly used to exfiltrate data covertly.",
            file="encrypted_pipe",
        )

    # Check upload commands referencing sensitive files
    if not _UPLOAD_PATTERNS.search(command):
        return None

    sensitive_match = _SENSITIVE_FILES.search(command)
    if not sensitive_match:
        return None

    matched = sensitive_match.group(0).strip()
    return make_deny(
        f"🛡️ Exfiltration blocked: uploading sensitive file `{matched}`. "
        "Credentials, keys, and env files must not be sent to external services.",
        file=matched,
    )


# ── BaseGuard adapter ───────────────────────────────────────────


class ExfilGuard(BaseGuard):
    """Data exfiltration prevention."""

    name = "exfil_guard"
    category = GuardCategory.SECURITY

    def check(self, ctx: GuardContext) -> GuardResult | None:
        """Block curl/wget/scp uploads of credentials, keys, and env files.

        Args:
            ctx: Guard context with tool_name and tool_input.

        Returns:
            GuardResult.deny identifying the sensitive file, or None if safe.
        """
        result = check(ctx.tool_name, ctx.tool_input)
        if result is None:
            return None
        return GuardResult.deny(
            result.get("reason", self.name),
            context=result.get("additionalContext", ""),
            file=result.get("file", ""),
        )
