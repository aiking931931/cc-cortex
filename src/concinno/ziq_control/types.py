"""TCT type definitions — Python mirror of tct-core/types.ts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

# ── Input ──

@dataclass(frozen=True)
class TctSignal:
    """Measurement for TCT control."""
    current: float
    history: list[float]
    timestamp: float = 0.0


# ── Output ──

TctDecision = Literal["increase", "decrease", "freeze"]


@dataclass(frozen=True)
class TctResult:
    """TCT control output."""
    decision: TctDecision
    riverbed: float
    tension: float
    magnitude: float
    frozen: bool
    reason: str


# ── Configuration ──

@dataclass
class TctConfig:
    """TCT controller configuration."""
    # Riverbed (R)
    riverbed_window: int = 20
    riverbed_decay: float = 0.95
    # Tension (T)
    tension_threshold: float = 0.3
    # Dynamic Equilibrium (D)
    freeze_oscillation_count: int = 3
    freeze_duration: int = 5
    floor: float = 0.0
    ceiling: float = 1.0


# ── Controller State ──

@dataclass
class TctState:
    """Stateful controller memory."""
    riverbed: float = 0.0
    freeze_remaining: int = 0
    recent_signs: list[int] = field(default_factory=list)
    last_tension: float = 0.0
    step: int = 0


# ── Adapter Interface ──

class TctAdapter(Protocol):
    """Adapter maps domain state to/from TCT signals."""

    def to_signal(self, state: object) -> TctSignal: ...
    def apply_result(self, state: object, result: TctResult) -> object: ...
    def get_config(self) -> TctConfig: ...
