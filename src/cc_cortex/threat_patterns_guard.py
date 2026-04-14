"""cc_cortex.threat_patterns_guard — Extended threat detection (Aegis port).

@module threat_patterns_guard
@responsibility Detect threat categories not covered by the existing
    prompt_injection_guard / exfil_guard / destruction_guard trio:
    harmful_content, social_engineering, persona_hijack, role_switch,
    no_restrictions_mode, and context_reset_attack.
@dependencies cc_cortex.guards.base
@exports ThreatPatternsGuard

Ported from Aegis (persona-api/src/persona/guards.py:33-129) as part
of CCC 1.5.0. Aegis had 9 regex patterns; CCC already covered 3 of
them, so this module carries the remaining 6. Zero runtime deps
(stdlib regex only).
"""

from __future__ import annotations

import re
from typing import Optional

from cc_cortex.guards.base import (
    BaseGuard,
    GuardCategory,
    GuardContext,
    GuardResult,
)

_THREAT_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    (
        "harmful_content",
        re.compile(
            r"(?:how\s+to\s+(?:make|build|create)\s+(?:a\s+)?"
            r"(?:bomb|weapon|explosive|drug|poison)"
            r"|synthesize\s+(?:meth|fentanyl|sarin))",
            re.IGNORECASE,
        ),
        "Blocked: harmful content request (weapons / drugs / poisons).",
    ),
    (
        "social_engineering",
        re.compile(
            r"(?:pretend\s+you\s+have\s+no\s+"
            r"(?:rules|restrictions|limits)"
            r"|act\s+as\s+(?:if|though)\s+you\s+"
            r"(?:can|have)\s+no\s+(?:filter|bound))",
            re.IGNORECASE,
        ),
        "Blocked: social engineering attempt (remove restrictions).",
    ),
    (
        "persona_hijack",
        re.compile(
            r"(?:(?:forget|stop|quit|drop|abandon|leave)"
            r"\s+(?:the\s+|that\s+|about\s+)?"
            r"(?:cooking|previous|last|current|old|that"
            r"|the\s+recipe|your\s+(?:role|persona))"
            r"[^.]{0,200}?"
            r"(?:you(?:'re|\s+are)\s+(?:now\s+)?(?:a|an|my)"
            r"|act\s+as|pretend|new\s+(?:role|persona"
            r"|identity|character)))",
            re.IGNORECASE | re.DOTALL,
        ),
        "Blocked: persona hijack attempt (role/identity switch).",
    ),
    (
        "role_switch_attack",
        re.compile(
            r"(?:you(?:'re|\s+are)\s+(?:now\s+)?(?:a|an|my)\s+"
            r"(?:hacking|exploit|attack|malware|phishing"
            r"|weapons?|drug|bomb|harassment|stalker)\s+"
            r"(?:instructor|teacher|expert|advisor|guide"
            r"|coach|assistant))",
            re.IGNORECASE,
        ),
        "Blocked: role-switch attack (malicious role assignment).",
    ),
    (
        "no_restrictions_mode",
        re.compile(
            r"(?:(?:new|switch|change)\s+"
            r"(?:game|mode|role|persona|task|assignment)"
            r"[^.]{0,100}?"
            r"(?:no\s+(?:restrictions?|rules?|limits?|safety"
            r"|filter|guard|bound)"
            r"|unrestricted|anything\s+goes|without\s+(?:filter"
            r"|limit|rule|restriction)))",
            re.IGNORECASE | re.DOTALL,
        ),
        "Blocked: no-restrictions mode request.",
    ),
    (
        "context_reset_attack",
        re.compile(
            r"(?:\[SYSTEM:\s*(?:maintenance|debug|dev|admin)"
            r".*?(?:no\s+safety|ignore|bypass|disable))"
            r"|(?:<\|(?:system|im_start)\|>\s*"
            r"(?:you\s+are|system|new\s+instruct))",
            re.IGNORECASE | re.DOTALL,
        ),
        "Blocked: context-reset attack (fake system directive).",
    ),
]


class ThreatPatternsGuard(BaseGuard):
    """Detect extended threat categories via Aegis-ported regex.

    Scans ``tool_input`` text (Bash commands, Write content, Edit
    strings) for six threat categories that the existing CCC guards
    do not cover. Returns DENY with a specific threat-type reason.

    Registered in the QUALITY layer (not SECURITY) so the step-back
    middleware lets legitimate security research / CTF / education
    contexts proceed after acknowledgment.
    """

    name = "threat_patterns"
    category = GuardCategory.QUALITY
    step_back_reason = "potential threat pattern detected"
    path_scope: list[str] = []

    def check(self, ctx: GuardContext) -> Optional[GuardResult]:
        text = _extract_scannable_text(ctx)
        if not text:
            return None

        for threat_type, pattern, reason in _THREAT_PATTERNS:
            match = pattern.search(text)
            if match:
                return GuardResult.deny(
                    reason,
                    context=(
                        f"⚠ ThreatPatternsGuard: {threat_type} "
                        f"detected — `{match.group(0)[:60]}`\n"
                        "If this is legitimate security research or "
                        "CTF, acknowledge to proceed."
                    ),
                    threat_type=threat_type,
                    matched_text=match.group(0)[:120],
                )
        return None


def _extract_scannable_text(ctx: GuardContext) -> str:
    """Pull text from tool_input that might contain threats."""
    inp = ctx.tool_input
    parts: list[str] = []

    if isinstance(inp.get("command"), str):
        parts.append(inp["command"])
    if isinstance(inp.get("content"), str):
        parts.append(inp["content"])
    if isinstance(inp.get("new_string"), str):
        parts.append(inp["new_string"])
    if isinstance(inp.get("prompt"), str):
        parts.append(inp["prompt"])
    if isinstance(inp.get("description"), str):
        parts.append(inp["description"])

    return " ".join(parts) if parts else ""
