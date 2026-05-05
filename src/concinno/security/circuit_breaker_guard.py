"""concinno.security.circuit_breaker_guard — rate-limit + circuit-breaker.

@module security.circuit_breaker_guard
@responsibility Stateful guard (4.4.0 Plan B Week 2) that enforces two
    related runtime safety invariants on per-resource call streams:

      1. **Rate limit** — at most ``max_calls`` calls within
         ``window_s`` seconds per resource. Uses a sliding deque of
         monotonic timestamps so the counter is accurate at the
         boundary, not just within fixed-size buckets.
      2. **Circuit breaker** — on consecutive failure runs of length
         ``failure_threshold`` the breaker flips ``CLOSED → OPEN`` and
         denies further calls for ``cooldown_s`` seconds. After
         cooldown the next call is admitted as a ``HALF_OPEN`` probe;
         success closes the circuit, failure re-opens it with the same
         cooldown (Hystrix pattern).

    Concrete callers wrap external dependencies (HTTP API, RPC,
    subprocess, LLM provider) and ``evaluate(...)`` once per call with
    the resource name and the outcome of the *previous* attempt. The
    guard's :meth:`scan` returns at most one :class:`Finding` per call:
    ``rate_limit_exceeded``, ``circuit_open``, or ``half_open_probe``
    (a low-severity informational finding so audit consumers can chart
    probe events).

@dependencies stdlib only (``collections.deque``, ``threading``,
    ``time``, ``dataclasses``, ``enum``, ``typing``) plus the
    ``PolicyGate`` base class. **Zero runtime deps** per Concinno
    contributor rule #4.

@exports
    CircuitState (str enum), CircuitBreakerGuard,
    CircuitBreakerFinding (re-export of :class:`Finding`),
    CallRecord, CircuitBreakerSnapshot

The guard inherits :class:`PolicyGate` so it gains the standard 4-tier
fail-mode chain (``silent`` / ``warn`` / ``warn+log`` / ``hard_deny``),
profile-aware fail-mode resolution, the
``# CONCINNO_DISABLE:circuit_breaker:<reason>`` per-payload escape, the
audit log with size rotation, and the ZIQ outcome bus emit. The state
machine is the only thing this module owns.

Design notes
------------
* **Stateful per instance**, not per process. Multiple guards can
  share state by passing the same :class:`_BreakerStateRegistry` (we
  expose ``share_state_with`` for callers that wrap a connection pool
  with several wrapper instances). The default is an instance-private
  registry — mirroring what :class:`SSRFGuard` does for caches.
* **Monotonic clock** for rate-limit windows so the counter is robust
  to wall-clock skew. Configurable via ``time_source`` for
  deterministic tests.
* **Half-open probe gating**: at most one in-flight probe per
  resource. Concurrent callers see ``circuit_open`` until the probe
  completes — preserves the Hystrix invariant that a thundering herd
  cannot retry in lockstep. The probe permit is granted at
  ``evaluate`` time and consumed by the outcome reported on the next
  ``evaluate`` call for the same resource.
* **Failure outcome interpretation**: callers explicitly pass
  ``outcome="success"|"failure"|"timeout"|"unknown"``. ``"timeout"``
  counts as a failure for breaker purposes but is recorded with its
  own audit field so SIEM can distinguish slow-from-broken. ``"unknown"``
  bypasses the failure counter — useful when the wrapper itself
  crashed before the dependency was reached.
* **Observation-only payloads**: a payload that omits ``outcome``
  (e.g. ``{"resource": "api.foo"}``) is treated as a pre-call probe.
  The breaker decides admit/deny based on the current state and the
  rate-limit window without mutating the failure counter; the rate
  counter still ticks (the call is happening regardless of how its
  outcome will be reported later).
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Literal

from concinno.security.policy_gate import (
    FailMode,
    Finding,
    PolicyGate,
    Severity,
)
from concinno.security.policy_gate import (
    Finding as CircuitBreakerFinding,
)

__all__ = [
    "CallRecord",
    "CircuitBreakerFinding",
    "CircuitBreakerGuard",
    "CircuitBreakerSnapshot",
    "CircuitState",
    "DEFAULT_BACKOFF_BASE_S",
    "DEFAULT_BACKOFF_MAX_S",
    "DEFAULT_COOLDOWN_S",
    "DEFAULT_FAILURE_THRESHOLD",
    "DEFAULT_MAX_CALLS",
    "DEFAULT_WINDOW_S",
    "reset_shared_breaker_registry",
]


# ── Defaults exposed as module constants ───────────────────────────

#: Default rate-limit window in seconds.
DEFAULT_WINDOW_S: float = 60.0

#: Default rate-limit cap per window per resource.
DEFAULT_MAX_CALLS: int = 60

#: Default consecutive-failure count that flips ``CLOSED → OPEN``.
DEFAULT_FAILURE_THRESHOLD: int = 5

#: Default cooldown before the next probe is admitted.
DEFAULT_COOLDOWN_S: float = 30.0

#: Exponential backoff base (each successive open multiplies by 2 up
#: to ``DEFAULT_BACKOFF_MAX_S``).
DEFAULT_BACKOFF_BASE_S: float = 1.0

#: Backoff ceiling per Hystrix tradition.
DEFAULT_BACKOFF_MAX_S: float = 60.0


# ── Per-line escape regex ───────────────────────────────────────────

# Matches ``# CONCINNO_DISABLE:circuit_breaker:<reason>``. Mirrors the
# ``deserialize_guard`` escape shape so operators only learn one form.
# The base class also recognises the broader ``# CONCINNO_DISABLE:``
# token (no guard suffix); that broader form short-circuits the entire
# scan via :meth:`PolicyGate.evaluate`.
_PER_LINE_ESCAPE = re.compile(
    r"#\s*CONCINNO_DISABLE\s*:\s*circuit_breaker\b",
    re.IGNORECASE,
)


# ── State enum ─────────────────────────────────────────────────────


class CircuitState(str, Enum):
    """Hystrix three-state machine.

    Transitions::

        CLOSED ── (failure_count >= threshold) ───────► OPEN
        OPEN   ── (now >= open_until) ────────────────► HALF_OPEN
        HALF_OPEN ── (probe success) ─────────────────► CLOSED
        HALF_OPEN ── (probe failure) ─────────────────► OPEN  (cooldown × 2)

    Values are the public string identifiers used in audit logs and
    ZIQ outcome metadata. Never rename — only append.
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


