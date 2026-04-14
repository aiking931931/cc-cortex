"""TCT — Tensor Control Theory Universal Control Layer (Python port).

R (Riverbed) = stability   → WHERE
T (Tension)  = trigger     → WHEN
D (Dynamic)  = three-state → HOW MUCH (increase / decrease / freeze)
"""

from cc_cortex.ziq_control.controller import (
    DEFAULT_CONFIG,
    compute_riverbed,
    compute_tension,
    create_tct_state,
    detect_oscillation,
    tct_control,
    tct_control_stateful,
)
from cc_cortex.ziq_control.types import TctConfig, TctDecision, TctResult, TctSignal, TctState

__all__ = [
    "TctSignal", "TctDecision", "TctResult", "TctConfig", "TctState",
    "DEFAULT_CONFIG", "create_tct_state", "tct_control", "tct_control_stateful",
    "compute_riverbed", "compute_tension", "detect_oscillation",
]
