"""concinno.security.http_client_guard — request-shape policy gate (4.6.0).

@module security.http_client_guard
@responsibility Inspect outbound HTTP-client tool calls — ``Bash`` running
    ``curl`` / ``wget`` / ``httpie``, or Python ``requests`` / ``httpx`` /
    ``aiohttp`` invocations — and flag findings against three independent
    policies:

      1. **Domain allow- / deny-list.** Domains in the operator-supplied
         denylist produce ``critical`` ``domain_denylist`` findings; the
         allowlist is informational — unknown domains stay accepted at
         severity ``low`` so the warn-mode chain only nudges, never
         blocks. The default lists are intentionally empty so an
         out-of-the-box guard is no more strict than its config.
      2. **Header sanitisation.** Authorization headers and cookies are
         pattern-matched against well-known leaked-secret prefixes
         (``Bearer ghp_``, ``sk-ant-``, AWS / GitHub / Google PATs).
         A real Bearer token in a request is a likely
         credential-exfiltration vector and earns a ``high`` finding.
      3. **Method / content-type policy.** Form-encoded POST to a
         non-allowlisted domain is the canonical browser-form-grab
         exfil pattern; ``DELETE`` / ``PUT`` against ``*.prod.*`` /
         ``*.production.*`` URLs earns a ``high`` finding because the
         blast radius is irreversible.

    The guard does **not** validate the network endpoint — that is the
    job of :mod:`concinno.security.ssrf_guard`. Both modules are wired
    into the same Layer-1 (SECURITY) stage of the guard pipeline; this
    module owns request **semantics**, SSRFGuard owns network
    **destinations**. They compose without overlap.

@dependencies stdlib only (``re``, ``shlex``, ``fnmatch``, ``dataclasses``,
    ``typing``) plus the ``PolicyGate`` base class. Zero runtime deps
    per Concinno contributor rule #4.

@exports
    HttpClientGuard, HttpClientFinding (re-export of :class:`Finding`),
    HttpRequestPayload, parse_curl_command, parse_python_http_kwargs,
    extract_payload, DEFAULT_ALLOWLIST, DEFAULT_DENYLIST,
    SECRET_HEADER_PATTERNS

Default OFF in 4.0.0 SEMVER baseline: see ``DEFAULT_OFF_4_0_0`` in
:mod:`concinno.feature_config`. Operators opt in via
``concinno features set http_client_guard enabled true`` after
populating ``~/.concinno/http_client_guard.json``.
"""

from __future__ import annotations

import fnmatch
import re
import shlex
from dataclasses import dataclass, field
from typing import Any

from concinno.security.policy_gate import (
    FailMode,
    Finding,
    PolicyGate,
    Severity,
)
from concinno.security.policy_gate import Finding as HttpClientFinding

__all__ = [
    "DEFAULT_ALLOWLIST",
    "DEFAULT_DENYLIST",
    "HttpClientFinding",
    "HttpClientGuard",
    "HttpClientPipelineGuard",
    "HttpRequestPayload",
    "PRODUCTION_HOST_PATTERNS",
    "SECRET_HEADER_PATTERNS",
    "extract_payload",
    "parse_curl_command",
    "parse_python_http_kwargs",
]

# ── Defaults ──────────────────────────────────────────────────────

#: Domains operators trust by default — empty so out-of-the-box behaviour
#: is permissive. Override by passing ``allowlist={"github.com", ...}``
#: to the constructor or via per-user config in
#: ``~/.concinno/http_client_guard.json``.
DEFAULT_ALLOWLIST: frozenset[str] = frozenset()

#: Domains hard-blocked by default. Empty for the same reason — the
#: guard ships disabled (``DEFAULT_OFF_4_0_0``) and even when enabled
#: stays permissive until the operator paints a denylist.
DEFAULT_DENYLIST: frozenset[str] = frozenset()

#: Glob patterns matched against the URL host (case-insensitive) to
#: classify production-shape destinations. ``*.prod.example`` and
#: ``api.production.acme`` both match.
PRODUCTION_HOST_PATTERNS: tuple[str, ...] = (
    "*.prod.*",
    "*.production.*",
    "prod.*",
    "production.*",
)

