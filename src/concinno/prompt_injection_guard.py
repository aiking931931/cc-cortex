"""concinno.prompt_injection_guard — NLP-level prompt injection detection.

@module prompt_injection_guard
@responsibility Detect jailbreak attempts, prompt injection, identity override,
    and encoded attacks in free-text messages. Designed for A2A agent context
    where attacks arrive as natural language, not structured tool calls.
@dependencies concinno.guards.base
@exports PromptInjectionGuard
"""

from __future__ import annotations

import base64
import re

from concinno.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

# ── Jailbreak patterns ────────────────────────────────────────

_JAILBREAK_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # DAN / unrestricted mode
    (
        "DAN jailbreak",
        re.compile(
            r"(?:you\s+are\s+(?:now\s+)?(?:DAN|evil|unrestricted|unfiltered|jailbroken))"
            r"|(?:Do\s+Anything\s+Now)"
            r"|(?:act\s+as\s+(?:an?\s+)?(?:unrestricted|unfiltered|evil|hacker))",
            re.IGNORECASE,
        ),
    ),
    # Ignore previous instructions
    (
        "instruction override",
        re.compile(
            r"(?:ignore|forget|disregard|override|bypass|skip)\s+"
            r"(?:all\s+)?(?:previous|prior|above|system|original|your)\s+"
            r"(?:instructions?|rules?|guidelines?|constraints?|prompts?"
            r"|programming|tasks?|directives?|commands?)",
            re.IGNORECASE,
        ),
    ),
    # System prompt extraction
    (
        "system prompt extraction",
        re.compile(
            r"(?:output|show|display|reveal|print|repeat|echo)\s+"
            r"(?:your\s+)?(?:system\s+prompt|instructions?|rules?|initial\s+prompt"
            r"|configuration|system\s+message)",
            re.IGNORECASE,
        ),
    ),
    # Roleplay attacks
    (
        "roleplay jailbreak",
        re.compile(
            r"(?:let'?s?\s+(?:play|pretend|imagine|roleplay|role-play))"
            r".*(?:hacker|Shadow|villain|evil|malicious|unrestricted"
            r"|no\s+(?:rules?|restrictions?|limits?|boundaries))",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    # Without restrictions / limits
    (
        "restriction removal",
        re.compile(
            r"(?:free\s+from|without|remove|disable|turn\s+off)\s+"
            r"(?:all\s+)?(?:restrictions?|limitations?|filters?|safeguards?"
            r"|safety|guardrails?|boundaries|rules?|constraints?)",
            re.IGNORECASE,
        ),
    ),
    # Token smuggling / delimiter injection
    (
        "delimiter injection",
        re.compile(
            r"(?:<\|(?:im_start|system|endoftext|pad)\|>)"
            r"|(?:\[(?:INST|SYS|/INST|/SYS)\])"
            r"|(?:<<SYS>>|<</SYS>>)"
            r"|(?:###\s*(?:System|Human|Assistant)\s*:)",
            re.IGNORECASE,
        ),
    ),
]

# ── Exfiltration intent in natural language ───────────────────

_EXFIL_NLP_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "NLP exfiltration",
        re.compile(
            r"(?:send|upload|post|transmit|exfiltrate|forward|transfer)\s+"
            r"(?:its?\s+)?(?:contents?|data|file|output|results?)\s+"
            r"(?:to|at|via)\s+(?:https?://|ftp://|webhook)",
            re.IGNORECASE,
        ),
    ),
    (
        "read and send secrets",
        re.compile(
            r"(?:read|cat|open|access|get)\s+"
            r"(?:the\s+)?(?:\.env|credentials|secret|password|api[_\s-]?key|token|config)"
            r".*(?:send|upload|post|curl|wget|http)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
]

# ── Identity override patterns ────────────────────────────────

_IDENTITY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "identity rewrite",
        re.compile(
            r"(?:edit|modify|change|rewrite|replace|overwrite|update)\s+"
            r"(?:the\s+)?(?:CLAUDE\.md|system\s+prompt|rules?|settings?"
            r"|identity|configuration|instructions?)",
            re.IGNORECASE,
        ),
    ),
    (
        "new identity assignment",
        re.compile(
            r"(?:you\s+are\s+(?:no\s+longer|not)\s+(?:Claude|an?\s+AI|an?\s+assistant))"
            r"|(?:your\s+new\s+(?:identity|name|role|purpose)\s+is)",
            re.IGNORECASE,
        ),
    ),
]

# ── Encoded payload detection ─────────────────────────────────

