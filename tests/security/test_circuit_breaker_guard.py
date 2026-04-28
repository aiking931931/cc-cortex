"""Tests for concinno.security.circuit_breaker_guard.

Coverage targets (≥25 cases):
  * State machine transitions (closed → open → half_open → closed)
  * Rate limit at boundary + sliding window aging
  * Failure threshold trips breaker
  * Cooldown timer + exponential backoff doubling
  * Half-open probe gating (one in flight)
  * Fail-mode chain (silent / warn / warn+log / hard_deny)
  * Escape hatch (per-line + base broad form)
  * Audit log only writes for warn+log + hard_deny
  * FEATURE_META schema valid + registered in DEFAULT_OFF_4_0_0
  * ZIQ outcome bus emit lifecycle
  * Payload coercion (dict / str / bytes / malformed)
  * Constructor parameter validation
  * Snapshot / list_resources / reset
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from concinno.security import (
    CircuitBreakerGuard,
    CircuitState,
    PolicyGate,
)

# ── Fixtures ───────────────────────────────────────────────────


@pytest.fixture
def audit_tmp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> Path:
    """Redirect audit writes + disable ZIQ bus for clean isolation."""
    monkeypatch.setenv("CONCINNO_AUDIT_DIR", str(tmp_path))
    monkeypatch.setenv("CONCINNO_ZIQ_BUS_DISABLED", "1")
    return tmp_path


class _FakeClock:
    """Deterministic monotonic clock for state-machine timing tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def advance(self, delta: float) -> None:
        self.now += delta

    def __call__(self) -> float:
        return self.now


def _ok(resource: str = "api.foo") -> dict[str, Any]:
    return {"resource": resource, "outcome": "success"}


def _fail(resource: str = "api.foo") -> dict[str, Any]:
    return {"resource": resource, "outcome": "failure"}


def _probe(resource: str = "api.foo") -> dict[str, Any]:
    """Pre-call probe — no outcome reported."""
    return {"resource": resource}


# ══════════════════════════════════════════════════════════════
#  1. Inheritance / class invariants
# ══════════════════════════════════════════════════════════════


def test_inherits_from_policygate() -> None:
    assert issubclass(CircuitBreakerGuard, PolicyGate)


def test_class_name_constant() -> None:
    assert CircuitBreakerGuard.name == "circuit_breaker_guard"


def test_initial_state_is_closed(audit_tmp: Path) -> None:
    g = CircuitBreakerGuard()
    snap = g.snapshot("api.foo")
    assert snap.state is CircuitState.CLOSED
    assert snap.consecutive_failures == 0


# ══════════════════════════════════════════════════════════════
#  2. Constructor validation
# ══════════════════════════════════════════════════════════════


def test_negative_max_calls_raises() -> None:
    with pytest.raises(ValueError, match="max_calls"):
        CircuitBreakerGuard(max_calls=-1)


def test_zero_window_raises() -> None:
    with pytest.raises(ValueError, match="window_s"):
        CircuitBreakerGuard(window_s=0)


def test_zero_failure_threshold_raises() -> None:
    with pytest.raises(ValueError, match="failure_threshold"):
        CircuitBreakerGuard(failure_threshold=0)


def test_negative_cooldown_raises() -> None:
    with pytest.raises(ValueError, match="cooldown_s"):
        CircuitBreakerGuard(cooldown_s=-1)


def test_backoff_max_below_base_raises() -> None:
    with pytest.raises(ValueError, match="backoff_max_s"):
        CircuitBreakerGuard(backoff_base_s=10.0, backoff_max_s=5.0)


def test_invalid_severity_raises() -> None:
    with pytest.raises(ValueError, match="rate_limit_severity"):
        CircuitBreakerGuard(rate_limit_severity="extreme")  # type: ignore[arg-type]


# ══════════════════════════════════════════════════════════════
#  3. CLOSED state — clean calls accepted
# ══════════════════════════════════════════════════════════════


