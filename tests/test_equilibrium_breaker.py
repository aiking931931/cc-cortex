"""Tests for EquilibriumBreaker (dynamic equilibrium circuit breaker) and MilestoneGate."""

from __future__ import annotations

from cc_cortex.core.state_store import StateStore
from cc_cortex.equilibrium_breaker import (
    COOLDOWN_STEPS,
    PRESSURE_THRESHOLD,
    EquilibriumBreaker,
)
from cc_cortex.guards.base import GuardAction, GuardCategory, GuardContext
from cc_cortex.milestone_gate import MilestoneGate


def _ctx(**kw) -> GuardContext:
    defaults = {
        "tool_name": "Read",
        "tool_input": {},
        "session_id": "test1234-abcd",
        "hook_event": "PreToolUse",
        "cache_dir": "",
    }
    defaults.update(kw)
    return GuardContext(**defaults)


# ═══════════════════════════════════════════════════════════
# EquilibriumBreaker
# ═══════════════════════════════════════════════════════════


class TestEquilibriumBreaker:
    def test_initial_state(self, tmp_path):
        store = StateStore(str(tmp_path))
        breaker = EquilibriumBreaker(store, "sess1234")
        assert breaker.pressure == 0.0
        assert breaker.cooldown_remaining == 0
        assert not breaker.is_tripped

    def test_deny_increases_pressure(self, tmp_path):
        store = StateStore(str(tmp_path))
        b = EquilibriumBreaker(store, "sess1234")
        b.record_deny()
        assert b.pressure == 1.0
        b.record_deny()
        assert b.pressure == 2.0

    def test_allow_decreases_pressure(self, tmp_path):
        store = StateStore(str(tmp_path))
        b = EquilibriumBreaker(store, "sess1234")
        # Build up pressure
        for _ in range(3):
            b.record_deny()
        assert b.pressure == 3.0
        b.record_allow()
        assert b.pressure == 2.8

    def test_trips_at_threshold(self, tmp_path):
        store = StateStore(str(tmp_path))
        b = EquilibriumBreaker(store, "sess1234")
        for _ in range(int(PRESSURE_THRESHOLD)):
            b.record_deny()
        assert b.is_tripped
        assert b.cooldown_remaining == COOLDOWN_STEPS

    def test_security_never_skipped(self, tmp_path):
        store = StateStore(str(tmp_path))
        b = EquilibriumBreaker(store, "sess1234")
        # Trip the breaker
        for _ in range(int(PRESSURE_THRESHOLD)):
            b.record_deny()
        assert b.is_tripped
        # SECURITY should never be skipped
        assert not b.should_skip(GuardCategory.SECURITY)

    def test_quality_skipped_when_tripped(self, tmp_path):
        store = StateStore(str(tmp_path))
        b = EquilibriumBreaker(store, "sess1234")
        for _ in range(int(PRESSURE_THRESHOLD)):
            b.record_deny()
        assert b.should_skip(GuardCategory.QUALITY)

    def test_cognitive_skipped_when_tripped(self, tmp_path):
        store = StateStore(str(tmp_path))
        b = EquilibriumBreaker(store, "sess1234")
        for _ in range(int(PRESSURE_THRESHOLD)):
            b.record_deny()
        assert b.should_skip(GuardCategory.COGNITIVE)

    def test_cooldown_decrements_via_tick(self, tmp_path):
        store = StateStore(str(tmp_path))
        b = EquilibriumBreaker(store, "sess1234")
        for _ in range(int(PRESSURE_THRESHOLD)):
            b.record_deny()
        initial = b.cooldown_remaining
        b.tick()  # tick() decrements, not should_skip()
        assert b.cooldown_remaining == initial - 1

    def test_cooldown_expires(self, tmp_path):
        store = StateStore(str(tmp_path))
        b = EquilibriumBreaker(store, "sess1234")
        for _ in range(int(PRESSURE_THRESHOLD)):
            b.record_deny()
        # Exhaust cooldown via tick() (one per tool-call)
        for _ in range(COOLDOWN_STEPS):
            b.tick()
        # Should no longer skip
        assert not b.should_skip(GuardCategory.QUALITY)

    def test_should_skip_does_not_decrement(self, tmp_path):
        """should_skip() must NOT decrement cooldown (tick() does that)."""
        store = StateStore(str(tmp_path))
        b = EquilibriumBreaker(store, "sess1234")
        for _ in range(int(PRESSURE_THRESHOLD)):
            b.record_deny()
        initial = b.cooldown_remaining
        # Multiple should_skip calls should NOT change cooldown
        b.should_skip(GuardCategory.QUALITY)
        b.should_skip(GuardCategory.QUALITY)
        b.should_skip(GuardCategory.QUALITY)
        assert b.cooldown_remaining == initial

    def test_pressure_decays_during_cooldown(self, tmp_path):
        """record_allow() should work during cooldown to decay pressure."""
        store = StateStore(str(tmp_path))
        b = EquilibriumBreaker(store, "sess1234")
        for _ in range(int(PRESSURE_THRESHOLD)):
            b.record_deny()
        assert b.is_tripped
        initial_pressure = b.pressure
        b.record_allow()
        assert b.pressure < initial_pressure

    def test_reset(self, tmp_path):
        store = StateStore(str(tmp_path))
        b = EquilibriumBreaker(store, "sess1234")
        for _ in range(int(PRESSURE_THRESHOLD)):
            b.record_deny()
        assert b.is_tripped
        b.reset()
        assert not b.is_tripped
        assert b.pressure == 0.0

    def test_pressure_capped(self, tmp_path):
        store = StateStore(str(tmp_path))
        b = EquilibriumBreaker(store, "sess1234")
        for _ in range(100):
            b.record_deny()
        assert b.pressure <= 20.0

    def test_not_tripped_allows_quality(self, tmp_path):
        store = StateStore(str(tmp_path))
        b = EquilibriumBreaker(store, "sess1234")
        b.record_deny()  # pressure=1, below threshold
        assert not b.should_skip(GuardCategory.QUALITY)


