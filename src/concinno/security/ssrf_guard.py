"""concinno.security.ssrf_guard — Layer 7 SSRF validator (substrate shim).

@module security.ssrf_guard
@responsibility Re-export the substrate SSRF primitive that lives in
    ``lyceum.security.ssrf_guard`` since Wave 2.7-G. Concinno keeps
    this thin shim so existing callers
    (``from concinno.security.ssrf_guard import SSRFGuard``) don't
    churn while the canonical implementation lives Lyceum-side and
    stays available to non-Concinno harnesses.

Wave 2.7-G (2026-05-02) — port note:
    SSRF is the Hermes-parity Layer 7 primitive (per the original
    docstring). It's pure stdlib (ipaddress + socket + urllib.parse)
    with no Concinno-governance dependency, so it qualifies as a
    substrate move under the Wave 2.7 audit
    (``_AI_BRAIN/05_Planning/2026-05-02-lyceum-api-surface-audit.md``).
    Other layers in the 9-layer stack (PII / SQL / RCE / deserialize
    / HTTP client / policy_gate / circuit breaker / LLM judge /
    permission mode / bash validators) integrate with Concinno's
    ``BaseGuard`` pipeline + audit log + ZIQ outcome bus and stay
    Concinno-side.

@dependencies lyceum.security.ssrf_guard
@exports SSRFVerdict, SSRFCheckConfig, DNSResolver, SSRFGuard,
    BlockReason, CLOUD_METADATA_HOSTS, CLOUD_METADATA_IPS,
    DEFAULT_ALLOWED_SCHEMES, DEFAULT_MAX_REDIRECTS,
    is_private_ip, canonicalize_host
"""

from __future__ import annotations

from lyceum.security.ssrf_guard import (  # noqa: F401 — public API
    CLOUD_METADATA_HOSTS,
    CLOUD_METADATA_IPS,
    DEFAULT_ALLOWED_SCHEMES,
    DEFAULT_MAX_REDIRECTS,
    BlockReason,
    DNSResolver,
    SSRFCheckConfig,
    SSRFGuard,
    SSRFVerdict,
    canonicalize_host,
    is_private_ip,
)

__all__ = [
    "BlockReason",
    "CLOUD_METADATA_HOSTS",
    "CLOUD_METADATA_IPS",
    "DEFAULT_ALLOWED_SCHEMES",
    "DEFAULT_MAX_REDIRECTS",
    "DNSResolver",
    "SSRFCheckConfig",
    "SSRFGuard",
    "SSRFVerdict",
    "canonicalize_host",
    "is_private_ip",
]