# ── Outcome literal ────────────────────────────────────────────────

#: Caller-reported outcome of the previous call. ``"unknown"``
#: bypasses the failure counter (wrapper-level crash before reaching
#: the dependency). ``"timeout"`` counts as a failure but is recorded
#: separately in audit.
CallOutcome = Literal["success", "failure", "timeout", "unknown"]

_VALID_OUTCOMES: frozenset[str] = frozenset({
    "success", "failure", "timeout", "unknown",
})


# ── Data classes ───────────────────────────────────────────────────


@dataclass(frozen=True)
class CallRecord:
    """One call observation in the sliding window.

    ``ts`` is a monotonic-clock reading (not wall clock) so window
    arithmetic is robust to clock skew. Callers pass payloads with
    wall-clock timestamps; the guard converts on entry by reading its
    own ``time_source``.
    """

    ts: float
    outcome: CallOutcome


@dataclass
class CircuitBreakerSnapshot:
    """Read-only inspection of a resource's current breaker state.

    Returned by :meth:`CircuitBreakerGuard.snapshot` so callers can
    chart breaker health without poking the internal registry.
    """

    resource: str
    state: CircuitState
    consecutive_failures: int
    open_until: float
    cooldown_s: float
    calls_in_window: int
    window_start: float


@dataclass
class _BreakerState:
    """Internal mutable state for one resource. Guarded by lock."""

    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    #: Monotonic timestamp at which the breaker leaves OPEN.
    open_until: float = 0.0
    #: Current backoff (seconds). Doubles on each re-open up to the
    #: configured ceiling.
    cooldown_s: float = DEFAULT_COOLDOWN_S
    #: True when a HALF_OPEN probe has been admitted but its outcome
    #: has not yet been reported. While True, further callers see
    #: ``circuit_open`` (one probe at a time).
    probe_in_flight: bool = False
    #: Sliding window of call timestamps for rate-limit accounting.
    call_log: deque[float] = field(default_factory=deque)