# ═══════════════════════════════════════════════════════════
# MilestoneGate
# ═══════════════════════════════════════════════════════════


class TestMilestoneGate:
    def test_no_injection_before_milestone(self, tmp_path):
        gate = MilestoneGate()
        ctx = _ctx(cache_dir=str(tmp_path))
        # Steps 1-19 should not inject (GREEN zone = every 20)
        for _ in range(19):
            result = gate.check(ctx)
            assert result is None

    def test_injection_at_milestone(self, tmp_path):
        gate = MilestoneGate()
        ctx = _ctx(cache_dir=str(tmp_path))
        # Run 20 steps
        result = None
        for _ in range(20):
            result = gate.check(ctx)
        assert result is not None
        assert result.action == GuardAction.ALLOW
        assert "D3" in result.context

    def test_yellow_zone_frequency(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CC_TOKEN_ZONE", "YELLOW")
        gate = MilestoneGate()
        ctx = _ctx(cache_dir=str(tmp_path))
        # YELLOW = every 10 steps
        for _ in range(9):
            result = gate.check(ctx)
            assert result is None
        result = gate.check(ctx)
        assert result is not None
        assert "D3" in result.context

    def test_orange_zone_frequency(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CC_TOKEN_ZONE", "ORANGE")
        gate = MilestoneGate()
        ctx = _ctx(cache_dir=str(tmp_path))
        # ORANGE = every 5 steps
        for _ in range(4):
            result = gate.check(ctx)
            assert result is None
        result = gate.check(ctx)
        assert result is not None

    def test_no_cache_dir_passes(self):
        gate = MilestoneGate()
        ctx = _ctx(cache_dir="")
        result = gate.check(ctx)
        assert result is None

    def test_task_name_included(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CC_SESSION_TASK", "Fix login bug")
        gate = MilestoneGate()
        ctx = _ctx(cache_dir=str(tmp_path))
        for _ in range(20):
            result = gate.check(ctx)
        assert "Fix login bug" in result.context
