"""cc_cortex.handoff — Handoff subsystem facade."""

from cc_cortex.handoff_validator import HandoffGuard, ValidationResult
from cc_cortex.structured_handoff import (
    HandoffRecord,
    HandoffTemplate,
    StructuredHandoffGuard,
)

__all__ = [
    "HandoffGuard",
    "HandoffRecord",
    "HandoffTemplate",
    "StructuredHandoffGuard",
    "ValidationResult",
]