class _BreakerStateRegistry:
    """Per-resource state map. Guards subclass-private by default but
    can be shared across instances via ``share_state_with``.
    """

    def __init__(self) -> None:
        self._states: dict[str, _BreakerState] = {}
        self._lock = threading.Lock()

    def get_or_create(self, resource: str) -> _BreakerState:
        with self._lock:
            st = self._states.get(resource)
            if st is None:
                st = _BreakerState()
                self._states[resource] = st
            return st

    def reset(self, resource: str | None = None) -> None:
        """Forget state for ``resource`` (or all when ``None``)."""
        with self._lock:
            if resource is None:
                self._states.clear()
            else:
                self._states.pop(resource, None)

    def snapshot_keys(self) -> list[str]:
        with self._lock:
            return list(self._states.keys())


# ── Shared (process-wide) registry singleton ───────────────────────


_shared_registry_lock = threading.Lock()
_shared_registry_instance: _BreakerStateRegistry | None = None


def _shared_breaker_registry() -> _BreakerStateRegistry:
    """Return the process-wide shared registry (lazy-instantiated).

    4.4.0+ default for :class:`CircuitBreakerGuard`. Two default-
    constructed guards now converge on the same registry so they
    agree on breaker state for a logical resource — the legacy
    behaviour (per-instance registry) is preserved when the operator
    passes ``share_state_with=False`` to opt out.

    Tests that need a clean slate can monkey-patch this module's
    ``_shared_registry_instance`` to ``None`` between cases.
    """
    global _shared_registry_instance
    if _shared_registry_instance is None:
        with _shared_registry_lock:
            if _shared_registry_instance is None:
                _shared_registry_instance = _BreakerStateRegistry()
    return _shared_registry_instance


def reset_shared_breaker_registry() -> None:
    """Drop the shared registry singleton — primarily for tests.

    A subsequent default-constructed :class:`CircuitBreakerGuard`
    will re-create it. Has no effect on guards that explicitly
    passed ``share_state_with=<sibling>`` or ``share_state_with=False``.
    """
    global _shared_registry_instance
    with _shared_registry_lock:
        _shared_registry_instance = None


# ── Guard ──────────────────────────────────────────────────────────


