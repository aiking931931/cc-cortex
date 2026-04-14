"""TCT Controller — Python mirror of tct-core/controller.ts.

R(t) = EMA(history)           — riverbed: what's "normal"
T(t) = (current - R) / |R|    — tension: how far from normal
D    = three-state decision    — increase / decrease / freeze

Freeze principle: during oscillation or chaos, NOT adapting IS
the highest form of adaptation.
"""

from __future__ import annotations

from dataclasses import replace
from typing import NamedTuple

from cc_cortex.ziq_control.types import TctConfig, TctResult, TctSignal, TctState

DEFAULT_CONFIG = TctConfig()


class StatefulResult(NamedTuple):
    result: TctResult
    new_state: TctState


def create_tct_state() -> TctState:
    return TctState()


# ── Core: Stateless Pure Function ──


def tct_control(signal: TctSignal, config: TctConfig | None = None) -> TctResult:
    """Compute TCT control decision from a signal. Pure function."""
    cfg = config or DEFAULT_CONFIG
    current = signal.current
    history = signal.history

    # R: Compute Riverbed (EMA)
    riverbed = compute_riverbed(history, cfg.riverbed_window, cfg.riverbed_decay)

    # T: Compute Tension
    tension = compute_tension(current, riverbed)

    # Detect Oscillation
    oscillating = detect_oscillation(history, cfg.freeze_oscillation_count)

    # D: Three-State Decision
    if oscillating:
        return TctResult(
            decision="freeze",
            riverbed=riverbed,
            tension=tension,
            magnitude=0.0,
            frozen=True,
            reason=(
                f"oscillation detected ({cfg.freeze_oscillation_count}+ sign changes)"
                " — freezing"
            ),
        )

    if current < cfg.floor:
        mag = 1.0 if cfg.floor == 0 else min(1.0, (cfg.floor - current) / cfg.floor)
        return TctResult(
            decision="increase",
            riverbed=riverbed,
            tension=tension,
            magnitude=mag,
            frozen=False,
            reason=f"current {current:.3f} < floor {cfg.floor} — increase",
        )

    if current > cfg.ceiling:
        rng = 1.0 if cfg.ceiling == 0 else cfg.ceiling
        mag = min(1.0, (current - cfg.ceiling) / rng)
        return TctResult(
            decision="decrease",
            riverbed=riverbed,
            tension=tension,
            magnitude=mag,
            frozen=False,
            reason=f"current {current:.3f} > ceiling {cfg.ceiling} — decrease",
        )

    # Within bounds — fine-tune based on tension
    abs_tension = abs(tension)
    if abs_tension > cfg.tension_threshold:
        decision = "decrease" if tension > 0 else "increase"
        return TctResult(
            decision=decision,
            riverbed=riverbed,
            tension=tension,
            magnitude=min(1.0, abs_tension / (cfg.tension_threshold * 3)),
            frozen=False,
            reason=f"tension {tension:.3f} exceeds threshold ±{cfg.tension_threshold} — {decision}",
        )

    # Stable
    return TctResult(
        decision="decrease",
        riverbed=riverbed,
        tension=tension,
        magnitude=0.0,
        frozen=False,
        reason="stable — no adjustment needed",
    )


# ── Stateful Controller ──


def tct_control_stateful(
    signal: TctSignal,
    state: TctState,
    config: TctConfig | None = None,
) -> StatefulResult:
    """Stateful TCT control — maintains freeze state across calls."""
    cfg = config or DEFAULT_CONFIG
    step = state.step + 1

    # If currently frozen, count down
    if state.freeze_remaining > 0:
        remaining = state.freeze_remaining - 1
        riverbed = compute_riverbed(signal.history, cfg.riverbed_window, cfg.riverbed_decay)
        tension = compute_tension(signal.current, riverbed)
        result = TctResult(
            decision="freeze",
            riverbed=riverbed,
            tension=tension,
            magnitude=0.0,
            frozen=True,
            reason=f"frozen — {remaining} steps remaining",
        )
        new_state = replace(state, freeze_remaining=remaining, step=step)
        return StatefulResult(result, new_state)

    # Run stateless control
    result = tct_control(signal, cfg)

    # Track tension sign
    sign = 1 if result.tension > 0 else (-1 if result.tension < 0 else 0)
    max_signs = cfg.freeze_oscillation_count * 2
    recent = (state.recent_signs + [sign])[-max_signs:]

    freeze_remaining = cfg.freeze_duration if result.frozen else 0

    new_state = TctState(
        riverbed=result.riverbed,
        freeze_remaining=freeze_remaining,
        recent_signs=recent,
        last_tension=result.tension,
        step=step,
    )
    return StatefulResult(result, new_state)


# ── Internal Computations ──


def compute_riverbed(history: list[float], window: int, decay: float) -> float:
    """Compute riverbed (R) as Exponential Moving Average."""
    if not history:
        return 0.0
    slc = history[-window:]
    if len(slc) == 1:
        return slc[0]
    ema = slc[0]
    for v in slc[1:]:
        ema = decay * ema + (1 - decay) * v
    return ema


def compute_tension(current: float, riverbed: float) -> float:
    """Compute tension (T) as normalized deviation from riverbed."""
    if riverbed == 0:
        return 0.0 if current == 0 else (1.0 if current > 0 else -1.0)
    return (current - riverbed) / abs(riverbed)


def detect_oscillation(history: list[float], count: int) -> bool:
    """Detect oscillation — consecutive sign changes in deltas."""
    if len(history) < count + 2:
        return False
    recent = history[-(count + 2):]
    deltas = [recent[i] - recent[i - 1] for i in range(1, len(recent))]
    sign_changes = sum(
        1 for i in range(1, len(deltas)) if deltas[i] * deltas[i - 1] < 0
    )
    return sign_changes >= count
