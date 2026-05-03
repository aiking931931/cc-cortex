# SPDX-License-Identifier: AGPL-3.0-or-later
"""concinno.fieldread — 5 fixed semantic namespaces + breadcrumb router.

Cigito v3 patent moat axis 3, governance side. Standalone — no
``aiking_core`` runtime dependency (Concinno is upstream of aiking_core).

Public surface:

    NAMESPACES, COGNITION, SKILLS, FEEDBACK, HANDOFF, AUDIT
    Namespace, is_namespace, route
    Breadcrumb, breadcrumb_from_path
    FieldReadCompressor, CompressedContent
    L1_BUDGET_CHARS, L2_BUDGET_CHARS, Tier

Quick start::

    from concinno.fieldread import FieldReadCompressor, HANDOFF

    compressor = FieldReadCompressor()
    result = compressor.compress(
        content=long_handoff_markdown,
        namespace=HANDOFF,
        tier="l1",       # ≤200 chars index
    )
    print(result.content, result.breadcrumb.render())
"""

from __future__ import annotations

from concinno.fieldread.breadcrumb import (
    Breadcrumb,
    breadcrumb_from_path,
)
from concinno.fieldread.compressor import (
    L1_BUDGET_CHARS,
    L2_BUDGET_CHARS,
    CompressedContent,
    FieldReadCompressor,
    Tier,
)
from concinno.fieldread.namespaces import (
    AUDIT,
    COGNITION,
    FEEDBACK,
    HANDOFF,
    NAMESPACES,
    SKILLS,
    Namespace,
    is_namespace,
    route,
)

__all__ = [
    # Namespace constants
    "AUDIT",
    "COGNITION",
    "FEEDBACK",
    "HANDOFF",
    "NAMESPACES",
    "Namespace",
    "SKILLS",
    # Routing
    "is_namespace",
    "route",
    # Breadcrumbs
    "Breadcrumb",
    "breadcrumb_from_path",
    # Compressor
    "CompressedContent",
    "FieldReadCompressor",
    "L1_BUDGET_CHARS",
    "L2_BUDGET_CHARS",
    "Tier",
]
