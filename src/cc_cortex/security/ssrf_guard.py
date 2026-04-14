"""cc_cortex.security.ssrf_guard — Layer 7 SSRF validator (Hermes-parity).

@module security.ssrf_guard
@responsibility Pure-validation SSRF guard. Given a URL (or a sequence of
    redirect hops), return a verdict deciding whether the fetch is safe.
    Blocks outbound requests targeting internal ranges (RFC1918, loopback,
    link-local, carrier-grade NAT, cloud metadata endpoints) with
    fail-closed DNS resolution and redirect re-validation. This module
    does NOT perform HTTP requests — callers wrap their own httpx /
    requests / urllib calls around `SSRFGuard.check()` and feed each
    redirect hop into `check_redirect_chain()`.

    Layer 7 of the 9-layer Hermes-parity security stack. Matches the
    behaviour of the TypeScript `tirith` SSRF layer in Hermes Agent with
    a stdlib-only Python port, plus explicit redirect re-validation
    (Hermes does this implicitly via its HTTP client wrapper; we make
    it a first-class API because callers bring their own client).

@dependencies stdlib only (dataclasses, ipaddress, socket, typing,
    urllib.parse)
@exports SSRFVerdict, SSRFCheckConfig, DNSResolver, SSRFGuard,
    BlockReason, CLOUD_METADATA_HOSTS, CLOUD_METADATA_IPS,
    DEFAULT_ALLOWED_SCHEMES, DEFAULT_MAX_REDIRECTS,
    is_private_ip, canonicalize_host

Design notes:
  - Fail-closed everywhere. Ambiguity → reject. A false positive is a
    caller annoyance; a false negative is a catastrophic SSRF to the
    cloud metadata endpoint. We pay the annoyance tax.
  - Literal IP hosts short-circuit DNS — `http://169.254.169.254/` is
    rejected without resolution. DNS is only invoked for symbolic
    hostnames, and on ANY resolution error we fail closed, never
    "assume public".
  - Cloud metadata is enforced at TWO layers: exact hostname match
    (against the canonicalized host string) AND exact IP match (against
    every resolved IP). Either layer is enough; both together defeat
    both "use the hostname" and "spray the raw IP" attacks.
  - Redirect re-validation is the real point of this module. Most SSRF
    in production doesn't look like `http://169.254.169.254/` — it
    looks like `http://attacker.com/redirect?to=169.254.169.254`, the
    server fetches the first URL legitimately, follows the 302, and
    lands on the metadata endpoint. `check_redirect_chain()` forces the
    caller to revalidate every hop.
  - IPv6 is a first-class citizen. `::1`, `fc00::/7`, `fe80::/10`, and
    the AWS IPv6 metadata address `fd00:ec2::254` are all blocked.
  - `user_blocklist_ip_networks` is a tuple of CIDR strings so the
    config struct stays hashable / easy to serialize. Parsed once per
    guard construction into `ipaddress.ip_network` objects.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field
from typing import Iterable, Literal, Protocol, cast
from urllib.parse import urlparse

BlockReason = Literal[
    "allow",
    "scheme_not_allowed",
    "url_malformed",
    "hostname_empty",
    "dns_resolution_failed",
    "ip_in_private_range",
    "ip_loopback",
    "ip_link_local",
    "ip_carrier_nat",
    "ip_unspecified",
    "cloud_metadata_exact_host",
    "cloud_metadata_exact_ip",
    "redirect_chain_too_long",
    "user_blocklist",
]

DEFAULT_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})
DEFAULT_MAX_REDIRECTS = 5

# Exact host blocklist — match against lowercase, dot-stripped hostname
# before DNS. These are the canonical cloud metadata service hostnames
# that every major provider uses; blocking by name defeats the trivial
# "I used the DNS name, not the IP" evasion.
CLOUD_METADATA_HOSTS: frozenset[str] = frozenset(
    {
        "metadata.google.internal",
        "metadata.google.com",
        "metadata",
        "metadata.azure.com",
        "instance-data",
        "instance-data.ec2.internal",
    }
)

# Exact IP blocklist. 169.254.169.254 is AWS + Azure + GCP (shared).
# fd00:ec2::254 is AWS IPv6 metadata.
CLOUD_METADATA_IPS: frozenset[str] = frozenset(
    {
        "169.254.169.254",
        "fd00:ec2::254",
    }
)

# Carrier-grade NAT range (RFC 6598). `.is_private` on older Python
# versions does not include this; we match it explicitly.
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")


@dataclass(frozen=True)
class SSRFVerdict:
    """Result of a single URL (or redirect chain) validation."""

    ok: bool
    reason: BlockReason
    url: str
    host: str
    resolved_ip: str | None
    detail: str = ""


@dataclass
class SSRFCheckConfig:
    """Tunable policy for an SSRFGuard instance."""

    allowed_schemes: frozenset[str] = DEFAULT_ALLOWED_SCHEMES
    user_blocklist_hosts: frozenset[str] = field(default_factory=frozenset)
    user_blocklist_ip_networks: tuple[str, ...] = ()
    max_redirects: int = DEFAULT_MAX_REDIRECTS
    resolver: "DNSResolver | None" = None


class DNSResolver(Protocol):
    """Resolver protocol. Injectable so tests (and callers that already
    have a cached resolver) don't hit real DNS. Implementations must
    return a list of `ipaddress.IPv4Address | IPv6Address` objects, or
    raise `socket.gaierror` / `OSError` on failure. An empty list is
    treated as a resolution failure by the guard (fail-closed)."""

    def resolve(self, host: str) -> list[ipaddress._BaseAddress]: ...


class _StdlibDNSResolver:
    """Default resolver backed by `socket.getaddrinfo`. Returns every
    address family the host advertises so IPv6-only metadata addresses
    cannot slip through an A-record-only resolver."""

    def resolve(self, host: str) -> list[ipaddress._BaseAddress]:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        out: list[ipaddress._BaseAddress] = []
        seen: set[str] = set()
        for info in infos:
            sockaddr = info[4]
            raw_ip = sockaddr[0]
            if not isinstance(raw_ip, str):
                continue
            # IPv6 sockaddr carries a scope id on link-local addresses.
            ip_str = raw_ip.split("%", 1)[0]
            if ip_str in seen:
                continue
            seen.add(ip_str)
            try:
                out.append(ipaddress.ip_address(ip_str))
            except ValueError:
                # Skip anything the stdlib cannot canonicalize.
                continue
        return out


def canonicalize_host(host: str) -> str:
    """Lowercase, strip trailing dot, IDNA-decode for reliable exact
    matching against the cloud metadata hostname set. Falls back to the
    plain lowercased form if IDNA encoding fails (e.g. the host is
    already an IP literal or contains characters the codec rejects)."""

    lowered = host.strip().lower().rstrip(".")
    if not lowered:
        return lowered
    try:
        return lowered.encode("idna").decode("ascii")
    except (UnicodeError, UnicodeDecodeError, UnicodeEncodeError):
        return lowered


def _classify_private_ipv4(ip: ipaddress.IPv4Address) -> BlockReason | None:
    """Return the specific block reason for an IPv4, or None if public."""

    if str(ip) == "169.254.169.254":
        return "cloud_metadata_exact_ip"
    if ip.is_loopback:
        return "ip_loopback"
    if ip.is_link_local:
        return "ip_link_local"
    if ip.is_unspecified:
        return "ip_unspecified"
    if ip in _CGNAT_NETWORK:
        return "ip_carrier_nat"
    if ip.is_private:
        return "ip_in_private_range"
    return None


def _classify_private_ipv6(ip: ipaddress.IPv6Address) -> BlockReason | None:
    """Return the specific block reason for an IPv6, or None if public."""

    if ip.exploded == ipaddress.IPv6Address("fd00:ec2::254").exploded:
        return "cloud_metadata_exact_ip"
    if ip.is_loopback:
        return "ip_loopback"
    if ip.is_link_local:
        return "ip_link_local"
    if ip.is_unspecified:
        return "ip_unspecified"
    # `.is_private` covers ULA (fc00::/7) and site-local (fec0::/10,
    # deprecated but still classed private). We treat ULA as private.
    if ip.is_private:
        return "ip_in_private_range"
    return None


def is_private_ip(ip: ipaddress._BaseAddress) -> bool:
    """True if the IP lands in any range this guard blocks. Does NOT
    distinguish the reason — use `_classify_private_ipv4/6` for that."""

    if isinstance(ip, ipaddress.IPv4Address):
        return _classify_private_ipv4(ip) is not None
    if isinstance(ip, ipaddress.IPv6Address):
        return _classify_private_ipv6(ip) is not None
    return False


class SSRFGuard:
    """Pure-validation SSRF guard. Stateless aside from a small counter
    dict used for `stats()`. Thread-safe for `check()` / `resolve_host()`
    as long as the injected resolver is thread-safe; `stats()` counters
    use plain int increments and may undercount under heavy contention
    (we accept that — these are observability counters, not billing)."""

    def __init__(self, config: SSRFCheckConfig | None = None) -> None:
        self._config = config or SSRFCheckConfig()
        self._resolver: DNSResolver = self._config.resolver or _StdlibDNSResolver()
        _NetT = ipaddress.IPv4Network | ipaddress.IPv6Network
        self._user_blocked_networks: tuple[_NetT, ...] = tuple(
            ipaddress.ip_network(cidr, strict=False)
            for cidr in self._config.user_blocklist_ip_networks
        )
        self._checks_total = 0
        self._allows = 0
        self._redirects_checked = 0
        self._blocks_by_reason: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check(self, url: str) -> SSRFVerdict:
        """Validate a single URL. Always returns a verdict (never raises
        for bad input — a malformed URL becomes a `url_malformed` block)."""

        self._checks_total += 1
        verdict = self._check_impl(url)
        if verdict.ok:
            self._allows += 1
        else:
            self._blocks_by_reason[verdict.reason] = (
                self._blocks_by_reason.get(verdict.reason, 0) + 1
            )
        return verdict

    def check_redirect_chain(self, urls: Iterable[str]) -> SSRFVerdict:
        """Validate a redirect chain. Returns the first non-ok verdict;
        if every hop is safe, returns the verdict of the final hop. If
        the chain length exceeds `max_redirects`, returns a
        `redirect_chain_too_long` verdict pointing at the offending URL.
        An empty chain returns a `url_malformed` verdict — callers must
        supply at least the initial URL."""

        last_verdict: SSRFVerdict | None = None
        hop_count = 0
        for url in urls:
            hop_count += 1
            self._redirects_checked += 1
            if hop_count > self._config.max_redirects:
                reason: BlockReason = "redirect_chain_too_long"
                self._blocks_by_reason[reason] = (
                    self._blocks_by_reason.get(reason, 0) + 1
                )
                return SSRFVerdict(
                    ok=False,
                    reason=reason,
                    url=url,
                    host="",
                    resolved_ip=None,
                    detail=f"redirect chain exceeded max_redirects={self._config.max_redirects}",
                )
            verdict = self.check(url)
            if not verdict.ok:
                return verdict
            last_verdict = verdict
        if last_verdict is None:
            return SSRFVerdict(
                ok=False,
                reason="url_malformed",
                url="",
                host="",
                resolved_ip=None,
                detail="empty redirect chain",
            )
        return last_verdict

    def resolve_host(self, host: str) -> list[ipaddress._BaseAddress]:
        """Resolve via the configured resolver. Returns an empty list on
        any failure; the caller (our own `_check_impl`) interprets an
        empty list as fail-closed."""

        try:
            return self._resolver.resolve(host)
        except (socket.gaierror, OSError, UnicodeError):
            return []

    def stats(self) -> dict[str, int]:
        """Return an observability snapshot. Keys are stable."""

        return {
            "checks_total": self._checks_total,
            "allows": self._allows,
            "redirects_checked": self._redirects_checked,
            **{f"block_{reason}": count for reason, count in self._blocks_by_reason.items()},
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _check_impl(self, url: str) -> SSRFVerdict:
        try:
            parsed = urlparse(url)
        except ValueError as exc:
            return SSRFVerdict(
                ok=False,
                reason="url_malformed",
                url=url,
                host="",
                resolved_ip=None,
                detail=f"urlparse failed: {exc}",
            )

        scheme = (parsed.scheme or "").lower()
        if not scheme or scheme not in self._config.allowed_schemes:
            return SSRFVerdict(
                ok=False,
                reason="scheme_not_allowed",
                url=url,
                host="",
                resolved_ip=None,
                detail=f"scheme={scheme!r}",
            )

        raw_host = parsed.hostname
        if raw_host is None or raw_host == "":
            return SSRFVerdict(
                ok=False,
                reason="hostname_empty",
                url=url,
                host="",
                resolved_ip=None,
                detail="parsed.hostname is empty",
            )

        host = canonicalize_host(raw_host)
        if not host:
            return SSRFVerdict(
                ok=False,
                reason="hostname_empty",
                url=url,
                host="",
                resolved_ip=None,
                detail="hostname became empty after canonicalization",
            )

        if host in CLOUD_METADATA_HOSTS:
            return SSRFVerdict(
                ok=False,
                reason="cloud_metadata_exact_host",
                url=url,
                host=host,
                resolved_ip=None,
                detail="host matched CLOUD_METADATA_HOSTS",
            )

        if host in self._config.user_blocklist_hosts:
            return SSRFVerdict(
                ok=False,
                reason="user_blocklist",
                url=url,
                host=host,
                resolved_ip=None,
                detail="host matched user_blocklist_hosts",
            )

        # Literal IP? Skip DNS entirely.
        literal_ip = self._try_literal_ip(raw_host)
        if literal_ip is not None:
            return self._verdict_for_ip(url, host, literal_ip)

        resolved = self.resolve_host(host)
        if not resolved:
            return SSRFVerdict(
                ok=False,
                reason="dns_resolution_failed",
                url=url,
                host=host,
                resolved_ip=None,
                detail="resolver returned no addresses",
            )

        # Every resolved IP must pass. The first failure wins.
        first_ip_str: str | None = None
        for ip in resolved:
            if first_ip_str is None:
                first_ip_str = str(ip)
            sub_verdict = self._verdict_for_ip(url, host, ip)
            if not sub_verdict.ok:
                return sub_verdict

        return SSRFVerdict(
            ok=True,
            reason="allow",
            url=url,
            host=host,
            resolved_ip=first_ip_str,
            detail="",
        )

    @staticmethod
    def _try_literal_ip(host: str) -> ipaddress._BaseAddress | None:
        """Return an IP object if `host` parses as a literal address."""

        candidate = host
        # urlparse on `http://[::1]/` gives hostname `::1` (brackets
        # stripped), but be defensive and strip explicit brackets too.
        if candidate.startswith("[") and candidate.endswith("]"):
            candidate = candidate[1:-1]
        try:
            return ipaddress.ip_address(candidate)
        except ValueError:
            return None

    def _verdict_for_ip(
        self,
        url: str,
        host: str,
        ip: ipaddress._BaseAddress,
    ) -> SSRFVerdict:
        """Classify a single IP. Handles cloud metadata, user blocklist,
        and private-range rejection in that order."""

        ip_str = str(ip)
        if ip_str in CLOUD_METADATA_IPS:
            return SSRFVerdict(
                ok=False,
                reason="cloud_metadata_exact_ip",
                url=url,
                host=host,
                resolved_ip=ip_str,
                detail="ip matched CLOUD_METADATA_IPS",
            )

        # Normalize fd00:ec2::254 written in a non-canonical form.
        if isinstance(ip, ipaddress.IPv6Address):
            if ip.exploded == ipaddress.IPv6Address("fd00:ec2::254").exploded:
                return SSRFVerdict(
                    ok=False,
                    reason="cloud_metadata_exact_ip",
                    url=url,
                    host=host,
                    resolved_ip=ip_str,
                    detail="ip matched CLOUD_METADATA_IPS (ipv6 metadata)",
                )

        for net in self._user_blocked_networks:
            if isinstance(ip, ipaddress.IPv4Address) and isinstance(
                net, ipaddress.IPv4Network
            ):
                if ip in net:
                    return SSRFVerdict(
                        ok=False,
                        reason="user_blocklist",
                        url=url,
                        host=host,
                        resolved_ip=ip_str,
                        detail=f"ip in user_blocklist network {net}",
                    )
            elif isinstance(ip, ipaddress.IPv6Address) and isinstance(
                net, ipaddress.IPv6Network
            ):
                if ip in net:
                    return SSRFVerdict(
                        ok=False,
                        reason="user_blocklist",
                        url=url,
                        host=host,
                        resolved_ip=ip_str,
                        detail=f"ip in user_blocklist network {net}",
                    )

        if isinstance(ip, ipaddress.IPv4Address):
            reason = _classify_private_ipv4(ip)
        elif isinstance(ip, ipaddress.IPv6Address):
            reason = _classify_private_ipv6(ip)
        else:
            reason = None

        if reason is not None:
            return SSRFVerdict(
                ok=False,
                reason=cast(BlockReason, reason),
                url=url,
                host=host,
                resolved_ip=ip_str,
                detail=f"ip {ip_str} classified as {reason}",
            )

        return SSRFVerdict(
            ok=True,
            reason="allow",
            url=url,
            host=host,
            resolved_ip=ip_str,
            detail="",
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