def test_closed_clean_call_accepted(audit_tmp: Path) -> None:
    g = CircuitBreakerGuard(fail_mode_override="warn")
    r = g.evaluate(_ok())
    assert r.decision == "accept"
    assert not r.findings


def test_closed_failure_under_threshold_accepted(audit_tmp: Path) -> None:
    g = CircuitBreakerGuard(fail_mode_override="warn", failure_threshold=5)
    for _ in range(4):
        r = g.evaluate(_fail())
        assert r.decision == "accept"


# ══════════════════════════════════════════════════════════════
#  4. Failure threshold trips breaker
# ══════════════════════════════════════════════════════════════


def test_consecutive_failures_open_breaker(audit_tmp: Path) -> None:
    clock = _FakeClock()
    g = CircuitBreakerGuard(
        fail_mode_override="warn",
        failure_threshold=3,
        time_source=clock,
    )
    # 3 failures → breaker opens. The 4th call sees circuit_open.
    for _ in range(3):
        g.evaluate(_fail())
    snap = g.snapshot("api.foo")
    assert snap.state is CircuitState.OPEN
    assert snap.consecutive_failures == 3


def test_breaker_open_denies_subsequent_call(audit_tmp: Path) -> None:
    clock = _FakeClock()
    g = CircuitBreakerGuard(
        fail_mode_override="hard_deny",
        failure_threshold=2,
        cooldown_s=10.0,
        time_source=clock,
    )
    for _ in range(2):
        g.evaluate(_fail())
    r = g.evaluate(_probe())
    assert r.decision == "deny"
    assert any(f.type == "circuit_open" for f in r.findings)


def test_success_resets_failure_counter(audit_tmp: Path) -> None:
    g = CircuitBreakerGuard(fail_mode_override="warn", failure_threshold=3)
    g.evaluate(_fail())
    g.evaluate(_fail())
    g.evaluate(_ok())  # resets
    snap = g.snapshot("api.foo")
    assert snap.consecutive_failures == 0
    assert snap.state is CircuitState.CLOSED


def test_timeout_counts_as_failure(audit_tmp: Path) -> None:
    g = CircuitBreakerGuard(fail_mode_override="warn", failure_threshold=2)
    g.evaluate({"resource": "api.x", "outcome": "timeout"})
    g.evaluate({"resource": "api.x", "outcome": "timeout"})
    assert g.snapshot("api.x").state is CircuitState.OPEN


def test_unknown_outcome_does_not_tick_failure(audit_tmp: Path) -> None:
    g = CircuitBreakerGuard(fail_mode_override="warn", failure_threshold=2)
    for _ in range(10):
        g.evaluate({"resource": "api.x", "outcome": "unknown"})
    assert g.snapshot("api.x").state is CircuitState.CLOSED


# ══════════════════════════════════════════════════════════════
#  5. OPEN → HALF_OPEN transition (cooldown elapsed)
# ══════════════════════════════════════════════════════════════


def test_half_open_admits_first_probe(audit_tmp: Path) -> None:
    clock = _FakeClock()
    g = CircuitBreakerGuard(
        fail_mode_override="warn",
        failure_threshold=2,
        cooldown_s=10.0,
        time_source=clock,
    )
    g.evaluate(_fail())
    g.evaluate(_fail())  # OPEN
    clock.advance(11.0)  # past cooldown

    r = g.evaluate(_probe())
    # First call after cooldown is admitted as a half_open probe.
    assert r.decision == "warn"
    assert any(f.type == "half_open_probe" for f in r.findings)
    assert g.snapshot("api.foo").state is CircuitState.HALF_OPEN


def test_half_open_rejects_concurrent_callers(audit_tmp: Path) -> None:
    """Only one probe in flight per resource."""
    clock = _FakeClock()
    g = CircuitBreakerGuard(
        fail_mode_override="warn",
        failure_threshold=2,
        cooldown_s=10.0,
        time_source=clock,
    )
    g.evaluate(_fail())
    g.evaluate(_fail())
    clock.advance(11.0)

    r1 = g.evaluate(_probe())
    r2 = g.evaluate(_probe())  # second concurrent caller
    assert any(f.type == "half_open_probe" for f in r1.findings)
    assert any(f.type == "circuit_open" for f in r2.findings)


