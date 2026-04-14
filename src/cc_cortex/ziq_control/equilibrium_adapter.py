"""TCT adapter for CCC EquilibriumBreaker.

Wraps the existing breaker with TCT three-state control:
- Normal: original deny/allow pressure logic
- Freeze: when oscillation detected, suspend ALL threshold changes
  (breaker won't re-trip immediately after cooldown ends)

Feature flag: TCT_EQUILIBRIUM_ENABLED (default False).
When disabled, falls through to original logic unchanged.
"""

from __future__ import annotations

import os

from cc_cortex.ziq_control.controller import tct_control
from cc_cortex.ziq_control.types import TctConfig, TctSignal


def is_tct_enabled() -> bool:
    """Check if TCT control is enabled for equilibrium breaker."""
    return os.environ.get("TCT_EQUILIBRIUM_ENABLED", "").lower() in (
        "1", "true", "yes",
    )


# TCT config tuned for deny pressure domain:
# - floor=0 (no pressure is fine)
# - ceiling=PRESSURE_THRESHOLD (breaker trip point)
# - oscillation count=3 (3 sign changes = deny storm oscillation)
# - freeze duration=5 (stay frozen for 5 tool calls)
_EQUILIBRIUM_TCT_CONFIG = TctConfig(
    riverbed_window=20,
    riverbed_decay=0.9,
    tension_threshold=0.4,
    freeze_oscillation_count=3,
    freeze_duration=5,
    floor=0.0,
    ceiling=5.0,  # matches PRESSURE_THRESHOLD
)


def should_freeze_pressure(pressure_history: list[float]) -> bool:
    """Check if pressure changes should be frozen (oscillation detected).

    Called by EquilibriumBreaker.record_deny() and record_allow()
    before modifying pressure. If True, pressure stays unchanged.

    Args:
        pressure_history: recent pressure values (oldest first)

    Returns:
        True if TCT detects oscillation and recommends freeze
    """
    if not is_tct_enabled():
        return False

    if len(pressure_history) < 5:
        return False

    signal = TctSignal(
        current=pressure_history[-1],
        history=pressure_history,
    )
    result = tct_control(signal, _EQUILIBRIUM_TCT_CONFIG)
    return result.frozen
