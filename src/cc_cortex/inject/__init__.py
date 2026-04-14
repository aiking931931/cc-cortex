"""cc_cortex.inject — Cognitive injection subsystem facade.

Re-exports from top-level modules for ``from cc_cortex.inject import …`` usage.
Original files stay at cc_cortex.* (backward compatible).
"""

from cc_cortex.cognitive_anchor import (
    CognitiveAnchorGuard,
    classify_risk,
    get_anchor_prompt,
    get_base_identity,
)
from cc_cortex.cognitive_inject import (
    build_cognitive_context,
    build_delivery_standards,
    build_rag_context,
    build_thinking_directives,
)
from cc_cortex.think_inject import ThinkInjectGuard

__all__ = [
    "CognitiveAnchorGuard",
    "ThinkInjectGuard",
    "build_cognitive_context",
    "build_delivery_standards",
    "build_rag_context",
    "build_thinking_directives",
    "classify_risk",
    "get_anchor_prompt",
    "get_base_identity",
]