def test_half_open_success_closes_circuit(audit_tmp: Path) -> None:
    clock = _FakeClock()
    g = CircuitBreakerGuard(
        fail_mode_override="warn",
        failure_threshold=2,
        cooldown_s=10.0,
        time_source=clock,
    )
    g.evaluate(_fail())
    g.evaluate(_fail())  # report 2nd failure → trips OPEN
    clock.advance(11.0)
    g.evaluate(_probe())  # admit path: OPEN→HALF_OPEN, probe in flight
    g.evaluate(_ok())  # report path: probe succeeded → CLOSED
    snap = g.snapshot("api.foo")
    assert snap.state is CircuitState.CLOSED
    assert snap.consecutive_failures == 0


def test_half_open_failure_reopens_with_doubled_cooldown(
    audit_tmp: Path,
) -> None:
    clock = _FakeClock()
    g = CircuitBreakerGuard(
        fail_mode_override="warn",
        failure_threshold=2,
        cooldown_s=10.0,
        backoff_max_s=60.0,
        time_source=clock,
    )
    g.evaluate(_fail())
    g.evaluate(_fail())  # report → OPEN, cooldown=10
    clock.advance(11.0)
    g.evaluate(_probe())  # admit: OPEN→HALF_OPEN, probe in flight
    g.evaluate(_fail())  # report: probe failed → re-open with doubled cooldown
    snap = g.snapshot("api.foo")
    assert snap.state is CircuitState.OPEN
    assert snap.cooldown_s == pytest.approx(20.0)  # doubled


def test_backoff_does_not_exceed_ceiling(audit_tmp: Path) -> None:
    clock = _FakeClock()
    g = CircuitBreakerGuard(
        fail_mode_override="warn",
        failure_threshold=1,
        cooldown_s=40.0,
        backoff_max_s=50.0,
        time_source=clock,
    )
    g.evaluate(_fail())  # report → OPEN, cooldown=40
    clock.advance(41.0)
    g.evaluate(_probe())  # admit: HALF_OPEN
    g.evaluate(_fail())  # report: probe fail → cooldown=80 capped at 50
    assert g.snapshot("api.foo").cooldown_s == pytest.approx(50.0)


# ══════════════════════════════════════════════════════════════
#  6. Rate limit (sliding window)
# ══════════════════════════════════════════════════════════════


def test_rate_limit_under_cap_accepts(audit_tmp: Path) -> None:
    clock = _FakeClock()
    g = CircuitBreakerGuard(
        fail_mode_override="warn",
        max_calls=3, window_s=10.0, time_source=clock,
    )
    for i in range(3):
        clock.advance(0.1)
        r = g.evaluate(_probe())
        assert r.decision == "accept", f"call {i} should pass"


def test_rate_limit_at_cap_denies(audit_tmp: Path) -> None:
    clock = _FakeClock()
    g = CircuitBreakerGuard(
        fail_mode_override="warn",
        max_calls=3, window_s=10.0, time_source=clock,
    )
    for _ in range(3):
        g.evaluate(_probe())
    r = g.evaluate(_probe())
    assert any(f.type == "rate_limit_exceeded" for f in r.findings)


def test_rate_limit_window_aging_releases_calls(audit_tmp: Path) -> None:
    clock = _FakeClock()
    g = CircuitBreakerGuard(
        fail_mode_override="warn",
        max_calls=2, window_s=10.0, time_source=clock,
    )
    g.evaluate(_probe())
    g.evaluate(_probe())  # at cap
    clock.advance(11.0)  # window aged out
    r = g.evaluate(_probe())
    assert r.decision == "accept"


