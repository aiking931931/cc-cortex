"""cc_cortex.token — Token management subsystem facade.

Re-exports from top-level modules for ``from cc_cortex.token import …`` usage.
"""

from cc_cortex.token_monitor import (
    TokenGuard,
    check_budget_gate,
    check_threshold,
    read_real_token_usage,
)
from cc_cortex.token_zone import (
    Zone,
    detect_model,
    detect_zone,
    detect_zone_abs,
    format_ux,
    increment_compact_count,
    read_zone_file,
    should_gate_tool,
    write_zone_file,
    zone_injection,
)

__all__ = [
    "TokenGuard",
    "Zone",
    "check_budget_gate",
    "check_threshold",
    "detect_model",
    "detect_zone",
    "detect_zone_abs",
    "format_ux",
    "increment_compact_count",
    "read_real_token_usage",
    "read_zone_file",
    "should_gate_tool",
    "write_zone_file",
    "zone_injection",
]