#: Regex patterns that match well-known leaked-secret prefixes inside
#: ``Authorization`` / ``Cookie`` / ``X-Api-Key`` headers. Pattern-only
#: — no HMAC validation; that is the dedicated secret_scan guard's job.
SECRET_HEADER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("github_pat", re.compile(r"\bghp_[A-Za-z0-9]{20,}", re.IGNORECASE)),
    ("github_oauth", re.compile(r"\bgho_[A-Za-z0-9]{20,}", re.IGNORECASE)),
    ("anthropic_api_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai_api_key", re.compile(r"\bsk-[A-Za-z0-9]{32,}")),
    ("aws_secret", re.compile(
        r"\baws_secret_access_key\s*=\s*[A-Za-z0-9/+]{30,}", re.IGNORECASE,
    )),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("bearer_token_long", re.compile(
        r"\bBearer\s+[A-Za-z0-9_\-\.]{40,}", re.IGNORECASE,
    )),
)

_FORM_CONTENT_TYPE_RE = re.compile(
    r"application/x-www-form-urlencoded", re.IGNORECASE,
)

# Methods we treat as state-changing for the production-shape check.
_DESTRUCTIVE_METHODS: frozenset[str] = frozenset({"DELETE", "PUT", "PATCH"})


# ── Payload dataclass ─────────────────────────────────────────────


@dataclass(frozen=True)
class HttpRequestPayload:
    """Parsed request shape extracted from a tool invocation.

    Attributes:
        method: Uppercase HTTP method. Empty string when the parser
            could not determine one (e.g. a bare ``curl URL`` without
            ``-X``); the guard treats empty as ``GET``.
        url: Full request URL as it appears in the source command —
            *not* normalised. The host is parsed at scan time so the
            payload stays close to the raw input for audit log fidelity.
        headers: Header name → value map. Header names are stored
            verbatim; the scanner lowercases for comparison.
        body: Request body, if visible in the parser input. ``None``
            when the parser saw no body (e.g. a GET).
        source: Origin tag — ``"curl"``, ``"wget"``, ``"requests"``,
            ``"httpx"``, etc. Used in audit metadata so operators can
            grep by client.
    """

    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: str | None = None
    source: str = ""

    @property
    def host(self) -> str:
        """Lowercased hostname extracted from :attr:`url`. Best-effort."""
        # Cheap stdlib parse — full SSRF-grade canonicalisation lives
        # in ssrf_guard.canonicalize_host.
        from urllib.parse import urlparse

        try:
            return (urlparse(self.url).hostname or "").lower()
        except (TypeError, ValueError):
            return ""


# ── Tool-input parsers ────────────────────────────────────────────


_CURL_METHOD_FLAGS = {"-X", "--request"}
_CURL_HEADER_FLAGS = {"-H", "--header"}
_CURL_DATA_FLAGS = {
    "-d", "--data", "--data-raw", "--data-binary", "--data-urlencode",
}


def parse_curl_command(cmd: str) -> HttpRequestPayload | None:
    """Parse a Bash ``curl`` / ``wget`` / ``http`` (httpie) command.

    Returns ``None`` when the command does not look like an HTTP client
    invocation. Parsing is intentionally forgiving — unknown flags are
    skipped rather than raising. Multi-command lines (``curl A && curl
    B``) are *not* split here; the caller is expected to feed each
    sub-command separately, otherwise only the first URL will surface.
    """
    if not isinstance(cmd, str) or not cmd.strip():
        return None

    try:
        tokens = shlex.split(cmd, posix=True)
    except ValueError:
        return None
    if not tokens:
        return None

    # Locate the actual client binary, skipping leading env / sudo prefixes.
    client = ""
    start = 0
    for i, tok in enumerate(tokens):
        bare = tok.rsplit("/", 1)[-1]
        if bare in {"curl", "wget", "http", "https", "httpie"}:
            client = "curl" if bare == "curl" else (
                "wget" if bare == "wget" else "httpie"
            )
            start = i + 1
            break
    if not client:
        return None

    method = ""
    url = ""
    headers: dict[str, str] = {}
    body_parts: list[str] = []

    i = start
    while i < len(tokens):
        tok = tokens[i]
        if tok in _CURL_METHOD_FLAGS and i + 1 < len(tokens):
            method = tokens[i + 1].upper()
            i += 2
            continue
        if tok in _CURL_HEADER_FLAGS and i + 1 < len(tokens):
            raw = tokens[i + 1]
            if ":" in raw:
                name, _, value = raw.partition(":")
                headers[name.strip()] = value.strip()
            i += 2
            continue
        if tok in _CURL_DATA_FLAGS and i + 1 < len(tokens):
            body_parts.append(tokens[i + 1])
            if not method:
                method = "POST"
            i += 2
            continue
        if tok.startswith(("http://", "https://")):
            url = tok
            i += 1
            continue
        i += 1

    if not url:
        return None

    body = "&".join(body_parts) if body_parts else None
    return HttpRequestPayload(
        method=method or "GET",
        url=url,
        headers=headers,
        body=body,
        source=client,
    )