def test_rate_limit_zero_disables(audit_tmp: Path) -> None:
    g = CircuitBreakerGuard(fail_mode_override="warn", max_calls=0)
    for _ in range(1000):
        r = g.evaluate(_probe())
        assert r.decision == "accept"


def test_rate_limit_per_resource_independent(audit_tmp: Path) -> None:
    g = CircuitBreakerGuard(
        fail_mode_override="warn", max_calls=2, window_s=60.0,
    )
    g.evaluate(_probe("api.x"))
    g.evaluate(_probe("api.x"))  # x at cap
    # y still has full window
    r = g.evaluate(_probe("api.y"))
    assert r.decision == "accept"


# ══════════════════════════════════════════════════════════════
#  7. Fail-mode chain mapping
# ══════════════════════════════════════════════════════════════


def test_fail_mode_silent_accepts(audit_tmp: Path) -> None:
    g = CircuitBreakerGuard(
        fail_mode_override="silent", failure_threshold=1, cooldown_s=10.0,
    )
    g.evaluate(_fail())
    r = g.evaluate(_probe())  # breaker is OPEN → finding present
    assert r.findings  # finding exists
    assert r.decision == "accept"  # but silent suppresses


def test_fail_mode_warn_warns(audit_tmp: Path) -> None:
    g = CircuitBreakerGuard(
        fail_mode_override="warn", failure_threshold=1, cooldown_s=10.0,
    )
    g.evaluate(_fail())
    r = g.evaluate(_probe())
    assert r.decision == "warn"


def test_fail_mode_warn_log_warns(audit_tmp: Path) -> None:
    g = CircuitBreakerGuard(
        fail_mode_override="warn+log", failure_threshold=1, cooldown_s=10.0,
    )
    g.evaluate(_fail())
    r = g.evaluate(_probe())
    assert r.decision == "warn"


def test_fail_mode_hard_deny_denies(audit_tmp: Path) -> None:
    g = CircuitBreakerGuard(
        fail_mode_override="hard_deny",
        failure_threshold=1,
        cooldown_s=10.0,
    )
    g.evaluate(_fail())
    r = g.evaluate(_probe())
    assert r.decision == "deny"


# ══════════════════════════════════════════════════════════════
#  8. Audit log behaviour
# ══════════════════════════════════════════════════════════════


def test_audit_log_silent_writes_nothing(audit_tmp: Path) -> None:
    g = CircuitBreakerGuard(
        fail_mode_override="silent", failure_threshold=1, cooldown_s=10.0,
    )
    g.evaluate(_fail())
    g.evaluate(_probe())
    log = audit_tmp / "circuit_breaker_guard.jsonl"
    assert not log.exists()


def test_audit_log_warn_writes_nothing(audit_tmp: Path) -> None:
    g = CircuitBreakerGuard(
        fail_mode_override="warn", failure_threshold=1, cooldown_s=10.0,
    )
    g.evaluate(_fail())
    g.evaluate(_probe())
    log = audit_tmp / "circuit_breaker_guard.jsonl"
    assert not log.exists()


def test_audit_log_warn_plus_log_writes(audit_tmp: Path) -> None:
    g = CircuitBreakerGuard(
        fail_mode_override="warn+log",
        failure_threshold=1, cooldown_s=10.0,
    )
    g.evaluate(_fail())
    g.evaluate(_probe())
    log = audit_tmp / "circuit_breaker_guard.jsonl"
    assert log.exists()
    rec = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert rec["guard"] == "circuit_breaker_guard"
    assert any(f["type"] == "circuit_open" for f in rec["findings"])


def test_audit_log_hard_deny_writes(audit_tmp: Path) -> None:
    g = CircuitBreakerGuard(
        fail_mode_override="hard_deny",
        failure_threshold=1, cooldown_s=10.0,
    )
    g.evaluate(_fail())
    g.evaluate(_probe())
    log = audit_tmp / "circuit_breaker_guard.jsonl"
    assert log.exists()


# ══════════════════════════════════════════════════════════════
#  9. Escape hatch
# ══════════════════════════════════════════════════════════════


