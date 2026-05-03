# SPDX-License-Identifier: AGPL-3.0-or-later
"""Breadcrumb dataclass — retrieval audit trail (Cigito v3 patent moat axis 3).

Every :class:`~concinno.fieldread.compressor.FieldReadCompressor.compress`
call emits a chain of :class:`Breadcrumb` records showing which sections
of the source document were kept vs elided. Downstream ZIQ FTRL outcome
learning rates these chains; weak chains get de-prioritised next time.

This module is **standalone** (no aiking_core / lyceum runtime
dependency) so Concinno main can ship the patent surface without an
upstream coupling.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

__all__ = [
    "Breadcrumb",
    "breadcrumb_from_path",
]


@dataclass(frozen=True)
class Breadcrumb:
    """A path through the namespace hierarchy during a FieldRead retrieval.

    Cigito v3 patent axis 3 hinges on **breadcrumb-tracked retrieval audit
    trail** — every compress() call emits a chain showing which namespaces
    were traversed and which sections were kept vs elided.

    Attributes:
        namespace: One of :data:`concinno.fieldread.namespaces.NAMESPACES`.
        depth: 0-indexed depth of this crumb in its parent chain
            (``0`` = root, increments per ancestor).
        ancestors: Tuple of ancestor section identifiers leading to this
            crumb. Empty tuple at depth 0.
        section: Optional section identifier at this crumb level. When
            ``None`` the crumb represents the *namespace root* with no
            specific section selected.
        parent: Optional upstream :class:`Breadcrumb`, present when the
            chain was built incrementally (``compose()`` helper). Frozen
            so chains are hashable.
    """

    namespace: str
    depth: int = 0
    ancestors: tuple[str, ...] = ()
    section: Optional[str] = None
    parent: Optional["Breadcrumb"] = None

    @property
    def chain(self) -> tuple[str, ...]:
        """Full ``(namespace, ancestor_1, …, section)`` tuple."""
        parts: list[str] = [self.namespace]
        parts.extend(self.ancestors)
        if self.section:
            parts.append(self.section)
        return tuple(parts)

    def render(self) -> str:
        """Render as a ``<crumb>`` xml-ish tag for prompt injection."""
        return f"<crumb>{' > '.join(self.chain)}</crumb>"

    def compose(self, section: str) -> "Breadcrumb":
        """Return a new :class:`Breadcrumb` one level deeper.

        Useful when iterating sections inside a namespace — the parent
        chain is preserved frozen and the new crumb carries the deeper
        section identifier.

        Args:
            section: Identifier of the next section in the chain.

        Returns:
            New :class:`Breadcrumb` with depth + 1 and ``parent`` set
            to ``self``.
        """
        next_ancestors = self.ancestors
        if self.section:
            next_ancestors = (*self.ancestors, self.section)
        return Breadcrumb(
            namespace=self.namespace,
            depth=self.depth + 1,
            ancestors=next_ancestors,
            section=section,
            parent=self,
        )


def breadcrumb_from_path(
    path: str | Path,
    namespace: str,
) -> Breadcrumb:
    """Build a :class:`Breadcrumb` from a filesystem path + namespace.

    Walks up the path components and uses each parent directory name as
    an ancestor in the chain (deepest first → reversed to root-first).
    The filename (without extension) becomes the ``section`` if present.

    Args:
        path: Filesystem path (string or :class:`pathlib.Path`).
        namespace: One of :data:`concinno.fieldread.namespaces.NAMESPACES`.

    Returns:
        :class:`Breadcrumb` with depth equal to ``len(ancestors)``.

    Examples:
        >>> b = breadcrumb_from_path(
        ...     "_AI_BRAIN/06_Handoffs/concinno/交接_concinno.md",
        ...     "handoff",
        ... )
        >>> b.namespace
        'handoff'
        >>> b.section
        '交接_concinno'
    """
    p = Path(path)
    parts = p.parts
    if not parts:
        return Breadcrumb(namespace=namespace)

    section: Optional[str] = None
    if p.name and p.name != ".":
        section = p.stem or p.name

    parent_dirs: list[str] = []
    for component in parts[:-1]:
        if component in ("", "/", "\\", "."):
            continue
        # Strip drive letter / root markers on Windows.
        if component.endswith(":") or component.endswith(":\\"):
            continue
        parent_dirs.append(component)

    return Breadcrumb(
        namespace=namespace,
        depth=len(parent_dirs),
        ancestors=tuple(parent_dirs),
        section=section,
    )
