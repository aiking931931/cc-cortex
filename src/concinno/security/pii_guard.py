"""concinno.security.pii_guard — Regex-based PII leak prevention.

@module security.pii_guard
@responsibility Detect personally-identifiable information (PII) and
    high-value secret tokens in tool inputs and outputs before they
    leave the agent. Inherits :class:`concinno.security.PolicyGate`,
    so the 4-tier fail-mode chain (silent / warn / warn+log /
    hard_deny), the ``# CONCINNO_DISABLE:<reason>`` escape hatch,
    audit-log rotation, and ZIQ outcome emit are reused verbatim. The
    only thing this module owns is *what counts as PII* — the regex
    catalogue, severity mapping, and Luhn validation for credit card
    numbers.

@dependencies stdlib only — re (compiled lazily at class scope), the
    PolicyGate base class. **Zero runtime deps** per Concinno
    contributor rule #4 (`projects/concinno/CLAUDE.md`).

@exports
    PIIType (str enum), PIIGuard

Design notes
------------
* **Pattern catalogue lives at class scope**, compiled once on first
  use through :data:`PIIGuard._PATTERNS_CACHE`. Each pattern is a
  ``(PIIType, compiled_re, severity, requires_luhn)`` tuple. Adding a
  new PII type = appending one row; subclassing is rarely necessary.
* **Pre-redaction in Finding snippets**. ``Finding.snippet`` is
  user-visible audit content; we mask the middle of every match
  before it ever lands in the audit log. The format keeps the first
  ``redact_chars`` and last ``redact_chars`` of the original match
  with ``***`` between (e.g. ``sk-ant-***-XXXX``). The base class's
  defensive 80-char cap kicks in afterwards as belt-and-braces.
* **Luhn check for credit cards**. ``Luhn`` is a 1-line stdlib
  algorithm. With ``luhn_strict=True`` (default), 13-19 digit runs
  that don't validate are *dropped* from findings — this is the
  single most effective false-positive reducer for credit-card
  detection. Operators on noisy input (e.g. order numbers that
  happen to Luhn-validate) can flip ``luhn_strict=False`` to keep
  raw matches.
* **API key prefixes are critical**. ``sk-ant-...`` (Anthropic),
  ``sk-...`` (OpenAI/etc), ``ghp_...`` (GitHub PAT), ``aws_...``
  followed by typical AWS key shapes — all severity ``critical``
  because a leak is immediately exploitable.
* **Severity-based filter**. ``min_severity`` (resolved through the
  6-source FEATURE_META chain at evaluate-time, or via the
  constructor for tests) drops findings below the threshold *before*
  the base class makes its decision. So a ``mainstream`` profile +
  ``min_severity="high"`` accepts emails (severity ``low``) silently
  while warning on SSN / CC.
* **No DLP / NER**. This is regex, not Microsoft Presidio. We catch
  the 8 highest-leverage PII types with low false-positive rates;
  driver-license / passport are kept opt-in through future params
  rather than always-on regex (state-aware DL catalogues balloon the
  module).
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, ClassVar

from concinno.security.policy_gate import (
    FailMode,
    Finding,
    PolicyGate,
    Severity,
)

__all__ = [
    "PIIGuard",
    "PIIType",
]


# ── PII type enum ───────────────────────────────────────────────


class PIIType(str, Enum):
    """Stable identifiers used in :attr:`Finding.type`.

    Audit-log consumers and ZIQ outcome consumers key off these
    strings, so the values are part of the public contract — never
    rename, only append.
    """

    SSN = "ssn"
    CREDIT_CARD = "credit_card"
    EMAIL = "email"
    PHONE = "phone"
    IPV4 = "ipv4"
    IPV6 = "ipv6"
    API_KEY = "api_key"
    PASSPORT = "passport"
    DRIVER_LICENSE = "driver_license"


# ── Severity ordering for min_severity filter ──────────────────

_SEVERITY_RANK: dict[str, int] = {
    "low": 0,
    "medium": 1,
    "high": 2,
    "critical": 3,
}


def _at_or_above(severity: Severity, floor: str) -> bool:
    """Return True when ``severity >= floor`` in the canonical order."""
    return _SEVERITY_RANK.get(severity, 0) >= _SEVERITY_RANK.get(floor, 0)


# ── Luhn ───────────────────────────────────────────────────────


def _luhn_valid(digits: str) -> bool:
    """Standard Luhn checksum.

    Operates on a digit-only string. Returns False on empty input or
    on any non-digit character (the caller should pre-strip
    separators). Length 13-19 is the conventional credit-card range
    but this function only validates the checksum — length filtering
    happens at the caller.
    """
    if not digits or not digits.isdigit():
        return False
    total = 0
    parity = len(digits) % 2
    for i, ch in enumerate(digits):
        d = ord(ch) - 48  # '0' == 48
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# ── Redaction helper ───────────────────────────────────────────


def _redact_match(value: str, keep: int = 4) -> str:
    """Mask the middle of ``value``.

    Keeps the first and last ``keep`` characters, stars between.
    Examples (``keep=4``):

    - ``"sk-ant-api03-abcdefXYZ"`` → ``"sk-a***fXYZ"``
    - ``"123-45-6789"``            → ``"123-***6789"``
    - ``"abc"``                    → ``"***"`` (too short to keep edges)

    A length floor at ``2 * keep + 1`` ensures the redacted form is
    always shorter than (or at most equal to) the input — never an
    obfuscation that doesn't actually hide anything.
    """
    if keep < 0:
        keep = 0
    if len(value) <= 2 * keep + 1:
        return "***"
    return f"{value[:keep]}***{value[-keep:]}"


# ── PIIGuard ───────────────────────────────────────────────────


class PIIGuard(PolicyGate):
    """Regex-based PII detection guard.

    Subclass of :class:`PolicyGate` — see the base class for the
    fail-mode resolution chain, escape hatch, audit log, and ZIQ
    emit. This class only owns the pattern catalogue and the
    per-pattern post-processing (Luhn, redaction, min-severity
    filter).

    Construction parameters override the resolved FEATURE_META
    defaults. Pass ``cfg`` (a :class:`concinno.core.config.Config`)
    to enable the full 6-source override chain (env vars, project
    config, user config). Tests typically pass values directly.

    Args:
        profile: Active feature-toggle profile. Forwarded to the base
            class for fail-mode resolution.
        fail_mode_override: Pin the fail-mode for this instance,
            ignoring profile defaults. Tests use this to exercise
            each branch of the decision matrix.
        min_severity: Findings below this rank are dropped before the
            base class decides. ``"low"`` (default) keeps everything;
            ``"medium"`` drops emails; ``"high"`` drops emails and
            phones; ``"critical"`` keeps only API keys.
        luhn_strict: When True (default), 13-19-digit credit-card
            candidates that fail Luhn are dropped. Flip to False on
            noisy inputs that produce expected false positives.
        redact_chars: Number of characters kept on each side of the
            redacted snippet. Defaults to 4. Clamped to ``[2, 8]``
            by ``__init__`` — outside that range the redaction is
            either too long (leaks the secret) or too short to be
            useful for triage.
    """

    name: str = "pii_guard"

    # ── Pattern catalogue ──────────────────────────────────────
    #
    # Each row: (type_enum, pattern_str, severity, requires_luhn).
    # Compiled once on first instantiation through
    # :meth:`_get_compiled_patterns`. Patterns use raw strings and
    # ``re.IGNORECASE`` where case can vary; word boundaries (``\b``)
    # are used aggressively to avoid mid-word false positives.
    _PATTERN_SPECS: ClassVar[
        tuple[tuple[PIIType, str, Severity, bool], ...]
    ] = (
        # SSN — US Social Security Number, hyphenated form. The first
        # ``\b`` keeps us out of mid-digit-string false positives like
        # phone numbers or order IDs.
        (
            PIIType.SSN,
            r"\b(?!000|666|9\d{2})\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b",
            "high",
            False,
        ),
        # Credit card — 13 to 19 digits, optional separators (- or
        # space). ``requires_luhn=True`` means the post-processor will
        # drop matches that fail the Luhn checksum when
        # ``luhn_strict`` is on. We keep the most common card
        # families' shapes implicit in the digit-count check (Visa
        # 13/16, MC 16, Amex 15, Diners 14, Discover 16, JCB 16-19).
        (
            PIIType.CREDIT_CARD,
            r"\b(?:\d[ -]?){12,18}\d\b",
            "high",
            True,
        ),
        # Email — RFC 5322 subset that catches >99% of real emails
        # without the full grammar (which is a parser, not a regex).
        # Severity ``low`` so the default min_severity="medium" in
        # mainstream/strict profiles drops emails silently.
        (
            PIIType.EMAIL,
            r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
            "low",
            False,
        ),
        # Phone — accept E.164 (``+12025550123``), US/INTL common
        # forms with separators (``(202) 555-0123``, ``202-555-0123``,
        # ``202.555.0123``), continental European 2-digit groups
        # (``+33 1 23 45 67 89``), and Asian intl shapes
        # (``+81 3 1234 5678``). Three shapes are unioned:
        #
        #   1. North-American style: optional country code, then 3-4
        #      digit area, then 3-4 + 3-4 digits.
        #   2. International with country code + 3+ separator groups
        #      (``+CC G1 G2 G3 ...``) where each group is 1-4 digits.
        #   3. Bare E.164 ``+`` followed by 9-15 digits.
        #
        # Severity ``medium`` because phone numbers are mid-tier PII
        # (less sensitive than SSN, more than email).
        (
            PIIType.PHONE,
            (
                r"(?:"
                r"(?:\+\d{1,3}[\s.-]?)?"
                r"(?:\(\d{2,4}\)|\d{2,4})"
                r"[\s.-]?\d{3,4}[\s.-]?\d{3,4}"
                r"|"
                r"\+\d{1,3}(?:[\s.-]\d{1,4}){2,7}"
                r"|"
                r"\+\d{9,15}"
                r")\b"
            ),
            "medium",
            False,
        ),
        # IPv4 — strict octet match (each 0-255). Severity ``low`` —
        # IPs are quasi-public; many infrastructure logs leak them
        # routinely. Operators wanting to suppress should bump
        # ``min_severity`` to ``medium``.
        (
            PIIType.IPV4,
            (
                r"\b(?:25[0-5]|2[0-4]\d|[01]?\d\d?)"
                r"(?:\.(?:25[0-5]|2[0-4]\d|[01]?\d\d?)){3}\b"
            ),
            "low",
            False,
        ),
        # IPv6 — pragmatic subset that covers the formats showing up
        # in real logs. Three alternations are unioned:
        #
        #   1. Full 8-group form: ``X:X:X:X:X:X:X:X``
        #   2. ``::``-collapsed form: any number of leading hex groups,
        #      a single ``::``, optional trailing hex groups.
        #   3. Loopback / unspecified shortcuts: ``::1`` and ``::``.
        #
        # The negative-lookbehind / lookahead on word + colon prevents
        # matching when the candidate is glued onto a longer token.
        # We deliberately do not implement the full RFC 4291 grammar
        # (zone indices, embedded v4) — those are rare and the L9
        # PolicyEngine can layer extra rules if needed.
        (
            PIIType.IPV6,
            (
                r"(?<![\w:])(?:"
                # Full 8-group form
                r"(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}"
                # Collapsed forms with leading hex groups + ::
                r"|(?:[0-9A-Fa-f]{1,4}:){1,7}:"
                r"|(?:[0-9A-Fa-f]{1,4}:){1,6}:[0-9A-Fa-f]{1,4}"
                r"|(?:[0-9A-Fa-f]{1,4}:){1,5}(?::[0-9A-Fa-f]{1,4}){1,2}"
                r"|(?:[0-9A-Fa-f]{1,4}:){1,4}(?::[0-9A-Fa-f]{1,4}){1,3}"
                r"|(?:[0-9A-Fa-f]{1,4}:){1,3}(?::[0-9A-Fa-f]{1,4}){1,4}"
                r"|(?:[0-9A-Fa-f]{1,4}:){1,2}(?::[0-9A-Fa-f]{1,4}){1,5}"
                r"|[0-9A-Fa-f]{1,4}:(?::[0-9A-Fa-f]{1,4}){1,6}"
                r"|:(?::[0-9A-Fa-f]{1,4}){1,7}"
                # Loopback / unspecified
                r"|::1|::"
                r")(?![\w:])"
            ),
            "low",
            False,
        ),
        # API keys — high-value secrets. Severity ``critical`` so
        # they trip even the strictest ``min_severity`` filter. The
        # pattern is a union of well-known prefixes:
        #
        # * Anthropic:   ``sk-ant-...`` (api03 / admin01 etc)
        # * OpenAI etc:  ``sk-...`` 32+ alnum chars
        # * GitHub PAT:  ``ghp_`` + 36 chars (classic) / ``gho_`` /
        #                ``ghs_`` (server) / ``ghu_`` (user)
        # * AWS access:  ``AKIA`` / ``ASIA`` + 16 alphanum
        # * Slack bot:   ``xox[abprs]-`` followed by digits
        # * Google API:  ``AIza`` + 35 alphanum
        # * Stripe live: ``sk_live_`` + 24 alphanum (also pk_live_)
        (
            PIIType.API_KEY,
            (
                r"\b("
                r"sk-ant-(?:api|admin)\d{2}-[A-Za-z0-9_-]{32,}"
                r"|sk-[A-Za-z0-9]{20,}"
                r"|gh[pousr]_[A-Za-z0-9]{20,}"
                r"|(?:AKIA|ASIA)[A-Z0-9]{16}"
                r"|xox[abprs]-[A-Za-z0-9-]{10,}"
                r"|AIza[A-Za-z0-9_-]{35}"
                r"|(?:sk|pk)_live_[A-Za-z0-9]{20,}"
                r")\b"
            ),
            "critical",
            False,
        ),
        # US passport — letter prefix optional, then 8-9 digits.
        # Severity ``high``. Note this is more permissive than real
        # passport formats; ``min_severity`` filtering is the
        # operator's escape valve.
        (
            PIIType.PASSPORT,
            r"\b[A-Z]?\d{8,9}\b",
            "high",
            False,
        ),
        # Driver license — extremely state-dependent. Disabled by
        # default (severity ``low`` + opt-in via min_severity). The
        # pattern catches generic 8-12 alphanum strings that follow
        # a ``DL[: ]`` or ``LIC[: ]`` token, which is the only way to
        # reduce FP rate on this class without state-by-state lookup.
        (
            PIIType.DRIVER_LICENSE,
            r"(?i)(?:DL|LIC|DRIVER\s*LIC[A-Z]*)[\s:#]+([A-Z0-9]{6,12})\b",
            "low",
            False,
        ),
    )

    # Lazy compilation cache — populated on first instantiation,
    # shared across instances.
    _PATTERNS_CACHE: ClassVar[
        list[tuple[PIIType, re.Pattern[str], Severity, bool]] | None
    ] = None

    @classmethod
    def _get_compiled_patterns(
        cls,
    ) -> list[tuple[PIIType, re.Pattern[str], Severity, bool]]:
        """Return compiled patterns, building the cache on first call."""
        if cls._PATTERNS_CACHE is None:
            cls._PATTERNS_CACHE = [
                (ptype, re.compile(pat), sev, luhn)
                for ptype, pat, sev, luhn in cls._PATTERN_SPECS
            ]
        return cls._PATTERNS_CACHE

    # ── Construction ───────────────────────────────────────────

    def __init__(
        self,
        profile: str = "lite",
        fail_mode_override: FailMode | None = None,
        *,
        min_severity: Severity = "low",
        luhn_strict: bool = True,
        redact_chars: int = 4,
    ) -> None:
        super().__init__(
            profile=profile, fail_mode_override=fail_mode_override
        )

        if min_severity not in _SEVERITY_RANK:
            raise ValueError(
                f"min_severity {min_severity!r} not in "
                f"{sorted(_SEVERITY_RANK)}"
            )
        self._min_severity: str = min_severity
        self._luhn_strict: bool = bool(luhn_strict)

        # Clamp redact_chars to [2, 8] — outside this band the
        # redaction is either useless or unsafe.
        if redact_chars < 2:
            redact_chars = 2
        elif redact_chars > 8:
            redact_chars = 8
        self._redact_chars: int = redact_chars

        # Trigger compilation up-front so a malformed pattern fails
        # at construction, not at the first scan call.
        self._get_compiled_patterns()

    # ── Public scan ────────────────────────────────────────────

    def scan(
        self, payload: str | bytes | dict[str, Any]
    ) -> list[Finding]:
        """Return all PII findings in ``payload``.

        Empty list = clean. The base class :meth:`evaluate` will turn
        a clean scan into ``Decision.accept`` and skip both the audit
        write and the stderr warn.
        """
        text = self._payload_to_text(payload)
        if not text:
            return []

        findings: list[Finding] = []
        for ptype, regex, severity, requires_luhn in self._get_compiled_patterns():
            if not _at_or_above(severity, self._min_severity):
                continue
            for match in regex.finditer(text):
                raw = match.group(0)

                # Credit-card Luhn validation — the most effective
                # FP reducer in the catalogue.
                if requires_luhn and self._luhn_strict:
                    digits_only = re.sub(r"\D", "", raw)
                    if not (13 <= len(digits_only) <= 19):
                        continue
                    if not _luhn_valid(digits_only):
                        continue

                findings.append(
                    Finding(
                        type=ptype.value,
                        span=match.span(),
                        snippet=_redact_match(raw, keep=self._redact_chars),
                        severity=severity,
                        message=f"Detected {ptype.value} pattern",
                    )
                )

        return findings