_ENCODING_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "base64 decode and execute",
        re.compile(
            r"(?:base64\s+(?:-d|--decode|decode))"
            r"|(?:echo\s+[A-Za-z0-9+/=]{20,}\s*\|\s*(?:base64|python|bash|sh|eval))"
            r"|(?:atob\s*\()",
            re.IGNORECASE,
        ),
    ),
    (
        "hex decode execution",
        re.compile(
            r"(?:xxd\s+-r)"
            r"|(?:python[23]?\s+-c\s+.*(?:decode|unhexlify|fromhex))",
            re.IGNORECASE,
        ),
    ),
]

# ── Secret access in natural language ─────────────────────────

_SECRET_ACCESS_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "secret file access",
        re.compile(
            r"(?:cat|read|head|tail|less|more|type|get-content|wget\s+--post-file=)\s*"
            r"(?:.*[\\/])?(?:\.env|credentials\.json|id_rsa|id_ed25519"
            r"|\.key|\.pem|\.pfx|\.p12|/etc/shadow|/etc/passwd"
            r"|kubeconfig|\.aws/credentials|\.ssh/)",
            re.IGNORECASE,
        ),
    ),
    (
        "environment variable leak",
        re.compile(
            r"(?:echo|print|env|export|set)\s+.*(?:\$\{?(?:API_KEY|SECRET|TOKEN"
            r"|PASSWORD|CREDENTIALS|AWS_SECRET|ANTHROPIC_API_KEY|OPENAI_API_KEY))",
            re.IGNORECASE,
        ),
    ),
]

# ── Bonus: detect base64-encoded malicious payloads ───────────


def _check_base64_payload(text: str) -> str | None:
    """Try to decode base64 strings and check for dangerous content."""
    b64_re = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")
    for match in b64_re.finditer(text):
        try:
            decoded = base64.b64decode(match.group()).decode("utf-8", errors="ignore")
        except Exception:
            continue
        # Check decoded content for dangerous patterns
        danger_patterns = [
            r"rm\s+-rf",
            r"os\.system",
            r"subprocess",
            r"eval\(",
            r"exec\(",
            r"__import__",
            r"/etc/passwd",
            r"/etc/shadow",
            r"curl.*-[FdT]",
            r"wget.*--post",
        ]
        for dp in danger_patterns:
            if re.search(dp, decoded, re.IGNORECASE):
                return f"Decoded base64 contains dangerous pattern: {dp}"
    return None


# ── Guard class ───────────────────────────────────────────────


class PromptInjectionGuard(BaseGuard):
    """NLP-level prompt injection and jailbreak detection.

    Scans free-text content (Bash commands, Write content, tool results)
    for jailbreak patterns, exfiltration intent, identity override,
    encoding evasion, and secret access attempts.
    """

    name = "prompt_injection_guard"
    category = GuardCategory.SECURITY

    def check(self, ctx: GuardContext) -> GuardResult | None:
        """Scan tool input text for prompt injection patterns.

        Checks Bash commands and Write content for NLP-level attacks
        that structured guards might miss.
        """
        text = self._extract_text(ctx)
        if not text or len(text) < 10:
            return None

        # Run all pattern categories
        all_patterns = [
            *_JAILBREAK_PATTERNS,
            *_EXFIL_NLP_PATTERNS,
            *_IDENTITY_PATTERNS,
            *_ENCODING_PATTERNS,
            *_SECRET_ACCESS_PATTERNS,
        ]

        triggers: list[str] = []
        for name, pattern in all_patterns:
            if pattern.search(text):
                triggers.append(name)

        # Check base64 payloads
        b64_result = _check_base64_payload(text)
        if b64_result:
            triggers.append(f"encoded payload: {b64_result}")

        if not triggers:
            return None

        trigger_list = ", ".join(triggers[:5])
        return GuardResult.deny(
            f"🛡️ Prompt injection detected: {trigger_list}. "
            "Input contains patterns associated with jailbreak, "
            "identity override, or data exfiltration attempts.",
            triggers=triggers,
        )

    def _extract_text(self, ctx: GuardContext) -> str:
        """Extract scannable text from any tool context."""
        parts: list[str] = []

        if ctx.tool_name == "Bash":
            cmd = ctx.tool_input.get("command", "")
            if cmd:
                parts.append(cmd)
        elif ctx.tool_name in ("Write", "Edit"):
            content = ctx.tool_input.get("content", "")
            new_str = ctx.tool_input.get("new_string", "")
            if content:
                parts.append(content)
            if new_str:
                parts.append(new_str)

        if ctx.tool_result:
            parts.append(ctx.tool_result)

        return "\n".join(parts)
