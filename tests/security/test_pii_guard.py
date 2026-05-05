"""Tests for concinno.security.pii_guard — regex PII detection (4.3.0).

Covers ≥120 cases across all PII types, profile/fail-mode integration,
Luhn validation, redaction safety, escape hatch, audit log, ZIQ emit,
and false-positive ceiling on benign payloads.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from concinno.security import (
    PIIGuard,
    PIIType,
    PolicyGateResult,
)
from concinno.security import pii_guard as pii_mod

# ── Shared fixtures ─────────────────────────────────────────────


@pytest.fixture
def audit_tmp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Path:
    """Redirect audit writes to a per-test tmp dir + disable ZIQ bus."""
    monkeypatch.setenv("CONCINNO_AUDIT_DIR", str(tmp_path))
    monkeypatch.setenv("CONCINNO_ZIQ_BUS_DISABLED", "1")
    return tmp_path


@pytest.fixture
def guard_low(audit_tmp: Path) -> PIIGuard:
    """Most permissive guard — captures every PII type incl. low-severity."""
    return PIIGuard(
        profile="lite",
        fail_mode_override="warn",
        min_severity="low",
        luhn_strict=True,
    )


def _types(result: PolicyGateResult) -> set[str]:
    return {f.type for f in result.findings}


# ════════════════════════════════════════════════════════════════
# 1. SSN — 10 positive + 5 negative
# ════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "ssn",
    [
        "123-45-6789",
        "111-22-3333",
        "555-12-1234",
        "001-01-0001",
        "100-50-9999",
        "234-56-7890",
        "789-01-2345",
        "456-78-9012",
        "321-45-6789",
        "678-90-1234",
    ],
)
def test_ssn_positive_detection(guard_low: PIIGuard, ssn: str) -> None:
    result = guard_low.evaluate(f"SSN on file: {ssn}")
    assert PIIType.SSN.value in _types(result)


@pytest.mark.parametrize(
    "not_ssn",
    [
        "000-12-3456",         # invalid area 000
        "666-12-3456",         # invalid area 666
        "999-12-3456",         # invalid area 9xx
        "123-00-3456",         # invalid group 00
        "123-45-0000",         # invalid serial 0000
    ],
)
def test_ssn_negative_invalid_format(
    guard_low: PIIGuard, not_ssn: str
) -> None:
    result = guard_low.evaluate(f"Number: {not_ssn}")
    assert PIIType.SSN.value not in _types(result)


# ════════════════════════════════════════════════════════════════
# 2. Credit card — 10 positive + 5 negative (Luhn-aware)
# ════════════════════════════════════════════════════════════════

# All Luhn-valid sample cards (publicly known test PANs).
_VALID_CARDS = [
    "4242 4242 4242 4242",     # Stripe test Visa
    "4111 1111 1111 1111",     # generic Visa
    "5555 5555 5555 4444",     # Stripe test MC
    "2223 0031 2200 3222",     # Stripe test MC (2-series)
    "3782 822463 10005",       # Amex 15-digit (with separators)
    "6011 1111 1111 1117",     # Discover
    "3056 9300 0902 0004",     # Diners 14-digit
    "5105105105105100",        # MC unpunctuated
    "4012-8888-8888-1881",     # Visa hyphenated
    "378282246310005",         # Amex unpunctuated
]


@pytest.mark.parametrize("card", _VALID_CARDS)
def test_credit_card_luhn_valid_detected(
    guard_low: PIIGuard, card: str
) -> None:
    result = guard_low.evaluate(f"PAN: {card}")
    assert PIIType.CREDIT_CARD.value in _types(result)


@pytest.mark.parametrize(
    "not_card",
    [
        "4242 4242 4242 4243",     # last digit broken Luhn
        "1234 5678 9012 3456",     # arbitrary 16-digit not Luhn
        "1111 1111 1111 1112",     # not Luhn
        "9999 9999 9999 9998",     # not Luhn
        "0000 0000 0000 0001",     # not Luhn
    ],
)
def test_credit_card_luhn_invalid_dropped(
    guard_low: PIIGuard, not_card: str
) -> None:
    result = guard_low.evaluate(f"id: {not_card}")
    assert PIIType.CREDIT_CARD.value not in _types(result)


def test_credit_card_luhn_off_keeps_non_luhn(audit_tmp: Path) -> None:
    g = PIIGuard(
        profile="lite",
        fail_mode_override="warn",
        min_severity="low",
        luhn_strict=False,
    )
    result = g.evaluate("id: 4242 4242 4242 4243")
    assert PIIType.CREDIT_CARD.value in _types(result)


# ════════════════════════════════════════════════════════════════
# 3. Email — 10 positive + 5 negative
# ════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "email",
    [
        "alice@example.com",
        "bob.smith@example.co.uk",
        "user+tag@example.org",
        "first.last@sub.example.com",
        "_private@example.io",
        "12345@numbers.example",
        "x@y.zz",
        "john_doe@example.dev",
        "carol-anne@example.net",
        "support@xn--bcher-kva.example",
    ],
)
def test_email_positive(guard_low: PIIGuard, email: str) -> None:
    result = guard_low.evaluate(f"Reach me at {email} please")
    assert PIIType.EMAIL.value in _types(result)


@pytest.mark.parametrize(
    "not_email",
    [
        "alice at example dot com",
        "@example.com",                # leading @
        "alice@",                       # trailing @
        "alice@example",                # no TLD
        "plain text without at sign",
    ],
)
def test_email_negative(guard_low: PIIGuard, not_email: str) -> None:
    result = guard_low.evaluate(not_email)
    assert PIIType.EMAIL.value not in _types(result)


# ════════════════════════════════════════════════════════════════
# 4. Phone — 10 positive + 5 negative
# ════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "phone",
    [
        "+1 202-555-0123",
        "+44 20 7946 0958",
        "(202) 555-0123",
        "202-555-0123",
        "202.555.0123",
        "+12025550123",
        "020 7946 0958",
        "+33 1 23 45 67 89",
        "+81 3 1234 5678",
        "555-1234567",
    ],
)
def test_phone_positive(guard_low: PIIGuard, phone: str) -> None:
    result = guard_low.evaluate(f"Call me: {phone}")
    assert PIIType.PHONE.value in _types(result)


@pytest.mark.parametrize(
    "not_phone",
    [
        "abc-def-ghij",                # letters
        "12",                          # too short
        "phone please",                # no digits
        "9",                           # one digit
        "                ",            # whitespace only
    ],
)
def test_phone_negative(guard_low: PIIGuard, not_phone: str) -> None:
    result = guard_low.evaluate(not_phone)
    assert PIIType.PHONE.value not in _types(result)


# ════════════════════════════════════════════════════════════════
# 5. IPv4 — 10 positive + 5 negative
# ════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "ip",
    [
        "10.0.0.1",
        "192.168.1.1",
        "172.16.0.5",
        "8.8.8.8",
        "1.1.1.1",
        "127.0.0.1",
        "255.255.255.255",
        "0.0.0.0",
        "203.0.113.42",
        "169.254.1.1",
    ],
)
def test_ipv4_positive(guard_low: PIIGuard, ip: str) -> None:
    result = guard_low.evaluate(f"server: {ip}")
    assert PIIType.IPV4.value in _types(result)


@pytest.mark.parametrize(
    "not_ip",
    [
        "256.1.2.3",          # octet > 255
        "1.2.3",               # too few octets
        "999.999.999.999",     # all out of range
        "abc.def.ghi.jkl",     # letters
        "300.0.0.1",           # first octet > 255
    ],
)
def test_ipv4_negative(guard_low: PIIGuard, not_ip: str) -> None:
    result = guard_low.evaluate(not_ip)
    assert PIIType.IPV4.value not in _types(result)


# ════════════════════════════════════════════════════════════════
# 6. IPv6 — 10 positive + 5 negative
# ════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "ip6",
    [
        "2001:0db8:85a3:0000:0000:8a2e:0370:7334",
        "2001:db8:85a3::8a2e:370:7334",
        "fe80::1",
        "::1",
        "2001:db8::ff00:42:8329",
        "2606:4700:4700::1111",
        "2620:0:2d0:200::7",
        "fd12:3456:789a::1",
        "2001:db8:0:0:1::1",
        "2a00:1450:4001:81b::200e",
    ],
)
def test_ipv6_positive(guard_low: PIIGuard, ip6: str) -> None:
    result = guard_low.evaluate(f"v6 host: {ip6}")
    assert PIIType.IPV6.value in _types(result)


@pytest.mark.parametrize(
    "not_ip6",
    [
        "ghij::klmn",          # non-hex
        "12345::1",            # group > 4 chars
        "plain text",
        "192.168.1.1",         # v4 not v6
        "z::z",                 # not hex
    ],
)
def test_ipv6_negative(guard_low: PIIGuard, not_ip6: str) -> None:
    result = guard_low.evaluate(not_ip6)
    assert PIIType.IPV6.value not in _types(result)


# ════════════════════════════════════════════════════════════════
# 7. API key — 10 positive + 5 negative
# ════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "key",
    [
        "sk-ant-api03-" + "A" * 40,
        "sk-ant-admin01-" + "B" * 64,
        "sk-" + "x" * 48,
        "ghp_" + "Z" * 36,
        "gho_" + "9" * 36,
        "ghs_" + "a" * 36,
        "AKIAIOSFODNN7EXAMPLE",
        "ASIA1234567890123456",
        "AIza" + "0" * 35,
        "sk_live_" + "1" * 24,
    ],
)
def test_api_key_positive(guard_low: PIIGuard, key: str) -> None:
    result = guard_low.evaluate(f"export API_KEY={key}")
    assert PIIType.API_KEY.value in _types(result)


@pytest.mark.parametrize(
    "not_key",
    [
        "sk-ant",              # too short
        "ghp_short",           # 9 chars after prefix
        "AKIAshort",           # not 16 alnum after AKIA
        "AIza" + "0" * 30,     # 30 chars not 35
        "regular sentence with no key prefix at all",
    ],
)
def test_api_key_negative(guard_low: PIIGuard, not_key: str) -> None:
    result = guard_low.evaluate(not_key)
    assert PIIType.API_KEY.value not in _types(result)


def test_api_key_severity_is_critical(guard_low: PIIGuard) -> None:
    """API keys must be `critical` so `min_severity=critical` still catches."""
    result = guard_low.evaluate("ghp_" + "A" * 36)
    keys = [f for f in result.findings if f.type == PIIType.API_KEY.value]
    assert keys
    assert all(f.severity == "critical" for f in keys)


# ════════════════════════════════════════════════════════════════
# 8. Passport / Driver License — basic coverage
# ════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "passport",
    ["A12345678", "B98765432", "123456789", "Z11111111"],
)
def test_passport_positive(guard_low: PIIGuard, passport: str) -> None:
    result = guard_low.evaluate(f"passport: {passport}")
    assert PIIType.PASSPORT.value in _types(result)


@pytest.mark.parametrize(
    "dl_text",
    [
        "DL: D1234567",
        "LIC: ABC12345",
        "DRIVER LIC# X9876543",
        "dl: 12345678",
    ],
)
def test_driver_license_positive(
    guard_low: PIIGuard, dl_text: str
) -> None:
    result = guard_low.evaluate(dl_text)
    assert PIIType.DRIVER_LICENSE.value in _types(result)


# ════════════════════════════════════════════════════════════════
# 9. Severity filter (min_severity)
# ════════════════════════════════════════════════════════════════


def test_min_severity_medium_drops_email_and_ip(
    audit_tmp: Path,
) -> None:
    g = PIIGuard(
        profile="lite",
        fail_mode_override="warn",
        min_severity="medium",
    )
    result = g.evaluate(
        "email: alice@example.com, ip: 1.2.3.4, ssn: 123-45-6789"
    )
    types = _types(result)
    assert PIIType.EMAIL.value not in types
    assert PIIType.IPV4.value not in types
    assert PIIType.SSN.value in types


def test_min_severity_critical_keeps_only_api_keys(
    audit_tmp: Path,
) -> None:
    g = PIIGuard(
        profile="lite",
        fail_mode_override="warn",
        min_severity="critical",
    )
    result = g.evaluate(
        "email: alice@example.com\n"
        "ssn: 123-45-6789\n"
        "card: 4242 4242 4242 4242\n"
        f"key: ghp_{'A' * 36}"
    )
    types = _types(result)
    assert types == {PIIType.API_KEY.value}


def test_min_severity_invalid_raises() -> None:
    with pytest.raises(ValueError):
        PIIGuard(min_severity="bogus")  # type: ignore[arg-type]


# ════════════════════════════════════════════════════════════════
# 10. Redaction safety
# ════════════════════════════════════════════════════════════════


def test_redaction_does_not_leak_full_value(
    guard_low: PIIGuard,
) -> None:
    secret = "ghp_" + "X" * 36
    result = guard_low.evaluate(secret)
    keys = [f for f in result.findings if f.type == PIIType.API_KEY.value]
    assert keys
    for f in keys:
        # Snippet must NOT contain the full secret.
        assert secret not in f.snippet
        # Must contain '***' marker.
        assert "***" in f.snippet


def test_redaction_default_keeps_4_each_side(audit_tmp: Path) -> None:
    g = PIIGuard(
        profile="lite",
        fail_mode_override="warn",
        min_severity="low",
        redact_chars=4,
    )
    result = g.evaluate("123-45-6789")
    f = next(f for f in result.findings if f.type == PIIType.SSN.value)
    # "123-45-6789" → keep first 4 + last 4 with *** between.
    assert f.snippet.startswith("123-")
    assert f.snippet.endswith("6789")
    assert "***" in f.snippet


def test_redact_chars_clamps_low(audit_tmp: Path) -> None:
    g = PIIGuard(
        profile="lite",
        fail_mode_override="warn",
        min_severity="low",
        redact_chars=0,
    )
    # _redact_chars should be clamped to 2.
    assert g._redact_chars == 2  # noqa: SLF001 (test asserts clamp)


def test_redact_chars_clamps_high(audit_tmp: Path) -> None:
    g = PIIGuard(
        profile="lite",
        fail_mode_override="warn",
        min_severity="low",
        redact_chars=99,
    )
    assert g._redact_chars == 8  # noqa: SLF001


def test_redact_short_value_returns_stars() -> None:
    assert pii_mod._redact_match("ab", keep=4) == "***"  # noqa: SLF001
    assert pii_mod._redact_match("a", keep=4) == "***"  # noqa: SLF001


def test_redact_long_value_keeps_edges() -> None:
    out = pii_mod._redact_match("ABCDEFGHIJK", keep=3)  # noqa: SLF001
    assert out.startswith("ABC")
    assert out.endswith("IJK")
    assert "***" in out


# ════════════════════════════════════════════════════════════════
# 11. Profile / fail-mode integration
# ════════════════════════════════════════════════════════════════


def test_profile_lite_default_silent(audit_tmp: Path) -> None:
    """Lite profile default fail_mode_default is `silent` for pii_guard."""
    g = PIIGuard(profile="lite")
    result = g.evaluate("ssn: 123-45-6789")
    # Profile default is silent → decision == accept (suppressed).
    assert result.decision == "accept"
    assert result.fail_mode == "silent"


def test_profile_mainstream_warns(audit_tmp: Path) -> None:
    g = PIIGuard(profile="mainstream")
    result = g.evaluate("ssn: 123-45-6789")
    assert result.decision == "warn"
    assert result.fail_mode == "warn"


def test_profile_strict_hard_deny(audit_tmp: Path) -> None:
    g = PIIGuard(profile="strict")
    result = g.evaluate("ssn: 123-45-6789")
    assert result.decision == "deny"
    assert result.fail_mode == "hard_deny"


def test_profile_paranoid_hard_deny(audit_tmp: Path) -> None:
    g = PIIGuard(profile="paranoid")
    result = g.evaluate("ssn: 123-45-6789")
    assert result.decision == "deny"
    assert result.fail_mode == "hard_deny"


def test_fail_mode_override_wins_over_profile(audit_tmp: Path) -> None:
    g = PIIGuard(profile="paranoid", fail_mode_override="silent")
    result = g.evaluate("ssn: 123-45-6789")
    assert result.decision == "accept"
    assert result.fail_mode == "silent"


# ════════════════════════════════════════════════════════════════
# 12. Escape hatch
# ════════════════════════════════════════════════════════════════


def test_escape_hatch_short_circuits(audit_tmp: Path) -> None:
    g = PIIGuard(profile="paranoid")
    result = g.evaluate(
        "# CONCINNO_DISABLE: known test fixture\nssn: 123-45-6789"
    )
    assert result.decision == "accept"
    assert result.escaped is True
    # Findings list is empty when escaped.
    assert result.findings == ()


def test_escape_hatch_recorded_in_audit_entry(
    audit_tmp: Path,
) -> None:
    g = PIIGuard(profile="paranoid")
    result = g.evaluate(
        "# CONCINNO_DISABLE: legit\nssn: 123-45-6789"
    )
    assert result.audit_entry["escaped"] is True


# ════════════════════════════════════════════════════════════════
# 13. Audit log behaviour
# ════════════════════════════════════════════════════════════════


def test_audit_log_written_on_warn_log(audit_tmp: Path) -> None:
    g = PIIGuard(profile="lite", fail_mode_override="warn+log")
    g.evaluate("ssn: 123-45-6789")
    log = audit_tmp / "pii_guard.jsonl"
    assert log.exists()
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["guard"] == "pii_guard"
    assert entry["decision"] == "warn"
    assert entry["fail_mode"] == "warn+log"
    assert entry["findings"]
    # Every snippet stays redacted in the audit log.
    for f in entry["findings"]:
        assert "***" in f["snippet"]


def test_audit_log_skipped_on_silent(audit_tmp: Path) -> None:
    g = PIIGuard(profile="lite", fail_mode_override="silent")
    g.evaluate("ssn: 123-45-6789")
    log = audit_tmp / "pii_guard.jsonl"
    assert not log.exists()


def test_audit_log_skipped_on_warn_only(audit_tmp: Path) -> None:
    g = PIIGuard(profile="lite", fail_mode_override="warn")
    g.evaluate("ssn: 123-45-6789")
    log = audit_tmp / "pii_guard.jsonl"
    assert not log.exists()


def test_audit_log_written_on_hard_deny(audit_tmp: Path) -> None:
    g = PIIGuard(profile="lite", fail_mode_override="hard_deny")
    g.evaluate("ssn: 123-45-6789")
    log = audit_tmp / "pii_guard.jsonl"
    assert log.exists()
    entry = json.loads(log.read_text(encoding="utf-8").strip())
    assert entry["decision"] == "deny"


def test_audit_snippet_never_leaks_raw_secret(
    audit_tmp: Path,
) -> None:
    secret = "ghp_" + "X" * 36
    g = PIIGuard(profile="lite", fail_mode_override="warn+log")
    g.evaluate(secret)
    log = audit_tmp / "pii_guard.jsonl"
    raw = log.read_text(encoding="utf-8")
    assert secret not in raw


# ════════════════════════════════════════════════════════════════
# 14. ZIQ outcome bus emit (mocked)
# ════════════════════════════════════════════════════════════════


def test_ziq_emit_called_with_correct_reward(
    audit_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bus is disabled in fixture; we re-enable per-test and stub `get_bus`."""
    monkeypatch.delenv("CONCINNO_ZIQ_BUS_DISABLED", raising=False)

    captured: list[Any] = []

    class _StubBus:
        def emit(self, outcome: Any) -> None:
            captured.append(outcome)

    import concinno.ziq_outcome_bus as bus_mod

    monkeypatch.setattr(bus_mod, "get_bus", lambda: _StubBus())
    monkeypatch.setattr(bus_mod, "is_bus_disabled", lambda: False)

    g = PIIGuard(profile="lite", fail_mode_override="warn")
    g.evaluate("ssn: 123-45-6789")

    assert len(captured) == 1
    out = captured[0]
    assert out.tunable == "security.pii_guard"
    # warn → reward 0.5
    assert out.reward == 0.5
    assert out.metadata["decision"] == "warn"
    assert out.metadata["n_findings"] >= 1