class CircuitBreakerGuard(PolicyGate):
    """Stateful rate-limit + circuit-breaker policy gate.

    Wrap external dependencies in this guard at the call boundary.
    For each call, build a payload dict::

        {
            "resource": "api.openai.chat",
            "outcome": "success" | "failure" | "timeout" | "unknown",
        }

    Then dispatch::

        result = guard.evaluate(payload)
        if result.decision == "deny":
            raise CircuitOpenError(result.reason)

    The ``outcome`` field reports the result of the *previous* call to
    that resource (HALF_OPEN probe outcome when the breaker just
    re-admitted traffic, or whatever the last fully-completed call
    returned otherwise). Callers that want a stateless pre-flight
    check can omit ``outcome`` — the guard will only enforce the rate
    limit and the breaker state, leaving the failure counter
    untouched.

    Args:
        profile: Active feature-toggle profile name. Forwarded to the
            base class for fail-mode resolution.
        fail_mode_override: Pin the fail-mode for this instance. Tests
            use this to exercise each branch.
        max_calls: Cap on calls per ``window_s`` per resource. Set to
            ``0`` to disable rate-limit checking.
        window_s: Sliding-window length in seconds. Must be positive.
        failure_threshold: Consecutive ``failure``/``timeout`` outcomes
            that flip the breaker open. Must be ``>= 1``.
        cooldown_s: Seconds the breaker stays OPEN before the next
            HALF_OPEN probe is admitted. Doubled on each re-open up to
            ``backoff_max_s``.
        backoff_base_s: Initial cooldown (used when the breaker has
            never opened before for this resource). Defaults to
            :data:`DEFAULT_BACKOFF_BASE_S`.
        backoff_max_s: Cooldown ceiling. Must be ``>= backoff_base_s``.
        rate_limit_severity: Severity stamped on rate-limit findings.
        circuit_open_severity: Severity stamped on circuit-open
            findings.
        share_state_with: Reuse another guard's state registry. Useful
            when several wrapper instances guard the same logical
            resource.
        time_source: Callable returning monotonic seconds. Override
            for deterministic tests; defaults to ``time.monotonic``.
    """

    name: str = "circuit_breaker_guard"

    def __init__(
        self,
        profile: str = "lite",
        fail_mode_override: FailMode | None = None,
        *,
        max_calls: int = DEFAULT_MAX_CALLS,
        window_s: float = DEFAULT_WINDOW_S,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        cooldown_s: float = DEFAULT_COOLDOWN_S,
        backoff_base_s: float = DEFAULT_BACKOFF_BASE_S,
        backoff_max_s: float = DEFAULT_BACKOFF_MAX_S,
        rate_limit_severity: Severity = "medium",
        circuit_open_severity: Severity = "high",
        share_state_with: "CircuitBreakerGuard | bool | None" = None,
        time_source: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(
            profile=profile, fail_mode_override=fail_mode_override
        )

        if max_calls < 0:
            raise ValueError(f"max_calls must be >= 0, got {max_calls!r}")
        if window_s <= 0:
            raise ValueError(f"window_s must be > 0, got {window_s!r}")
        if failure_threshold < 1:
            raise ValueError(
                f"failure_threshold must be >= 1, got {failure_threshold!r}"
            )
        if cooldown_s < 0:
            raise ValueError(f"cooldown_s must be >= 0, got {cooldown_s!r}")
        if backoff_base_s < 0:
            raise ValueError(
                f"backoff_base_s must be >= 0, got {backoff_base_s!r}"
            )
        if backoff_max_s < backoff_base_s:
            raise ValueError(
                f"backoff_max_s ({backoff_max_s}) must be >= "
                f"backoff_base_s ({backoff_base_s})"
            )
        if rate_limit_severity not in {"low", "medium", "high", "critical"}:
            raise ValueError(
                f"rate_limit_severity {rate_limit_severity!r} invalid"
            )
        if circuit_open_severity not in {"low", "medium", "high", "critical"}:
            raise ValueError(
                f"circuit_open_severity {circuit_open_severity!r} invalid"
            )

        self._max_calls = int(max_calls)
        self._window_s = float(window_s)
        self._failure_threshold = int(failure_threshold)
        self._initial_cooldown_s = float(cooldown_s)
        self._backoff_base_s = float(backoff_base_s)
        self._backoff_max_s = float(backoff_max_s)
        self._rate_limit_severity: Severity = rate_limit_severity
        self._circuit_open_severity: Severity = circuit_open_severity
        self._time_source = time_source if time_source is not None else time.monotonic

        if share_state_with is False:
            # Explicit opt-out — operator wants instance-private state
            # (test fixtures, multi-tenant isolation, intentional
            # divergent breaker tuning per call site). Pass
            # ``share_state_with=False`` to get the legacy per-instance
            # registry without naming a sibling guard.
            self._registry = _BreakerStateRegistry()
        elif isinstance(share_state_with, CircuitBreakerGuard):
            # Operator handed in a sibling guard — share that exact
            # registry. This branch existed in the legacy contract.
            self._registry = share_state_with._registry
        else:
            # Default (4.4.0+) — also covers ``share_state_with=None``
            # (left at constructor default) and the redundant
            # ``share_state_with=True`` (operator explicitly asking
            # for the shared registry). Both paths converge here so
            # two ``CircuitBreakerGuard()`` instances guarding
            # ``api.foo`` end up on the same breaker state. Previously
            # two default-constructed instances each started with
            # their own registry, which silently broke "same logical
            # resource, different wrapper" deployments (e.g. one
            # guard per worker thread / async task wrapping the
            # same upstream API).
            self._registry = _shared_breaker_registry()

    # ── PolicyGate hook ────────────────────────────────────────────

    def scan(
        self,
        payload: str | bytes | dict[str, Any],
    ) -> list[Finding]:
        """Apply the breaker + rate-limit state machine to ``payload``.

        Returns at most one finding per call. The base class
        :meth:`evaluate` turns the finding into ``warn`` / ``deny``
        per the resolved fail-mode.

        Payload schema::

            {
                "resource": str (required),
                "outcome": "success"|"failure"|"timeout"|"unknown" (optional),
            }

        Strings / bytes are interpreted as the resource name with
        outcome left unset (pre-call probe). Anything else returns a
        single ``low`` finding of type ``"malformed_payload"`` per the
        sibling-guard convention.
        """
        resource, outcome, malformed = self._parse_payload(payload)
        if malformed is not None:
            return [malformed]

        # Per-line escape support — when ``payload`` is a string the
        # base ``# CONCINNO_DISABLE:`` token is detected by the base
        # class. The narrower ``circuit_breaker``-suffixed form is
        # honoured here too so operators can tag scope explicitly.
        if isinstance(payload, str) and _PER_LINE_ESCAPE.search(payload):
            return []

        now = self._time_source()
        state = self._registry.get_or_create(resource)

        # The state machine + window mutation runs under the per-
        # registry lock so concurrent ``evaluate`` calls converge on
        # one canonical view. Lock is process-local — multi-process
        # writers to the same logical resource need a separate
        # coordination layer (out of scope per module docstring).
        with self._registry._lock:
            return self._step_state_machine(state, resource, outcome, now)

    # ── State-machine step (called under registry lock) ────────────

    def _step_state_machine(
        self,
        state: _BreakerState,
        resource: str,
        outcome: CallOutcome | None,
        now: float,
    ) -> list[Finding]:
        """Single state-machine tick. Caller must hold registry lock.

        Two distinct payload shapes drive different paths:

          * ``outcome`` reported (success / failure / timeout / unknown)
            → caller is *reporting* the result of a call they have
            already made. We update breaker state for the next caller
            and return ``accept`` (no decision to make for the call
            being reported, it has already happened).
          * ``outcome=None`` → caller is asking *should I attempt this
            call?* We refresh OPEN→HALF_OPEN aging, enforce the rate
            limit, and admit / deny.

        Mixing the two paths into a single evaluate would let a single
        ``failure`` report both trip the breaker AND deny the same
        report — a double-accounting bug Hystrix avoids by separating
        the *report* path from the *admit* path.
        """
        # ── Report path: caller is reporting an already-made call. ──
        if outcome is not None:
            self._apply_outcome(state, outcome, now)
            # Refresh aging so a callsite that reports right at the
            # cooldown boundary sees HALF_OPEN on its next probe.
            if state.state == CircuitState.OPEN and now >= state.open_until:
                state.state = CircuitState.HALF_OPEN
                state.probe_in_flight = False
            return []

        # ── Admit path: caller asking permission to make a new call. ──

        # Refresh aging — OPEN may have ticked into HALF_OPEN.
        if state.state == CircuitState.OPEN and now >= state.open_until:
            state.state = CircuitState.HALF_OPEN
            state.probe_in_flight = False

        # Still OPEN (cooldown not yet elapsed) → deny.
        if state.state == CircuitState.OPEN:
            return [self._make_open_finding(resource, state, now)]

        # HALF_OPEN — admit at most one concurrent probe.
        if state.state == CircuitState.HALF_OPEN:
            if state.probe_in_flight:
                return [self._make_open_finding(resource, state, now)]
            state.probe_in_flight = True
            self._tick_rate_window(state, now)
            return [
                Finding(
                    type="half_open_probe",
                    span=(-1, -1),
                    snippet=f"resource={resource!r}",
                    severity="low",
                    message=(
                        f"circuit breaker HALF_OPEN — admitting probe for "
                        f"{resource!r}; cooldown was {state.cooldown_s:.2f}s"
                    ),
                )
            ]

        # CLOSED — enforce rate limit then admit.
        rate_finding = self._enforce_rate_limit(state, resource, now)
        if rate_finding is not None:
            return [rate_finding]
        self._tick_rate_window(state, now)
        return []

    # ── Outcome application (caller holds lock) ────────────────────

    def _apply_outcome(
        self,
        state: _BreakerState,
        outcome: CallOutcome,
        now: float,
    ) -> None:
        """Update failure counter / breaker state from ``outcome``.

        Conventions::

            success           → failure counter resets, HALF_OPEN closes
            failure / timeout → failure counter ticks, HALF_OPEN re-opens
                                with backoff doubled
            unknown           → no-op on counter (wrapper-level crash)
        """
        if outcome == "unknown":
            return

        if state.state == CircuitState.HALF_OPEN:
            # Probe in progress — resolve it.
            state.probe_in_flight = False
            if outcome == "success":
                # Probe passed → close the circuit, reset cooldown.
                state.state = CircuitState.CLOSED
                state.consecutive_failures = 0
                state.cooldown_s = self._initial_cooldown_s
            else:
                # Probe failed → re-open with doubled cooldown.
                self._open_breaker(state, now, doubled=True)
            return

        # CLOSED path — normal accounting.
        if outcome == "success":
            state.consecutive_failures = 0
            state.cooldown_s = self._initial_cooldown_s
            return

        # failure / timeout
        state.consecutive_failures += 1
        if state.consecutive_failures >= self._failure_threshold:
            self._open_breaker(state, now, doubled=False)

    def _open_breaker(
        self, state: _BreakerState, now: float, *, doubled: bool,
    ) -> None:
        """Transition into OPEN and schedule the next probe slot.

        ``doubled=False`` opens for the first time (or after a clean
        close) — anchor on the operator-configured cooldown, not on
        the dataclass default. ``doubled=True`` follows a HALF_OPEN
        probe failure — multiply the *current* cooldown so successive
        probe failures grow geometrically until the backoff ceiling.
        """
        if doubled:
            new_cooldown = state.cooldown_s * 2
        else:
            # First time opening for this resource — anchor on the
            # operator-configured cooldown so a constructor passing
            # ``cooldown_s=10`` is honoured even though the dataclass
            # default is ``DEFAULT_COOLDOWN_S``.
            new_cooldown = max(self._initial_cooldown_s, self._backoff_base_s)
        state.cooldown_s = min(new_cooldown, self._backoff_max_s)
        state.state = CircuitState.OPEN
        state.open_until = now + state.cooldown_s
        state.probe_in_flight = False

    # ── Rate-limit helpers (caller holds lock) ─────────────────────

    def _enforce_rate_limit(
        self, state: _BreakerState, resource: str, now: float,
    ) -> Finding | None:
        """Return a finding when the rate limit would be exceeded."""
        if self._max_calls <= 0:
            return None
        self._purge_old_calls(state, now)
        if len(state.call_log) >= self._max_calls:
            return Finding(
                type="rate_limit_exceeded",
                span=(-1, -1),
                snippet=f"resource={resource!r}",
                severity=self._rate_limit_severity,
                message=(
                    f"rate limit exceeded — {len(state.call_log)} calls in "
                    f"{self._window_s:.2f}s window (max={self._max_calls})"
                ),
            )
        return None

    def _tick_rate_window(self, state: _BreakerState, now: float) -> None:
        """Append ``now`` to the sliding window after purging stale entries."""
        if self._max_calls <= 0:
            return
        self._purge_old_calls(state, now)
        state.call_log.append(now)

    def _purge_old_calls(self, state: _BreakerState, now: float) -> None:
        """Drop call timestamps older than ``window_s`` seconds ago."""
        cutoff = now - self._window_s
        log = state.call_log
        while log and log[0] < cutoff:
            log.popleft()

    # ── Finding factories ──────────────────────────────────────────

    def _make_open_finding(
        self, resource: str, state: _BreakerState, now: float,
    ) -> Finding:
        seconds_left = max(0.0, state.open_until - now)
        return Finding(
            type="circuit_open",
            span=(-1, -1),
            snippet=f"resource={resource!r}",
            severity=self._circuit_open_severity,
            message=(
                f"circuit OPEN for {resource!r} — "
                f"{seconds_left:.2f}s until next probe; "
                f"consecutive_failures={state.consecutive_failures}"
            ),
        )

    # ── Payload normalisation ──────────────────────────────────────

    def _parse_payload(
        self, payload: str | bytes | dict[str, Any],
    ) -> tuple[str, CallOutcome | None, Finding | None]:
        """Coerce the payload into ``(resource, outcome, malformed)``.

        ``malformed`` is non-None when the payload shape is unusable;
        the caller short-circuits and returns the finding without
        touching breaker state. Mirrors the
        ``deserialize_guard.scan(dict) → "malformed_payload"`` pattern.
        """
        if isinstance(payload, dict):
            resource = payload.get("resource")
            if not isinstance(resource, str) or not resource:
                return ("", None, _malformed("resource missing or non-string"))
            outcome_raw = payload.get("outcome")
            if outcome_raw is None:
                return (resource, None, None)
            if not isinstance(outcome_raw, str):
                return ("", None, _malformed(
                    f"outcome must be str, got {type(outcome_raw).__name__}"
                ))
            if outcome_raw not in _VALID_OUTCOMES:
                return ("", None, _malformed(
                    f"outcome {outcome_raw!r} not in {sorted(_VALID_OUTCOMES)}"
                ))
            # Validated above — narrow the literal for mypy.
            outcome: CallOutcome = outcome_raw  # type: ignore[assignment]
            return (resource, outcome, None)

        if isinstance(payload, str):
            text = payload.strip()
            if not text:
                return ("", None, _malformed("empty resource string"))
            return (text, None, None)

        if isinstance(payload, bytes):
            try:
                text = payload.decode("utf-8", errors="strict").strip()
            except UnicodeDecodeError:
                return ("", None, _malformed("payload bytes not UTF-8"))
            if not text:
                return ("", None, _malformed("empty resource string"))
            return (text, None, None)

        return ("", None, _malformed(
            f"CircuitBreakerGuard expects dict / str / bytes, got "
            f"{type(payload).__name__}"
        ))

    # ── Public inspection ──────────────────────────────────────────

    def snapshot(self, resource: str) -> CircuitBreakerSnapshot:
        """Return a read-only view of ``resource``'s breaker state.

        Creates the entry on first call (mirroring evaluate's
        get-or-create) so callers can chart from the moment they
        decide a resource exists.
        """
        now = self._time_source()
        st = self._registry.get_or_create(resource)
        with self._registry._lock:
            self._purge_old_calls(st, now)
            return CircuitBreakerSnapshot(
                resource=resource,
                state=st.state,
                consecutive_failures=st.consecutive_failures,
                open_until=st.open_until,
                cooldown_s=st.cooldown_s,
                calls_in_window=len(st.call_log),
                window_start=max(0.0, now - self._window_s),
            )

    def list_resources(self) -> list[str]:
        """Return all resource names with breaker state."""
        return self._registry.snapshot_keys()

    def reset(self, resource: str | None = None) -> None:
        """Forget breaker state for ``resource`` (or all when ``None``).

        Operator escape hatch — used by tests and by maintenance CLIs
        (``concinno breakers reset api.foo``) when an external SLA
        change invalidates the historical failure counter.
        """
        self._registry.reset(resource)


# ── Module-private helpers ─────────────────────────────────────────


def _malformed(message: str) -> Finding:
    """Construct the conventional ``malformed_payload`` finding.

    Matches :class:`PIIGuard` / :class:`DeserializeGuard` so the audit
    consumer keys off a single stable type string regardless of which
    guard fired.
    """
    return Finding(
        type="malformed_payload",
        span=(-1, -1),
        snippet="",
        severity="low",
        message=message,
    )