def test_escape_hatch_per_line_pattern_skips_scan(audit_tmp: Path) -> None:
    g = CircuitBreakerGuard(fail_mode_override="hard_deny")
    payload_str = "api.foo  # CONCINNO_DISABLE:circuit_breaker:debugging"
    r = g.evaluate(payload_str)
    assert r.decision == "accept"


def test_escape_hatch_base_broad_form_works(audit_tmp: Path) -> None:
    """Base class also recognises ``# CONCINNO_DISABLE:`` (no suffix)."""
    g = CircuitBreakerGuard(fail_mode_override="hard_deny")
    payload_str = "api.foo  # CONCINNO_DISABLE: ops_override"
    r = g.evaluate(payload_str)
    assert r.escaped is True
    assert r.decision == "accept"


# ══════════════════════════════════════════════════════════════
#  10. Payload coercion
# ══════════════════════════════════════════════════════════════


def test_string_payload_treated_as_resource(audit_tmp: Path) -> None:
    g = CircuitBreakerGuard(fail_mode_override="warn")
    r = g.evaluate("api.bar")
    assert r.decision == "accept"
    assert "api.bar" in g.list_resources()


def test_bytes_payload_treated_as_resource(audit_tmp: Path) -> None:
    g = CircuitBreakerGuard(fail_mode_override="warn")
    r = g.evaluate(b"api.baz")
    assert r.decision == "accept"
    assert "api.baz" in g.list_resources()


def test_dict_missing_resource_yields_malformed(audit_tmp: Path) -> None:
    g = CircuitBreakerGuard(fail_mode_override="warn")
    r = g.evaluate({"outcome": "success"})
    assert any(f.type == "malformed_payload" for f in r.findings)


def test_dict_invalid_outcome_yields_malformed(audit_tmp: Path) -> None:
    g = CircuitBreakerGuard(fail_mode_override="warn")
    r = g.evaluate({"resource": "api.x", "outcome": "kaput"})
    assert any(f.type == "malformed_payload" for f in r.findings)


def test_unknown_payload_type_yields_malformed(audit_tmp: Path) -> None:
    g = CircuitBreakerGuard(fail_mode_override="warn")
    r = g.evaluate(12345)  # type: ignore[arg-type]
    assert any(f.type == "malformed_payload" for f in r.findings)


# ══════════════════════════════════════════════════════════════
#  11. snapshot / list_resources / reset
# ══════════════════════════════════════════════════════════════


def test_snapshot_returns_current_state(audit_tmp: Path) -> None:
    clock = _FakeClock()
    g = CircuitBreakerGuard(
        fail_mode_override="warn",
        failure_threshold=2, cooldown_s=10.0, time_source=clock,
    )
    g.evaluate(_fail())
    snap = g.snapshot("api.foo")
    assert snap.consecutive_failures == 1
    assert snap.state is CircuitState.CLOSED


def test_reset_specific_resource(audit_tmp: Path) -> None:
    g = CircuitBreakerGuard(fail_mode_override="warn", failure_threshold=2)
    g.evaluate(_fail("api.x"))
    g.evaluate(_fail("api.y"))
    g.reset("api.x")
    assert g.snapshot("api.x").consecutive_failures == 0
    assert g.snapshot("api.y").consecutive_failures == 1


def test_reset_all_resources(audit_tmp: Path) -> None:
    g = CircuitBreakerGuard(fail_mode_override="warn")
    g.evaluate(_fail("api.x"))
    g.evaluate(_fail("api.y"))
    g.reset()
    assert g.snapshot("api.x").consecutive_failures == 0
    assert g.snapshot("api.y").consecutive_failures == 0


def test_share_state_with_other_guard(audit_tmp: Path) -> None:
    g1 = CircuitBreakerGuard(fail_mode_override="warn", failure_threshold=2)
    g2 = CircuitBreakerGuard(
        fail_mode_override="warn", failure_threshold=2, share_state_with=g1,
    )
    g1.evaluate(_fail("api.shared"))
    g2.evaluate(_fail("api.shared"))  # 2nd failure on shared state → OPEN
    assert g1.snapshot("api.shared").state is CircuitState.OPEN
    assert g2.snapshot("api.shared").state is CircuitState.OPEN