def parse_python_http_kwargs(tool_input: dict[str, Any]) -> HttpRequestPayload | None:
    """Parse a ``requests`` / ``httpx`` style kwargs dict.

    Recognises the canonical keys: ``method``, ``url``, ``headers``,
    ``data`` / ``json`` / ``content``. Returns ``None`` if no URL is
    present — the guard never invents one.
    """
    if not isinstance(tool_input, dict):
        return None
    url = tool_input.get("url") or tool_input.get("URL") or ""
    if not isinstance(url, str) or not url:
        return None

    method_raw = tool_input.get("method") or tool_input.get("METHOD") or "GET"
    method = str(method_raw).upper()

    raw_headers = tool_input.get("headers") or {}
    headers: dict[str, str] = {}
    if isinstance(raw_headers, dict):
        for k, v in raw_headers.items():
            if isinstance(k, str):
                headers[k] = "" if v is None else str(v)

    body: str | None = None
    for key in ("json", "data", "content", "body"):
        if key in tool_input and tool_input[key] is not None:
            body = str(tool_input[key])
            break

    return HttpRequestPayload(
        method=method,
        url=url,
        headers=headers,
        body=body,
        source=str(tool_input.get("_source") or "python_http"),
    )


def extract_payload(
    tool_name: str, tool_input: dict[str, Any],
) -> HttpRequestPayload | None:
    """Top-level dispatch: pick the right parser for the tool kind.

    ``Bash`` → ``parse_curl_command(tool_input["command"])``.
    Anything else with a ``url`` key → ``parse_python_http_kwargs``.
    Returns ``None`` when no HTTP semantics are present.
    """
    if not isinstance(tool_name, str) or not isinstance(tool_input, dict):
        return None
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        if isinstance(cmd, str):
            return parse_curl_command(cmd)
        return None
    if "url" in tool_input or "URL" in tool_input:
        return parse_python_http_kwargs(tool_input)
    return None


# ── Guard ─────────────────────────────────────────────────────────


