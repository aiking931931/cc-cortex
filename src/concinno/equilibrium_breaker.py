"""concinno.equilibrium_breaker — Dynamic equilibrium circuit breaker.

@module equilibrium_breaker
@responsibility Prevent deny storms by tracking global deny pressure.
    When pressure exceeds threshold, temporarily suspend QUALITY gates
    (keep SECURITY always on). Implements dynamic equilibrium: restriction
    and permission must stay in balance.
@dependencies concinno.core.state_store, concinno.guards.base
@exports EquilibriumBreaker

Bug fixes (2026-03-26 red team):
  - Cooldown decrements per tool-call, not per guard-check
  - Pressure decays during cooldown via record_allow()
  - Cooldown tracked separately from skip logic
"""

from __future__ import annotations

from concinno.core.state_store import StateStore
from concinno.guards.base import GuardCategory
from concinno.ziq_control.equilibrium_adapter import should_freeze_pressure

_NS = "equilibrium_breaker"

# ── Thresholds ──────────────────────────────────────────────────
# Pressure increases by 1 on each deny, decreases by 0.2 on each allow.
# This asymmetry ensures restriction builds fast but permission
# restores slowly — converges instead of oscillating.

DENY_PRESSURE_INCREMENT = 1.0
ALLOW_PRESSURE_DECREMENT = 0.2

# When pressure >= this, QUALITY gates are suspended for COOLDOWN_STEPS.
PRESSURE_THRESHOLD = 5.0

# Number of **tool calls** (not guard checks) to suspend QUALITY gates.
COOLDOWN_STEPS = 10

# Maximum pressure cap (prevent runaway numbers).
PRESSURE_CAP = 20.0


class EquilibriumBreaker:
    """Global deny-storm circuit breaker.

    Dynamic equilibrium: when deny pressure builds too fast, the system
    temporarily relaxes QUALITY gates to let the agent recover.
    SECURITY gates are NEVER suspended.

    Fixed in v2 (2026-03-26):
      - cooldown decrements once per tick(), not per should_skip() call
      - record_allow() always callable (decays pressure during cooldown)
      - tick() must be called once per tool-call by the pipeline

    Usage:
        breaker = EquilibriumBreaker(state_store, session_id)
        breaker.tick()  # once per tool call — decrements cooldown
        if breaker.should_skip(guard):
            continue  # skip this guard
        # ... run guard ...
        if result.action == DENY:
            breaker.record_deny()
        else:
            breaker.record_allow()
    """

    def __init__(self, store: StateStore, session_id: str) -> None:
        self._store = store
        self._sid = session_id
        self._state: dict | None = None

    def _load(self) -> dict:
        if self._state is None:
            self._state = self._store.read(
                _NS,
                self._sid,
                default={
                    "pressure": 0.0,
                    "cooldown": 0,
                    "pressure_history": [],
                },
            )
            # Migration: add pressure_history if missing
            if "pressure_history" not in self._state:
                self._state["pressure_history"] = []
        return self._state

    def _save(self) -> None:
        if self._state is not None:
            self._store.write(_NS, self._sid, self._state)

    @property
    def pressure(self) -> float:
        return self._load().get("pressure", 0.0)

    @property
    def cooldown_remaining(self) -> int:
        return self._load().get("cooldown", 0)

    @property
    def is_tripped(self) -> bool:
        """True when breaker is active (QUALITY gates suspended)."""
        return self.cooldown_remaining > 0

    def tick(self) -> None:
        """Call once per tool-call. Decrements cooldown by 1.

        This ensures cooldown counts tool-calls, not guard-checks.
        The pipeline must call tick() before iterating guards.
        """
        state = self._load()
        cd = state.get("cooldown", 0)
        if cd > 0:
            state["cooldown"] = cd - 1
            self._save()

    def should_skip(self, guard_category: GuardCategory) -> bool:
        """Check if a guard should be skipped due to breaker state.

        SECURITY guards are NEVER skipped.
        QUALITY and COGNITIVE guards are skipped when breaker is tripped.
        Does NOT decrement cooldown (tick() does that).
        """
        if guard_category == GuardCategory.SECURITY:
            return False
        return self._load().get("cooldown", 0) > 0

    def _track_pressure(self, state: dict) -> None:
        """Append current pressure to history (max 30 entries)."""
        history: list[float] = state.get("pressure_history", [])
        history.append(state.get("pressure", 0.0))
        if len(history) > 30:
            history = history[-30:]
        state["pressure_history"] = history

    def record_deny(self) -> None:
        """Record a deny event. Pressure increases.

        TCT freeze: if oscillation detected, pressure stays unchanged.
        """
        state = self._load()
        history: list[float] = state.get("pressure_history", [])

        # TCT: freeze pressure during oscillation
        if should_freeze_pressure(history):
            self._track_pressure(state)
            self._save()
            return

        pressure = min(
            state.get("pressure", 0.0) + DENY_PRESSURE_INCREMENT,
            PRESSURE_CAP,
        )
        state["pressure"] = pressure

        # Trip breaker if threshold exceeded
        if pressure >= PRESSURE_THRESHOLD and state.get("cooldown", 0) == 0:
            state["cooldown"] = COOLDOWN_STEPS

        self._track_pressure(state)
        self._save()

    def record_allow(self) -> None:
        """Record an allow event. Pressure decreases.

        Always callable — even during cooldown, to decay pressure
        so the breaker doesn't re-trip immediately after cooldown ends.
        TCT freeze: if oscillation detected, pressure stays unchanged.
        """
        state = self._load()
        history: list[float] = state.get("pressure_history", [])

        # TCT: freeze pressure during oscillation
        if should_freeze_pressure(history):
            self._track_pressure(state)
            self._save()
            return

        pressure = max(
            state.get("pressure", 0.0) - ALLOW_PRESSURE_DECREMENT,
            0.0,
        )
        state["pressure"] = pressure
        self._track_pressure(state)
        self._save()

    def reset(self) -> None:
        """Full reset (e.g. new session or manual override)."""
        self._state = {"pressure": 0.0, "cooldown": 0}
        self._save()
