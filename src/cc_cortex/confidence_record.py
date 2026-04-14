"""cc_cortex.confidence_record — Re-export shim (merged into cc_cortex.confidence).

All functionality moved to cc_cortex.confidence. This shim preserves
backward compatibility for existing imports.
"""

from cc_cortex.confidence import (  # noqa: F401
    confidence_context,
    read_confidence,
    update_confidence,
)