def test_ziq_emit_no_op_when_disabled(
    audit_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """audit_tmp fixture sets CONCINNO_ZIQ_BUS_DISABLED=1 — emit must skip."""
    captured: list[Any] = []

    class _StubBus:
        def emit(self, outcome: Any) -> None:
            captured.append(outcome)

    import concinno.ziq_outcome_bus as bus_mod

    monkeypatch.setattr(bus_mod, "get_bus", lambda: _StubBus())
    # Real is_bus_disabled reads env, which fixture set to 1.

    g = PIIGuard(profile="lite", fail_mode_override="warn")
    g.evaluate("ssn: 123-45-6789")

    # Bus disabled → nothing captured.
    assert captured == []


def test_ziq_emit_reward_for_accept(
    audit_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CONCINNO_ZIQ_BUS_DISABLED", raising=False)
    captured: list[Any] = []

    class _StubBus:
        def emit(self, outcome: Any) -> None:
            captured.append(outcome)

    import concinno.ziq_outcome_bus as bus_mod

    monkeypatch.setattr(bus_mod, "get_bus", lambda: _StubBus())
    monkeypatch.setattr(bus_mod, "is_bus_disabled", lambda: False)

    g = PIIGuard(profile="lite", fail_mode_override="warn")
    g.evaluate("clean payload, no PII here whatsoever")

    assert len(captured) == 1
    assert captured[0].reward == 1.0


def test_ziq_emit_reward_for_deny(
    audit_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CONCINNO_ZIQ_BUS_DISABLED", raising=False)
    captured: list[Any] = []

    class _StubBus:
        def emit(self, outcome: Any) -> None:
            captured.append(outcome)

    import concinno.ziq_outcome_bus as bus_mod

    monkeypatch.setattr(bus_mod, "get_bus", lambda: _StubBus())
    monkeypatch.setattr(bus_mod, "is_bus_disabled", lambda: False)

    g = PIIGuard(profile="lite", fail_mode_override="hard_deny")
    g.evaluate("ssn: 123-45-6789")

    assert len(captured) == 1
    assert captured[0].reward == 0.0


# ════════════════════════════════════════════════════════════════
# 15. FEATURE_META registration
# ════════════════════════════════════════════════════════════════


def test_feature_meta_registered() -> None:
    from concinno.feature_config import FEATURE_META

    assert "pii_guard" in FEATURE_META
    meta = FEATURE_META["pii_guard"]
    assert meta["category"] == "security"
    assert meta["enabled"] is True
    assert meta["ziq_autotunable"] is True
    assert meta["cosmetic"] is False
    # All three params must be declared.
    assert set(meta["params"]) == {
        "min_severity",
        "luhn_strict",
        "redact_chars",
    }


def test_feature_meta_min_severity_default_medium() -> None:
    from concinno.feature_config import FEATURE_META

    assert FEATURE_META["pii_guard"]["params"]["min_severity"]["default"] == "medium"


def test_feature_meta_luhn_strict_default_true() -> None:
    from concinno.feature_config import FEATURE_META

    assert FEATURE_META["pii_guard"]["params"]["luhn_strict"]["default"] is True


def test_feature_meta_redact_chars_default_4() -> None:
    from concinno.feature_config import FEATURE_META

    assert FEATURE_META["pii_guard"]["params"]["redact_chars"]["default"] == 4


def test_get_fail_mode_lite_silent() -> None:
    from concinno.feature_config import get_fail_mode

    assert get_fail_mode("pii_guard", "lite") == "silent"


def test_get_fail_mode_mainstream_warn() -> None:
    from concinno.feature_config import get_fail_mode

    assert get_fail_mode("pii_guard", "mainstream") == "warn"


def test_get_fail_mode_strict_hard_deny() -> None:
    from concinno.feature_config import get_fail_mode

    assert get_fail_mode("pii_guard", "strict") == "hard_deny"


def test_get_fail_mode_paranoid_hard_deny() -> None:
    from concinno.feature_config import get_fail_mode

    assert get_fail_mode("pii_guard", "paranoid") == "hard_deny"


# ════════════════════════════════════════════════════════════════
# 16. Multi-finding + overlapping payloads
# ════════════════════════════════════════════════════════════════


def test_multiple_findings_in_one_payload(guard_low: PIIGuard) -> None:
    payload = (
        "Customer: alice@example.com\n"
        "SSN: 123-45-6789\n"
        "Phone: +1 202-555-0123\n"
        "IP: 10.0.0.1\n"
    )
    result = guard_low.evaluate(payload)
    types = _types(result)
    assert PIIType.EMAIL.value in types
    assert PIIType.SSN.value in types
    assert PIIType.PHONE.value in types
    assert PIIType.IPV4.value in types


def test_repeated_same_pii_type_emits_multiple_findings(
    guard_low: PIIGuard,
) -> None:
    result = guard_low.evaluate(
        "ssn1: 123-45-6789, ssn2: 234-56-7890, ssn3: 345-67-8901"
    )
    ssns = [f for f in result.findings if f.type == PIIType.SSN.value]
    assert len(ssns) == 3


# ════════════════════════════════════════════════════════════════
# 17. Payload type coercion (str / bytes / dict)
# ════════════════════════════════════════════════════════════════


def test_bytes_payload_handled(guard_low: PIIGuard) -> None:
    result = guard_low.evaluate(b"ssn: 123-45-6789")
    assert PIIType.SSN.value in _types(result)


def test_dict_payload_handled(guard_low: PIIGuard) -> None:
    result = guard_low.evaluate({"customer_ssn": "123-45-6789"})
    assert PIIType.SSN.value in _types(result)


def test_empty_string_clean(guard_low: PIIGuard) -> None:
    result = guard_low.evaluate("")
    assert result.decision == "accept"
    assert result.findings == ()


# ════════════════════════════════════════════════════════════════
# 18. Luhn helper unit
# ════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "digits",
    [
        "4242424242424242",
        "4111111111111111",
        "5555555555554444",
        "378282246310005",
        "6011111111111117",
    ],
)
def test_luhn_valid_known_cards(digits: str) -> None:
    assert pii_mod._luhn_valid(digits) is True  # noqa: SLF001


@pytest.mark.parametrize(
    "digits",
    [
        "",
        "abc",
        "4242424242424243",      # broken last digit
        "1111111111111112",
        "9999999999999998",
    ],
)
def test_luhn_invalid(digits: str) -> None:
    assert pii_mod._luhn_valid(digits) is False  # noqa: SLF001


# ════════════════════════════════════════════════════════════════
# 19. False-positive ceiling on benign content
# ════════════════════════════════════════════════════════════════


_BENIGN_SAMPLES: list[str] = [
    # 50 short lorem-ipsum + code-like fragments that should produce
    # zero matches at min_severity="medium" (i.e. drop emails / IPs
    # which are quasi-public).
    "Lorem ipsum dolor sit amet, consectetur adipiscing elit.",
    "Sed do eiusmod tempor incididunt ut labore et dolore magna aliqua.",
    "Ut enim ad minim veniam, quis nostrud exercitation ullamco.",
    "Duis aute irure dolor in reprehenderit in voluptate velit.",
    "Excepteur sint occaecat cupidatat non proident, sunt in culpa.",
    "def parse_args(argv: list[str]) -> dict[str, str]:",
    "    return {arg.split('=')[0]: arg.split('=')[1] for arg in argv}",
    "class Widget(Base): pass",
    "if __name__ == '__main__': main()",
    "# This is a comment about implementation details",
    "from typing import Any, Optional, Sequence",
    "raise ValueError('not enough arguments')",
    "logger.info('processed %d records', len(records))",
    "return sum(x * 2 for x in items if x > 0)",
    "with open(path, 'r', encoding='utf-8') as fh: data = fh.read()",
    "Tests passed: 8126/8126 in 42.3 seconds",
    "Coverage: 87.5% (lines 1234 of 1410 covered)",
    "Build #142 succeeded on commit abc1234",
    "Branch: feature/release-431-publish-2026-04-27",
    "Author: AI King <noreply@example>",   # incomplete email
    "Deploy ID: DEP-2026-04-27-001",
    "Memory usage: 1.2GB / 8GB allocated",
    "Latency p95: 42ms, p99: 89ms",
    "Error rate: 0.001% over the last 24 hours",
    "Throughput: 12,345 requests per second",
    "Server hostname: prod-api-7",
    "Cache hit rate increased to 87% after warmup",
    "Database connection pool size: 20 connections",
    "Worker thread spawned (id 42, priority normal)",
    "Heartbeat received from cluster member",
    "Configuration loaded from /etc/app/config.yaml",
    "Migrating schema from v3 to v4",
    "Queue depth: 0 messages pending",
    "GC pause: 12ms (young gen) / 180ms (old gen)",
    "Compaction completed in 3.2 seconds",
    "Index rebuild scheduled for Sunday 02:00 UTC",
    "Replication lag: 50ms behind primary",
    "Snapshot created: snap-2026-04-27-12345",
    "Permission denied (publickey)",
    "fatal: not a git repository",
    "warning: unused import 'os'",
    "Starting application server on port 8080",
    "Listening for HTTP requests on all interfaces",
    "Shutdown signal received, draining connections",
    "Reload complete in 1.4s — 23 modules updated",
    "Watchdog timer reset (counter=0)",
    "TLS handshake succeeded with peer",
    "Compressing response body (gzip, level 6)",
    "Rate limiter allowed request (tokens=10/100)",
    "Circuit breaker tripped: too many upstream failures",
]


def test_benign_payloads_below_2_percent_false_positive(
    audit_tmp: Path,
) -> None:
    """With ``min_severity='medium'`` (FEATURE_META default), benign
    samples should produce ≤2% false positives. The threshold matches
    the spec target."""
    g = PIIGuard(
        profile="lite",
        fail_mode_override="warn",
        min_severity="medium",
    )
    fp_count = 0
    for sample in _BENIGN_SAMPLES:
        result = g.evaluate(sample)
        if result.findings:
            fp_count += 1
    fp_rate = fp_count / len(_BENIGN_SAMPLES)
    assert fp_rate <= 0.02, (
        f"False-positive rate {fp_rate:.1%} exceeds 2% on "
        f"{len(_BENIGN_SAMPLES)} benign samples ({fp_count} flagged)"
    )


# ════════════════════════════════════════════════════════════════
# 20. Pattern catalogue cache
# ════════════════════════════════════════════════════════════════


def test_pattern_cache_shared_across_instances(audit_tmp: Path) -> None:
    g1 = PIIGuard(profile="lite", fail_mode_override="warn")
    g2 = PIIGuard(profile="lite", fail_mode_override="warn")
    # Same compiled pattern objects → cache is class-level.
    assert g1._get_compiled_patterns() is g2._get_compiled_patterns()  # noqa: SLF001


def test_pattern_cache_populated_on_construction(
    audit_tmp: Path,
) -> None:
    # Force-clear cache (private API; this is a regression guard).
    PIIGuard._PATTERNS_CACHE = None  # noqa: SLF001
    PIIGuard(profile="lite", fail_mode_override="warn")
    assert PIIGuard._PATTERNS_CACHE is not None  # noqa: SLF001


# ════════════════════════════════════════════════════════════════
# 21. PIIType enum stability
# ════════════════════════════════════════════════════════════════


def test_piitype_values_stable() -> None:
    """Audit-log keys — never rename; only append. Regression guard."""
    assert PIIType.SSN.value == "ssn"
    assert PIIType.CREDIT_CARD.value == "credit_card"
    assert PIIType.EMAIL.value == "email"
    assert PIIType.PHONE.value == "phone"
    assert PIIType.IPV4.value == "ipv4"
    assert PIIType.IPV6.value == "ipv6"
    assert PIIType.API_KEY.value == "api_key"
    assert PIIType.PASSPORT.value == "passport"
    assert PIIType.DRIVER_LICENSE.value == "driver_license"
