"""concinno.inject — Cognitive injection subsystem facade.

Re-exports from top-level modules for ``from concinno.inject import …`` usage.
Original files stay at concinno.* (backward compatible).
"""

from concinno.cognitive_inject import (
    build_cognitive_context,
    build_delivery_standards,
    build_rag_context,
    build_thinking_directives,
)
from concinno.think_inject import ThinkInjectGuard

__all__ = [
    "ThinkInjectGuard",
    "build_cognitive_context",
    "build_delivery_standards",
    "build_rag_context",
    "build_thinking_directives",
]
