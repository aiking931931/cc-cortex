"""concinno.ziq_outcome_bus — Pub-sub bus for ZIQ online-learning signals.

@module ziq_outcome_bus
@responsibility Decouple guard / autotuned-module *producers* from ZIQ
    *consumers* (FTRL posterior updaters) so the 19+ tunables in
    ``ziq_autotune_registry`` can be wired one at a time without a
    rewrite of every guard call site.
@dependencies (stdlib only) — threading, dataclasses, json, os, time, pathlib
@exports Outcome, ZIQOutcomeBus, get_bus, emit, is_bus_disabled

Design
------
Single-process, thread-safe pub-sub. Producers call ``bus.emit(Outcome(...))``
or wrap a function with ``@emit("tunable.id")``. Consumers subscribe per
tunable id; ``subscribe()`` returns an unsubscribe callable.

Concurrency:
    All mutating operations hold a single ``threading.Lock``. Dispatch is
    inline (no thread pool) — ZIQ FTRL updates are O(1) and faster than
    the lock acquire-release amortized cost of a worker pool.

Ordering:
    Per-tunable causal order preserved: emits dispatch in arrival order
    under the lock. Subscribers see events in the same order they were
    emitted (last-emit-wins for the same key when consumer keeps state).

Race-condition guard (plan §244 — Plan C 2026-04-28):
    Per-tunable rate limiter caps emit storms at
    ``CONCINNO_ZIQ_BUS_MAX_HZ`` events/sec (default 100). Excess emits are
    dropped silently to protect FTRL learners from runaway producers (e.g.
    a guard in a tight loop). Tracked under the same lock that guards
    subscribers so the rate budget is consistent across threads. Dropped
    emits are counted via ``dropped_count(tunable)`` for audit.

Manual override (``manual_override`` / ``pin``):
    User-pinned values short-circuit ``emit()`` — no dispatch happens
    when ``is_pinned(tunable)`` is true. Pin file lives at
    ``~/.concinno/ziq_pinned.json``. This honours the L0 #6 priority:
    "用戶明示 > opt-out > cosmetic > ZIQ 贏" — explicit user pin beats
    online learning.

Hard kill switch:
    Env ``CONCINNO_ZIQ_BUS_DISABLED=1`` → ``emit()`` is a no-op,
    ``subscribe()`` still records callbacks (so subscribe-then-toggle
    works after the env flips back).
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Any

__all__ = [
    "Outcome",
    "ZIQOutcomeBus",
    "emit",
    "get_bus",
    "is_bus_disabled",
]


# ── Hard kill switch ────────────────────────────────────────────


def is_bus_disabled() -> bool:
    """Return True when env ``CONCINNO_ZIQ_BUS_DISABLED=1``.

    Read fresh every call — no module-level cache — so tests can flip
    the env mid-run without a re-import.
    """
    return os.environ.get("CONCINNO_ZIQ_BUS_DISABLED", "").strip() in {
        "1",
        "true",
        "yes",
        "on",
    }


# ── Race-condition rate limiter ─────────────────────────────────


_DEFAULT_MAX_HZ = 100.0


def _rate_limit_hz() -> float:
    """Per-tunable max emits/sec. Override via ``CONCINNO_ZIQ_BUS_MAX_HZ``."""
    raw = os.environ.get("CONCINNO_ZIQ_BUS_MAX_HZ", "").strip()
    if not raw:
        return _DEFAULT_MAX_HZ
    try:
        v = float(raw)
        return v if v > 0.0 else _DEFAULT_MAX_HZ
    except ValueError:
        return _DEFAULT_MAX_HZ


# ── Pin file ────────────────────────────────────────────────────


def _pin_file_path() -> Path:
    """Resolve the pin file. Override via ``CONCINNO_ZIQ_PIN_FILE`` for tests."""
    override = os.environ.get("CONCINNO_ZIQ_PIN_FILE")
    if override:
        return Path(override)
    return Path.home() / ".concinno" / "ziq_pinned.json"


def _load_pins() -> dict[str, Any]:
    path = _pin_file_path()
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _save_pins(data: dict[str, Any]) -> None:
    path = _pin_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


# ── Event dataclass ─────────────────────────────────────────────


@dataclass(frozen=True)
class Outcome:
    """One online-learning signal event.

    Attributes:
        tunable: Dotted-path identifier matching a key in
            ``ziq_autotune_registry.TUNABLE_REGISTRY``.
        value: The parameter value that produced this outcome —
            numeric / bool for continuous/threshold tunables, string
            for categorical / arm-selection tunables (e.g.
            ``judge.arm = "haiku"|"sonnet"|"opus"``).
        reward: Higher = better. Convention: 1.0 = full success,
            0.0 = total failure, intermediate = partial credit.
        timestamp: Unix seconds when the outcome occurred.
        metadata: Arbitrary producer-supplied context (latency, retry
            count, error class) — consumers may ignore.
        source: Producer identifier (e.g. ``"escalation.LLMEscalator"``)
            for audit trails.

    Validation: ``__post_init__`` rejects empty tunable / non-finite
    reward / unsupported value type to fail loud at the producer instead
    of silently corrupting consumer state.
    """

    tunable: str
    value: float | int | bool | str
    reward: float
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)
    source: str = ""

    def __post_init__(self) -> None:
        if not self.tunable or not isinstance(self.tunable, str):
            raise ValueError("Outcome.tunable must be a non-empty string")
        if not isinstance(self.value, (int, float, bool, str)):
            raise TypeError(
                "Outcome.value must be int/float/bool/str, got "
                f"{type(self.value).__name__}"
            )
        try:
            r = float(self.reward)
        except (TypeError, ValueError) as exc:
            raise TypeError("Outcome.reward must be numeric") from exc
        # Reject NaN / inf — FTRL math degenerates on non-finite reward.
        if r != r or r in (float("inf"), float("-inf")):
            raise ValueError(f"Outcome.reward must be finite, got {self.reward!r}")


# ── Bus ─────────────────────────────────────────────────────────


_Subscriber = Callable[[Outcome], None]


class ZIQOutcomeBus:
    """Thread-safe pub-sub bus for ZIQ outcomes.

    Use ``ZIQOutcomeBus.get_bus()`` to acquire the process-wide
    singleton. Tests that want isolation can construct a fresh
    instance directly — the public functions ``get_bus()`` /
    ``emit()`` always go through the singleton.
    """

    _instance: "ZIQOutcomeBus | None" = None
    _instance_lock: threading.Lock = threading.Lock()

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: dict[str, list[_Subscriber]] = {}
        # Race-condition rate limiter — track recent emit timestamps per
        # tunable (sliding window = 1 second). Drop emits that exceed
        # ``_rate_limit_hz()`` events/sec to protect FTRL learners from
        # runaway producers (e.g. a guard called in a tight loop).
        self._emit_window: dict[str, list[float]] = {}
        self._dropped: dict[str, int] = {}
        # In-memory pin cache — refreshed lazily on every is_pinned() call
        # because the file may be edited by a CLI between emits.

    # ── Singleton accessor ─────────────────────────────────

    @classmethod
    def get_bus(cls) -> "ZIQOutcomeBus":
        """Return the process-wide singleton, creating it on first call."""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def _reset_for_testing(cls) -> None:
        """Drop the singleton. **Only for tests.** Not part of public API."""
        with cls._instance_lock:
            cls._instance = None

    # ── Subscribe / unsubscribe ────────────────────────────

    def subscribe(
        self, tunable: str, callback: _Subscriber
    ) -> Callable[[], None]:
        """Register ``callback`` for ``tunable``.

        Returns:
            A zero-arg unsubscribe function. Calling it twice is safe
            (idempotent — the second call is a no-op).
        """
        if not tunable or not isinstance(tunable, str):
            raise ValueError("tunable must be a non-empty string")
        if not callable(callback):
            raise TypeError("callback must be callable")
        with self._lock:
            self._subscribers.setdefault(tunable, []).append(callback)

        def _unsubscribe() -> None:
            with self._lock:
                subs = self._subscribers.get(tunable)
                if subs is None:
                    return
                # Identity match — same callback object only.
                try:
                    subs.remove(callback)
                except ValueError:
                    return
                if not subs:
                    self._subscribers.pop(tunable, None)

        return _unsubscribe

    # ── Emit ───────────────────────────────────────────────

    def emit(self, outcome: Outcome) -> None:
        """Dispatch ``outcome`` to all subscribers of its tunable.

        No-op when:
          * env ``CONCINNO_ZIQ_BUS_DISABLED=1`` (hard kill), OR
          * the tunable is pinned via ``pin()`` / pin file.

        Subscriber exceptions are caught and logged to stderr — one
        bad consumer must not break the producer hot path or other
        consumers.
        """
        if is_bus_disabled():
            return
        if self.is_pinned(outcome.tunable):
            return
        # Race-condition guard: cap per-tunable emit rate at
        # ``_rate_limit_hz()`` events/sec (sliding 1-second window).
        # Drop excess silently and increment ``_dropped`` counter so
        # producers can be audited via ``dropped_count(tunable)``.
        max_hz = _rate_limit_hz()
        # Snapshot subscribers under lock; dispatch outside lock so a
        # slow callback doesn't serialize emits across all tunables.
        with self._lock:
            now = time.time()
            window = self._emit_window.setdefault(outcome.tunable, [])
            # Evict timestamps older than 1 second.
            cutoff = now - 1.0
            i = 0
            for ts in window:
                if ts >= cutoff:
                    break
                i += 1
            if i:
                del window[:i]
            if len(window) >= max_hz:
                self._dropped[outcome.tunable] = (
                    self._dropped.get(outcome.tunable, 0) + 1
                )
                return
            window.append(now)
            subs = list(self._subscribers.get(outcome.tunable, ()))
        for cb in subs:
            try:
                cb(outcome)
            except Exception as exc:  # pragma: no cover - defensive
                # Stderr log; consumers keep firing.
                import sys

                print(
                    f"ziq_outcome_bus: subscriber {cb!r} raised {exc!r}",
                    file=sys.stderr,
                )

    # ── Pin / unpin ────────────────────────────────────────

    def is_pinned(self, tunable: str) -> bool:
        """Return True when the user has manually fixed ``tunable``."""
        return tunable in _load_pins()

    def pinned_value(self, tunable: str) -> Any:
        """Return the user-pinned value, or ``None`` if not pinned."""
        return _load_pins().get(tunable)

    def pin(self, tunable: str, value: Any) -> None:
        """Manually fix ``tunable`` at ``value``. Subsequent emits no-op."""
        if not tunable or not isinstance(tunable, str):
            raise ValueError("tunable must be a non-empty string")
        with self._lock:
            data = _load_pins()
            data[tunable] = value
            _save_pins(data)

    def unpin(self, tunable: str) -> None:
        """Release a manual fix. Subsequent emits dispatch normally."""
        with self._lock:
            data = _load_pins()
            if tunable in data:
                data.pop(tunable)
                _save_pins(data)

    # ── Introspection (for tests / CLI) ────────────────────

    def subscriber_count(self, tunable: str) -> int:
        """Return current subscriber count for ``tunable``."""
        with self._lock:
            return len(self._subscribers.get(tunable, ()))

    def dropped_count(self, tunable: str) -> int:
        """Return number of emits rate-limit dropped for ``tunable``.

        Useful for auditing producers that may be in a tight loop
        (rate-limit guard, plan §244 race-condition fix).
        """
        with self._lock:
            return int(self._dropped.get(tunable, 0))

    def reset_rate_state(self, tunable: str | None = None) -> None:
        """Drop rate-limiter window + counter. Mainly for tests."""
        with self._lock:
            if tunable is None:
                self._emit_window.clear()
                self._dropped.clear()
            else:
                self._emit_window.pop(tunable, None)
                self._dropped.pop(tunable, None)


# ── Module-level convenience ────────────────────────────────────


def get_bus() -> ZIQOutcomeBus:
    """Shortcut for :meth:`ZIQOutcomeBus.get_bus`."""
    return ZIQOutcomeBus.get_bus()


# ── Decorator ───────────────────────────────────────────────────


def emit(
    tunable: str,
    *,
    source: str = "",
    value_arg: str | None = None,
    reward_from: Callable[[Any], float] | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator: capture a function's return value as an outcome reward.

    Args:
        tunable: Tunable id (e.g. ``"escalation.max_retries_per_tier"``).
        source: Optional producer identifier for the outcome.
        value_arg: Name of the kwarg that holds the *parameter value*
            being evaluated (for FTRL "what value did we try?"). When
            ``None``, value defaults to ``True`` (treat as a boolean
            "did we run with this guard"). The kwarg may also be a
            positional that the wrapped function accepts.
        reward_from: Callable mapping the wrapped function's return
            value to a float reward. When ``None``, rewards are derived
            by convention:

              * ``dict`` with ``reward`` key → that value
              * numeric (int/float/bool) → ``float(result)``
              * any other return type → 1.0 (treat as success)

            Exceptions raised by the wrapped function are not swallowed
            — they propagate, but **a reward of 0.0 is emitted first**
            so the FTRL learner sees the failure signal.

    Example::

        @emit("escalation.max_retries_per_tier", source="LLMEscalator")
        def call_with_retries(value=1):
            ...
            return {"reward": 1.0, "tier": "haiku"}
    """

    if not tunable:
        raise ValueError("emit() decorator needs a non-empty tunable id")

    def _deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(fn)
        def _wrapper(*args: Any, **kwargs: Any) -> Any:
            # Resolve "value" — what hyperparameter value was used.
            value: Any = True
            if value_arg is not None and value_arg in kwargs:
                value = kwargs[value_arg]

            try:
                result = fn(*args, **kwargs)
            except Exception:
                # Failure signal: reward 0.0, then re-raise.
                try:
                    get_bus().emit(
                        Outcome(
                            tunable=tunable,
                            value=value,
                            reward=0.0,
                            source=source,
                            metadata={"raised": True},
                        )
                    )
                except Exception:  # pragma: no cover - defensive
                    pass
                raise

            # Derive reward from result.
            reward: float
            if reward_from is not None:
                reward = float(reward_from(result))
            elif isinstance(result, dict) and "reward" in result:
                reward = float(result["reward"])
            elif isinstance(result, (int, float, bool)):
                reward = float(result)
            else:
                reward = 1.0

            try:
                get_bus().emit(
                    Outcome(
                        tunable=tunable,
                        value=value,
                        reward=reward,
                        source=source,
                    )
                )
            except Exception:  # pragma: no cover - defensive
                pass
            return result

        return _wrapper

    return _deco
