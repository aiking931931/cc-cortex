"""cc_cortex.confidence_gate — Re-export shim (merged into cc_cortex.confidence).

All functionality moved to cc_cortex.confidence. This shim preserves
backward compatibility for existing imports.
"""

from cc_cortex.confidence import (  # noqa: F401
    ConfidenceGate,
    _build_deny_message,
    _build_uncertainty_patterns,
    _get_verify_context,
    detect_uncertainty,
    is_irreversible,
)
