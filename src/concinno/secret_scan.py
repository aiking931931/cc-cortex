"""concinno.secret_scan — Detect hardcoded secrets in Write/Edit content.

@module secret_scan
@responsibility PreToolUse gate that catches API keys, tokens, passwords, and
    private keys before they land in code via Write/Edit tools.
@dependencies concinno.constants, concinno.guards.base
@exports check, SecretScanGuard
"""

from __future__ import annotations

import re
from typing import Optional

from concinno.constants import WRITE_TOOLS as _WRITE_TOOLS
from concinno.constants import make_deny
from concinno.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

# ── Secret Patterns ──────────────────────────────────────────────

_PATTERNS: list[tuple[str, re.Pattern]] = [
    # AWS
    ("AWS Access Key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("AWS Secret Key", re.compile(
        r"""(?:aws_secret_access_key|secret_key)\s*[=:]\s*['"]?[A-Za-z0-9/+=]{40}"""
    )),
    # GitHub / GitLab tokens
    ("GitHub Token", re.compile(r"gh[pousr]_[A-Za-z0-9_]{36,}")),
    ("GitLab Token", re.compile(r"glpat-[A-Za-z0-9_\-]{20,}")),
    # Generic API key patterns
    ("API Key Assignment", re.compile(
        r"""(?:api[_-]?key|apikey|api[_-]?secret|access[_-]?token|auth[_-]?token"""
        r"""|secret[_-]?key|private[_-]?key|bearer)\s*[=:]\s*['"]([A-Za-z0-9_\-./+=]{20,})['"]""",
        re.IGNORECASE,
    )),
    # Private keys (PEM)
    ("Private Key", re.compile(r"-----BEGIN\s+(RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")),
    # Slack tokens
    ("Slack Token", re.compile(r"xox[bporas]-[A-Za-z0-9\-]{10,}")),
    # Generic password assignment
    ("Password Assignment", re.compile(
        r"""(?:password|passwd|pwd)\s*[=:]\s*['"]([^'"]{8,})['"]""",
        re.IGNORECASE,
    )),
    # Anthropic / OpenAI keys
    ("Anthropic Key", re.compile(r"sk-ant-api\d{2}-[A-Za-z0-9_\-]{80,}")),
    ("OpenAI Key", re.compile(r"sk-[A-Za-z0-9]{48,}")),
    # Stripe
    ("Stripe Key", re.compile(r"[sr]k_(live|test)_[A-Za-z0-9]{20,}")),
    # SendGrid
    ("SendGrid Key", re.compile(r"SG\.[A-Za-z0-9_\-]{22}\.[A-Za-z0-9_\-]{43}")),
    # JWT tokens (header.payload.signature)
    ("JWT Token", re.compile(
        r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    )),
    # Database connection strings with embedded passwords
    ("DB Connection String", re.compile(
        r"(?:postgres|mysql|mongodb)(?:ql)?://\w+:[^@\s]{6,}@",
        re.IGNORECASE,
    )),
]

# Files where secrets are expected (don't scan these)
_EXEMPT_EXTENSIONS = frozenset([".env.example", ".env.template", ".md"])
_EXEMPT_BASENAMES = frozenset(["CLAUDE.md", "README.md", "CHANGELOG.md"])


def _get_content(tool_name: str, tool_input: dict) -> tuple[str, str]:
    """Extract file path and content from tool input."""
    fp = tool_input.get("file_path") or tool_input.get("path") or ""
    if tool_name == "Edit":
        content = tool_input.get("new_string", "")
    elif tool_name == "Write":
        content = tool_input.get("content", "")
    else:
        content = ""
    return fp, content


def _is_exempt(file_path: str) -> bool:
    """Check if file is exempt from secret scanning."""
    import os

    basename = os.path.basename(file_path)
    if basename in _EXEMPT_BASENAMES:
        return True
    for ext in _EXEMPT_EXTENSIONS:
        if file_path.endswith(ext):
            return True
    return False


def check(
    tool_name: str,
    tool_input: dict,
    *,
    extra_patterns: list[tuple[str, re.Pattern]] | None = None,
) -> Optional[dict]:
    """Scan Write/Edit content for hardcoded secrets.

    Args:
        tool_name: Must be Write or Edit to trigger.
        tool_input: Tool input dict.
        extra_patterns: Additional (name, regex) patterns to check.

    Returns:
        Dict {permissionDecision: "deny", reason, secrets} or None.
    """
    if tool_name not in _WRITE_TOOLS:
        return None
    if not isinstance(tool_input, dict):
        return None

    fp, content = _get_content(tool_name, tool_input)
    if not content:
        return None
    if fp and _is_exempt(fp):
        return None

    patterns = list(_PATTERNS)
    if extra_patterns:
        patterns.extend(extra_patterns)

    found: list[str] = []
    for name, pattern in patterns:
        if pattern.search(content):
            found.append(name)

    if not found:
        return None

    import os

    basename = os.path.basename(fp) if fp else "unknown"
    secret_list = ", ".join(found[:3])
    extra = f" +{len(found) - 3} more" if len(found) > 3 else ""

    return make_deny(
        f"🔐 Secret detected in {basename}: {secret_list}{extra}. "
        "Use environment variables or a secrets manager instead. "
        "If this is a test fixture or example, use a clearly fake value.",
        secrets=found,
    )


# ── BaseGuard adapter ───────────────────────────────────────────


class SecretScanGuard(BaseGuard):
    """Detect hardcoded secrets in Write/Edit content."""

    name = "secret_scan"
    category = GuardCategory.SECURITY

    def check(self, ctx: GuardContext) -> GuardResult | None:
        """Scan Write/Edit content for hardcoded secrets (API keys, tokens, PEM).

        Args:
            ctx: Guard context with tool_name and tool_input.

        Returns:
            GuardResult.deny listing detected secret types, or None if clean.
        """
        result = check(ctx.tool_name, ctx.tool_input)
        if result is None:
            return None
        return GuardResult.deny(
            result.get("reason", self.name),
            context=result.get("additionalContext", ""),
            secrets=result.get("secrets", []),
        )
