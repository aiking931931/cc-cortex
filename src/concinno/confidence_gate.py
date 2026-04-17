"""concinno.confidence_gate — Re-export shim (merged into concinno.confidence).

All functionality moved to concinno.confidence. This shim preserves
backward compatibility for existing imports.
"""

from concinno.confidence import (  # noqa: F401
    ConfidenceGate,
    _build_deny_message,
    _build_uncertainty_patterns,
    _get_verify_context,
    detect_uncertainty,
    is_irreversible,
)
