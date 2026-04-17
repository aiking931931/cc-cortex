"""concinno.handoff — Handoff subsystem facade."""

from concinno.handoff_validator import HandoffGuard, ValidationResult
from concinno.structured_handoff import (
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
