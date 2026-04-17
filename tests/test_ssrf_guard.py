"""Tests for concinno.security.ssrf_guard (Layer 7 Hermes-parity)."""

from __future__ import annotations

import ipaddress
import socket

import pytest

from concinno.security.ssrf_guard import (
    CLOUD_METADATA_HOSTS,
    SSRFCheckConfig,
    SSRFGuard,
    canonicalize_host,
    is_private_ip,
)


class FakeResolver:
    """Injectable resolver. `mapping` maps lowercased host → list of
    IP strings, or the sentinel string "FAIL" to simulate gaierror."""

    def __init__(self, mapping: dict[str, list[str] | str]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    def resolve(self, host: str) -> list[ipaddress._BaseAddress]:
        self.calls.append(host)
        entry = self.mapping.get(host.lower())
        if entry is None or entry == "FAIL":
            raise socket.gaierror(f"FakeResolver: no mapping for {host}")
        assert isinstance(entry, list)
        return [ipaddress.ip_address(ip) for ip in entry]


def _guard_with(mapping: dict[str, list[str] | str], **overrides: object) -> SSRFGuard:
    cfg = SSRFCheckConfig(resolver=FakeResolver(mapping), **overrides)  # type: ignore[arg-type]
    return SSRFGuard(cfg)


# ---------------------------------------------------------------------------
# Scheme / URL shape
# ---------------------------------------------------------------------------


def test_http_public_url_allowed() -> None:
    g = _guard_with({"example.com": ["93.184.216.34"]})
    v = g.check("http://example.com/foo")
    assert v.ok is True
    assert v.reason == "allow"
    assert v.resolved_ip == "93.184.216.34"


def test_https_public_url_allowed() -> None:
    g = _guard_with({"example.com": ["1.1.1.1"]})
    v = g.check("https://example.com/")
    assert v.ok is True
    assert v.reason == "allow"


def test_file_scheme_rejected() -> None:
    g = _guard_with({})
    v = g.check("file:///etc/passwd")
    assert v.ok is False
    assert v.reason == "scheme_not_allowed"


def test_gopher_scheme_rejected() -> None:
    g = _guard_with({})
    v = g.check("gopher://example.com/")
    assert v.ok is False
    assert v.reason == "scheme_not_allowed"


def test_javascript_url_rejected() -> None:
    g = _guard_with({})
    v = g.check("javascript:alert(1)")
    assert v.ok is False
    assert v.reason == "scheme_not_allowed"


def test_data_url_rejected() -> None:
    g = _guard_with({})
    v = g.check("data:text/plain,hello")
    assert v.ok is False
    assert v.reason == "scheme_not_allowed"


def test_empty_hostname_rejected() -> None:
    g = _guard_with({})
    v = g.check("http:///path")
    assert v.ok is False
    assert v.reason == "hostname_empty"


def test_malformed_url_rejected() -> None:
    g = _guard_with({})
    v = g.check("not a url at all")
    assert v.ok is False
    # Either scheme_not_allowed (empty scheme) or hostname_empty —
    # both are fail-closed. We only require it NOT be allowed.
    assert v.reason in {"scheme_not_allowed", "hostname_empty", "url_malformed"}


# ---------------------------------------------------------------------------
# Literal IP hosts — no DNS involved
# ---------------------------------------------------------------------------


def test_literal_loopback_127_rejected() -> None:
    g = _guard_with({})
    v = g.check("http://127.0.0.1/admin")
    assert v.ok is False
    assert v.reason == "ip_loopback"


def test_literal_rfc1918_10_rejected() -> None:
    g = _guard_with({})
    v = g.check("http://10.0.0.5/")
    assert v.ok is False
    assert v.reason == "ip_in_private_range"


def test_literal_rfc1918_172_16_rejected() -> None:
    g = _guard_with({})
    v = g.check("http://172.16.254.1/")
    assert v.ok is False
    assert v.reason == "ip_in_private_range"


def test_literal_rfc1918_192_168_rejected() -> None:
    g = _guard_with({})
    v = g.check("http://192.168.1.1/")
    assert v.ok is False
    assert v.reason == "ip_in_private_range"


def test_literal_link_local_169_254_rejected() -> None:
    g = _guard_with({})
    # Use a non-metadata link-local address so we exercise the
    # link-local classifier (not the metadata exact-match path).
    v = g.check("http://169.254.1.1/")
    assert v.ok is False
    assert v.reason == "ip_link_local"


def test_literal_aws_metadata_exact_ip_rejected() -> None:
    g = _guard_with({})
    v = g.check("http://169.254.169.254/latest/meta-data/")
    assert v.ok is False
    assert v.reason == "cloud_metadata_exact_ip"


def test_literal_cg_nat_100_64_rejected() -> None:
    g = _guard_with({})
    v = g.check("http://100.64.0.1/")
    assert v.ok is False
    assert v.reason == "ip_carrier_nat"


def test_literal_unspecified_0_0_0_0_rejected() -> None:
    g = _guard_with({})
    v = g.check("http://0.0.0.0/")
    assert v.ok is False
    assert v.reason == "ip_unspecified"


def test_literal_ipv6_loopback_rejected() -> None:
    g = _guard_with({})
    v = g.check("http://[::1]/")
    assert v.ok is False
    assert v.reason == "ip_loopback"


def test_literal_ipv6_ula_fc00_rejected() -> None:
    g = _guard_with({})
    v = g.check("http://[fc00::1]/")
    assert v.ok is False
    assert v.reason == "ip_in_private_range"


def test_literal_ipv6_metadata_fd00_ec2_rejected() -> None:
    g = _guard_with({})
    v = g.check("http://[fd00:ec2::254]/latest/meta-data/")
    assert v.ok is False
    assert v.reason == "cloud_metadata_exact_ip"


# ---------------------------------------------------------------------------
# Hostname-based cloud metadata
# ---------------------------------------------------------------------------


def test_cloud_metadata_hostname_exact_rejected() -> None:
    # The hostname check fires BEFORE DNS resolution, so we do not need
    # to map it in the resolver.
    g = _guard_with({})
    v = g.check("http://metadata.google.internal/computeMetadata/v1/")
    assert v.ok is False
    assert v.reason == "cloud_metadata_exact_host"
    assert "metadata.google.internal" in CLOUD_METADATA_HOSTS


# ---------------------------------------------------------------------------
# DNS behaviour
# ---------------------------------------------------------------------------


def test_dns_resolution_failure_fails_closed() -> None:
    g = _guard_with({"nx.example": "FAIL"})
    v = g.check("http://nx.example/")
    assert v.ok is False
    assert v.reason == "dns_resolution_failed"


def test_dns_returns_private_ip_rejected() -> None:
    # Classic DNS rebinding shape: public hostname, resolves to RFC1918.
    g = _guard_with({"evil.example": ["10.0.0.1"]})
    v = g.check("http://evil.example/")
    assert v.ok is False
    assert v.reason == "ip_in_private_range"
    assert v.resolved_ip == "10.0.0.1"


def test_dns_returns_public_ip_allowed() -> None:
    g = _guard_with({"api.example.com": ["8.8.8.8"]})
    v = g.check("https://api.example.com/v1")
    assert v.ok is True
    assert v.reason == "allow"


def test_dns_returns_metadata_ip_rejected_via_ip_layer() -> None:
    # Name that isn't in the hostname blocklist, but resolves to the
    # AWS metadata IP. The IP-layer check must catch it.
    g = _guard_with({"sneaky.example": ["169.254.169.254"]})
    v = g.check("http://sneaky.example/")
    assert v.ok is False
    assert v.reason == "cloud_metadata_exact_ip"


def test_dns_mixed_public_and_private_rejects_on_private() -> None:
    # First-unsafe-wins: any private IP in the resolution set fails.
    g = _guard_with({"mixed.example": ["8.8.8.8", "10.0.0.5"]})
    v = g.check("http://mixed.example/")
    assert v.ok is False
    assert v.reason == "ip_in_private_range"


# ---------------------------------------------------------------------------
# Redirect chains
# ---------------------------------------------------------------------------


def test_redirect_chain_all_safe_returns_final() -> None:
    g = _guard_with(
        {
            "a.example": ["8.8.8.8"],
            "b.example": ["1.1.1.1"],
            "c.example": ["9.9.9.9"],
        }
    )
    v = g.check_redirect_chain(
        ["http://a.example/", "http://b.example/", "http://c.example/"]
    )
    assert v.ok is True
    assert v.host == "c.example"
    assert v.resolved_ip == "9.9.9.9"


def test_redirect_chain_first_unsafe_reported() -> None:
    g = _guard_with(
        {
            "a.example": ["8.8.8.8"],
            "b.example": ["169.254.169.254"],  # redirect landing on AWS metadata
        }
    )
    v = g.check_redirect_chain(["http://a.example/", "http://b.example/"])
    assert v.ok is False
    assert v.reason == "cloud_metadata_exact_ip"
    assert v.host == "b.example"


def test_redirect_chain_too_long() -> None:
    g = _guard_with(
        {f"h{i}.example": ["8.8.8.8"] for i in range(10)},
        max_redirects=3,
    )
    urls = [f"http://h{i}.example/" for i in range(10)]
    v = g.check_redirect_chain(urls)
    assert v.ok is False
    assert v.reason == "redirect_chain_too_long"


# ---------------------------------------------------------------------------
# User blocklists
# ---------------------------------------------------------------------------


def test_user_blocklist_host_rejected() -> None:
    cfg = SSRFCheckConfig(
        resolver=FakeResolver({"internal.corp": ["203.0.113.99"]}),
        user_blocklist_hosts=frozenset({"internal.corp"}),
    )
    g = SSRFGuard(cfg)
    v = g.check("http://internal.corp/")
    assert v.ok is False
    assert v.reason == "user_blocklist"


def test_user_blocklist_ip_network_rejected() -> None:
    cfg = SSRFCheckConfig(
        resolver=FakeResolver({"svc.example": ["198.51.100.7"]}),
        user_blocklist_ip_networks=("198.51.100.0/24",),
    )
    g = SSRFGuard(cfg)
    v = g.check("http://svc.example/")
    assert v.ok is False
    assert v.reason == "user_blocklist"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_is_private_ip_helper_ipv4_true() -> None:
    assert is_private_ip(ipaddress.ip_address("10.1.2.3")) is True
    assert is_private_ip(ipaddress.ip_address("127.0.0.1")) is True
    assert is_private_ip(ipaddress.ip_address("169.254.1.1")) is True
    assert is_private_ip(ipaddress.ip_address("100.64.0.1")) is True


def test_is_private_ip_helper_ipv4_false() -> None:
    assert is_private_ip(ipaddress.ip_address("8.8.8.8")) is False
    assert is_private_ip(ipaddress.ip_address("8.8.4.4")) is False


def test_canonicalize_host_lowercase_and_strip_dot() -> None:
    assert canonicalize_host("Example.COM.") == "example.com"
    assert canonicalize_host("MeTaDaTa.google.Internal") == "metadata.google.internal"
    # IDN: Unicode input should become its ASCII Punycode form for
    # stable exact matching.
    assert canonicalize_host("münchen.de").startswith("xn--")


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


def test_stats_counts_blocks_by_reason() -> None:
    g = _guard_with({"ok.example": ["1.1.1.1"]})
    g.check("http://ok.example/")            # allow
    g.check("http://127.0.0.1/")              # ip_loopback
    g.check("file:///etc/passwd")             # scheme_not_allowed
    g.check("http://10.0.0.1/")               # ip_in_private_range
    s = g.stats()
    assert s["checks_total"] == 4
    assert s["allows"] == 1
    assert s["block_ip_loopback"] == 1
    assert s["block_scheme_not_allowed"] == 1
    assert s["block_ip_in_private_range"] == 1


# ---------------------------------------------------------------------------
# Parametrized: every cloud metadata host should be rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", sorted(CLOUD_METADATA_HOSTS))
def test_every_cloud_metadata_host_rejected(host: str) -> None:
    g = _guard_with({})
    v = g.check(f"http://{host}/")
    assert v.ok is False
    assert v.reason == "cloud_metadata_exact_host"