class HttpClientGuard(PolicyGate):
    """PolicyGate that scans HTTP-client invocations for risky shapes.

    The :meth:`scan` accepts either a parsed :class:`HttpRequestPayload`
    or a raw string / dict that this class will route through
    :func:`extract_payload`. Returns at most one finding per policy axis:

      * ``domain_denylist`` (``critical``) — host on the configured
        denylist.
      * ``unknown_domain`` (``low``) — host is not on the allowlist;
        only emitted when the allowlist is non-empty so a default
        config does not warn on every fetch.
      * ``leaked_secret_header`` (``high``) — Authorization / Cookie /
        X-Api-Key value matches a well-known secret prefix.
      * ``form_post_to_unknown`` (``medium``) — form-encoded POST to a
        host that is neither on the allowlist nor an empty allowlist.
      * ``destructive_method_on_prod`` (``high``) — DELETE / PUT /
        PATCH against a ``*.prod.*`` / ``*.production.*`` host.

    Constructor parameters honour the FEATURE_META schema. Operator
    tunable values land here from :mod:`concinno.feature_config` via
    the standard Config chain (file → env → defaults).
    """

    name: str = "http_client_guard"

    def __init__(
        self,
        profile: str = "lite",
        fail_mode_override: FailMode | None = None,
        *,
        allowlist: frozenset[str] | set[str] | None = None,
        denylist: frozenset[str] | set[str] | None = None,
        production_patterns: tuple[str, ...] = PRODUCTION_HOST_PATTERNS,
        secret_severity: Severity = "high",
        denylist_severity: Severity = "critical",
    ) -> None:
        super().__init__(
            profile=profile, fail_mode_override=fail_mode_override,
        )
        self._allowlist: frozenset[str] = frozenset(
            (h or "").lower() for h in (allowlist or DEFAULT_ALLOWLIST) if h
        )
        self._denylist: frozenset[str] = frozenset(
            (h or "").lower() for h in (denylist or DEFAULT_DENYLIST) if h
        )
        self._prod_patterns: tuple[str, ...] = tuple(
            p.lower() for p in production_patterns
        )
        if secret_severity not in {"low", "medium", "high", "critical"}:
            raise ValueError(
                f"secret_severity {secret_severity!r} invalid"
            )
        if denylist_severity not in {"low", "medium", "high", "critical"}:
            raise ValueError(
                f"denylist_severity {denylist_severity!r} invalid"
            )
        self._secret_severity: Severity = secret_severity
        self._denylist_severity: Severity = denylist_severity

    # ── PolicyGate hook ────────────────────────────────────────────

    def scan(
        self, payload: str | bytes | dict[str, Any] | HttpRequestPayload,
    ) -> list[Finding]:
        """Return findings for the parsed request, or ``[]`` if clean."""
        request = self._coerce_payload(payload)
        if request is None:
            return []

        findings: list[Finding] = []
        host = request.host

        # 1. Domain policy
        if host and host in self._denylist:
            findings.append(Finding(
                type="domain_denylist",
                span=(0, len(request.url)),
                snippet=request.url[:80],
                severity=self._denylist_severity,
                message=f"host {host!r} on operator denylist",
            ))
        elif host and self._allowlist and host not in self._allowlist:
            findings.append(Finding(
                type="unknown_domain",
                span=(0, len(request.url)),
                snippet=request.url[:80],
                severity="low",
                message=f"host {host!r} not on allowlist",
            ))

        # 2. Header sanitisation — only inspect sensitive header families.
        for hdr_name, hdr_val in request.headers.items():
            lname = hdr_name.lower()
            if lname not in {"authorization", "cookie", "x-api-key"}:
                continue
            for kind, pat in SECRET_HEADER_PATTERNS:
                if pat.search(hdr_val):
                    findings.append(Finding(
                        type="leaked_secret_header",
                        span=(-1, -1),
                        snippet=f"{hdr_name}: ***",
                        severity=self._secret_severity,
                        message=f"{kind} prefix detected in {hdr_name}",
                    ))
                    break

        # 3. Form POST to non-allowlisted host (data exfil shape).
        ct = request.headers.get("Content-Type") or request.headers.get(
            "content-type", "",
        )
        if (
            request.method == "POST"
            and _FORM_CONTENT_TYPE_RE.search(ct or "")
            and host
            and self._allowlist
            and host not in self._allowlist
        ):
            findings.append(Finding(
                type="form_post_to_unknown",
                span=(0, len(request.url)),
                snippet=request.url[:80],
                severity="medium",
                message=(
                    "form-encoded POST to host outside allowlist "
                    "(possible exfil)"
                ),
            ))

        # 4. Destructive method on production-shape host.
        if request.method in _DESTRUCTIVE_METHODS and self._is_production(host):
            findings.append(Finding(
                type="destructive_method_on_prod",
                span=(0, len(request.url)),
                snippet=f"{request.method} {request.url[:60]}",
                severity="high",
                message=(
                    f"{request.method} against production-shape host {host!r}"
                ),
            ))

        return findings

    # ── Helpers ────────────────────────────────────────────────────

    def _is_production(self, host: str) -> bool:
        if not host:
            return False
        return any(fnmatch.fnmatch(host, p) for p in self._prod_patterns)

    @staticmethod
    def _coerce_payload(
        payload: str | bytes | dict[str, Any] | HttpRequestPayload,
    ) -> HttpRequestPayload | None:
        """Accept the four shapes :meth:`scan` is documented to take."""
        if isinstance(payload, HttpRequestPayload):
            return payload
        if isinstance(payload, str):
            return parse_curl_command(payload)
        if isinstance(payload, bytes):
            try:
                return parse_curl_command(payload.decode("utf-8", "replace"))
            except (UnicodeError, ValueError):
                return None
        if isinstance(payload, dict):
            # Two acceptable dict shapes:
            #   {"tool_name": "...", "tool_input": {...}}     ← hook ctx
            #   {"url": "...", "method": "..."}               ← raw kwargs
            if "tool_name" in payload and "tool_input" in payload:
                return extract_payload(
                    str(payload.get("tool_name", "")),
                    payload.get("tool_input") or {},
                )
            return parse_python_http_kwargs(payload)
        return None


