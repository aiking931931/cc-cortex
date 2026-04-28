"""concinno.ziq_emit_helpers — Reusable ZIQ outcome emit helpers.

@module ziq_emit_helpers
@responsibility Centralize the common reward-shaping logic used when guards /
    autotuned modules emit ``Outcome`` events. Each helper takes
    ``(value, observed, tripped, source, **metadata)`` and emits a normalized
    reward into the ZIQ outcome bus.

@dependencies concinno.ziq_outcome_bus
@exports emit_threshold_outcome, emit_budget_outcome, emit_iteration_outcome,
    emit_classification_outcome, emit_boolean_outcome, emit_continuous_outcome

Reward conventions (used by all helpers — derived from escalation pilot):

    tripped=True (gate fired):
        reward in [0.0, 0.5] — lower threshold = more aggressive trip
        = lower reward. Specific scaling per helper.

    tripped=False, observed > 0:
        reward in [0.5, 1.0] — proportional to remaining headroom
        ``1 - (observed / threshold) * 0.5``.

    tripped=False, observed = 0:
        reward = 1.0 (no signal needed but emitted for symmetry).

All emits are best-effort — never raise. The bus itself swallows
subscriber exceptions, but the helper additionally swallows bus errors
(``ImportError``, ``AttributeError``) so that wiring this layer cannot
break a guard call site even if ZIQ is partially broken.

Centralizing the math here means we wire each consumer with one line,
not 25 lines of try/except + Outcome construction. That keeps the
diff manageable across 12+ files and one place to fix reward bugs.
"""

from __future__ import annotations

from typing import Any


__all__ = [
    "emit_boolean_outcome",
    "emit_budget_outcome",
    "emit_classification_outcome",
    "emit_continuous_outcome",
    "emit_iteration_outcome",
    "emit_threshold_outcome",
]


def _safe_emit(
    tunable: str,
    value: Any,
    reward: float,
    source: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Best-effort emit — swallow every exception."""
    try:
        from concinno.ziq_outcome_bus import Outcome, get_bus

        get_bus().emit(
            Outcome(
                tunable=tunable,
                value=value,
                reward=float(reward),
                source=source,
                metadata=metadata or {},
            )
        )
    except Exception:
        pass


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def emit_threshold_outcome(
    tunable: str,
    *,
    value: int | float,
    observed: int | float,
    tripped: bool,
    source: str,
    trip_scale: float = 10.0,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Emit reward for a threshold-style tunable (gate trips at value).

    Args:
        tunable: ZIQ tunable id (e.g. ``"sentinel_gate.max_repeats"``).
        value: Current threshold value (the parameter under tune).
        observed: How many failures / repeats / iterations were seen.
        tripped: Whether the gate actually fired.
        source: Producer id for audit (e.g. ``"concinno.sentinel.gate_X"``).
        trip_scale: Divisor for tripped reward — higher = more lenient
            on aggressive thresholds.
        metadata: Extra context for FTRL learner.
    """
    if tripped:
        reward = _clamp(float(value) / float(trip_scale), 0.0, 0.5)
    else:
        used = float(observed) / max(1.0, float(value))
        reward = _clamp(1.0 - used * 0.5, 0.5, 1.0)
    _safe_emit(tunable, value, reward, source, metadata)


def emit_iteration_outcome(
    tunable: str,
    *,
    value: int,
    iterations_used: int,
    succeeded: bool,
    source: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Emit reward for an iteration-cap tunable (e.g. max_iterations).

    Succeeded under cap = reward grows with unused budget.
    Cap exhausted = reward 0.0 (signal to raise the cap).
    """
    if not succeeded:
        reward = 0.0
    else:
        used_ratio = float(iterations_used) / max(1.0, float(value))
        reward = _clamp(1.0 - used_ratio * 0.5, 0.5, 1.0)
    md = {"iterations_used": iterations_used, "succeeded": succeeded}
    if metadata:
        md.update(metadata)
    _safe_emit(tunable, value, reward, source, md)


def emit_budget_outcome(
    tunable: str,
    *,
    value: int | float,
    actual: int | float,
    overflowed: bool,
    source: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Emit reward for a budget-style tunable (token / memory / count).

    overflowed=True → reward 0.0 (budget too tight).
    overflowed=False → reward grows with unused fraction.
    """
    if overflowed:
        reward = 0.0
    else:
        ratio = float(actual) / max(1.0, float(value))
        reward = _clamp(1.0 - ratio * 0.4, 0.6, 1.0)
    md = {"actual": actual, "overflowed": overflowed}
    if metadata:
        md.update(metadata)
    _safe_emit(tunable, value, reward, source, md)


def emit_classification_outcome(
    tunable: str,
    *,
    value: str,
    correct: bool,
    confidence: float = 1.0,
    source: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Emit reward for a categorical / arm-selection tunable.

    Reward = correct ? confidence : (1.0 - confidence) for partial credit.
    """
    reward = _clamp(confidence if correct else (1.0 - confidence), 0.0, 1.0)
    md = {"correct": correct, "confidence": confidence}
    if metadata:
        md.update(metadata)
    _safe_emit(tunable, value, reward, source, md)


def emit_boolean_outcome(
    tunable: str,
    *,
    value: bool,
    success: bool,
    source: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Emit reward for a boolean tunable (feature on/off)."""
    reward = 1.0 if success else 0.0
    _safe_emit(tunable, value, reward, source, metadata)


def emit_continuous_outcome(
    tunable: str,
    *,
    value: float,
    reward: float,
    source: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Emit raw reward for a continuous tunable. Reward must be in [0, 1]."""
    _safe_emit(tunable, value, _clamp(reward, 0.0, 1.0), source, metadata)
