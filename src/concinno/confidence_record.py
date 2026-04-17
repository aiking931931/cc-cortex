"""concinno.confidence_record — Re-export shim (merged into concinno.confidence).

All functionality moved to concinno.confidence. This shim preserves
backward compatibility for existing imports.
"""

from concinno.confidence import (  # noqa: F401
    confidence_context,
    read_confidence,
    update_confidence,
)