# ══════════════════════════════════════════════════════════════
#  12. ZIQ outcome bus emit
# ══════════════════════════════════════════════════════════════


def test_ziq_emit_disabled_via_env(audit_tmp: Path) -> None:
    """``CONCINNO_ZIQ_BUS_DISABLED=1`` (set by audit_tmp) silences emit."""
    g = CircuitBreakerGuard(fail_mode_override="warn+log")
    # Should not raise even with no subscriber.
    g.evaluate(_fail())


def test_ziq_emit_subscriber_invoked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("CONCINNO_AUDIT_DIR", str(tmp_path))
    monkeypatch.delenv("CONCINNO_ZIQ_BUS_DISABLED", raising=False)
    from concinno.ziq_outcome_bus import Outcome, ZIQOutcomeBus
    ZIQOutcomeBus._reset_for_testing()
    bus = ZIQOutcomeBus.get_bus()
    received: list[Outcome] = []
    bus.subscribe("security.circuit_breaker_guard", received.append)

    g = CircuitBreakerGuard(
        fail_mode_override="warn+log",
        failure_threshold=1, cooldown_s=10.0,
    )
    g.evaluate(_fail())  # accept (counter ticks but not over yet → wait...)
    g.evaluate(_probe())  # OPEN, finding=circuit_open → warn
    # Two emits: first call accept, second warn.
    assert len(received) == 2
    assert received[-1].metadata["decision"] == "warn"
    ZIQOutcomeBus._reset_for_testing()


# ══════════════════════════════════════════════════════════════
#  13. FEATURE_META registration
# ══════════════════════════════════════════════════════════════


def test_feature_meta_registered() -> None:
    from concinno.feature_config import FEATURE_META

    assert "circuit_breaker_guard" in FEATURE_META
    meta = FEATURE_META["circuit_breaker_guard"]
    assert meta["category"] == "security"
    # 4.0.0 default-off-gates SEMVER baseline.
    assert meta["enabled"] is False
    assert meta["ziq_autotunable"] is True
    assert meta["cosmetic"] is False
    for param in (
        "max_calls", "window_s", "failure_threshold",
        "cooldown_s", "backoff_max_s",
    ):
        assert param in meta["params"]


def test_feature_meta_in_default_off() -> None:
    from concinno.feature_config import DEFAULT_OFF_4_0_0

    assert "circuit_breaker_guard" in DEFAULT_OFF_4_0_0


def test_feature_meta_strict_profile_hard_deny() -> None:
    from concinno.feature_config import get_fail_mode

    assert get_fail_mode(
        "circuit_breaker_guard", profile="strict",
    ) == "hard_deny"


def test_feature_meta_paranoid_profile_hard_deny() -> None:
    from concinno.feature_config import get_fail_mode

    assert get_fail_mode(
        "circuit_breaker_guard", profile="paranoid",
    ) == "hard_deny"


# ══════════════════════════════════════════════════════════════
#  14. Probe-only payload (no outcome) preserves state
# ══════════════════════════════════════════════════════════════


def test_probe_payload_does_not_tick_failure(audit_tmp: Path) -> None:
    g = CircuitBreakerGuard(fail_mode_override="warn", failure_threshold=2)
    for _ in range(10):
        g.evaluate(_probe())
    assert g.snapshot("api.foo").consecutive_failures == 0


def test_probe_payload_still_ticks_rate_window(audit_tmp: Path) -> None:
    g = CircuitBreakerGuard(
        fail_mode_override="warn", max_calls=3, window_s=60.0,
    )
    for _ in range(3):
        g.evaluate(_probe())
    r = g.evaluate(_probe())
    assert any(f.type == "rate_limit_exceeded" for f in r.findings)