# ── Pipeline adapter ──────────────────────────────────────────────
#
# The pipeline (concinno.guards.pipeline) consumes BaseGuard
# instances; PolicyGate is its own world (richer fail-mode chain,
# audit log, ZIQ emit). The adapter below bridges the two: it owns a
# private HttpClientGuard and translates its scan output into the
# BaseGuard ALLOW/DENY contract. No new logic — the policy is owned
# by HttpClientGuard, the adapter is dispatch only.

_SEV_RANK: dict[str, int] = {
    "low": 0, "medium": 1, "high": 2, "critical": 3,
}

# Imported eagerly here. The pipeline modules already pull in
# concinno.security at import time (see guards.registry), so there is
# no cycle risk; the lazy form would only obscure failures.
from concinno.guards.base import (  # noqa: E402
    BaseGuard,
    GuardCategory,
    GuardContext,
    GuardResult,
)


class HttpClientPipelineGuard(BaseGuard):
    """BaseGuard adapter wiring HttpClientGuard into the pipeline.

    Registered in :func:`concinno.guards.registry._register_security`
    so PreToolUse hooks invoke this guard before the request leaves
    the agent. The default-OFF feature flag in
    :mod:`concinno.feature_config` keeps the guard quiet until an
    operator opts in.

    A ``critical`` finding becomes a hard DENY; ``high`` and lower
    findings ALLOW with a finding-summary additionalContext block so
    the LLM sees the warning without losing the call.
    """

    name: str = "http_client_guard"
    category: GuardCategory = GuardCategory.SECURITY
    feature_name: str = "http_client_guard"

    def __init__(self, inner: HttpClientGuard | None = None) -> None:
        self._inner = inner or HttpClientGuard()

    def check(self, ctx: GuardContext) -> GuardResult | None:
        """Run the inner guard against ``ctx.tool_input``.

        Returns ``None`` (pass through) when there is no HTTP shape to
        scan, ``GuardResult.deny(...)`` on critical, or
        ``GuardResult.allow(context=...)`` for sub-critical findings.
        """
        request = extract_payload(ctx.tool_name, ctx.tool_input)
        if request is None:
            return None

        # PolicyGate.evaluate signature accepts str | bytes | dict; we
        # serialise the parsed request to a dict that round-trips
        # through HttpClientGuard._coerce_payload via the
        # parse_python_http_kwargs branch.
        request_dict: dict[str, Any] = {
            "url": request.url,
            "method": request.method,
            "headers": dict(request.headers),
        }
        if request.body is not None:
            request_dict["body"] = request.body
        result = self._inner.evaluate(request_dict)
        if not result.findings:
            return None

        # Decide DENY vs ALLOW from the worst-severity finding.
        worst = max(result.findings, key=lambda f: _SEV_RANK[f.severity])
        summary_lines = [
            f"⚠ [http_client_guard] {f.type}: {f.message}"
            for f in result.findings
        ]
        summary = "\n".join(summary_lines)

        if worst.severity == "critical" or result.decision == "deny":
            return GuardResult.deny(
                reason=f"http_client_guard: {worst.type}",
                context=summary,
            )
        return GuardResult.allow(context=summary)
