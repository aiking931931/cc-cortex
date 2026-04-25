"""Pinned memory store — generic anti-drift primitive.

A pinned memory is a fact the user or agent has explicitly marked
as important. Pinned facts:

* are excluded from consolidation summarisation,
* are returned with priority by ``Persona.recall``,
* survive every ``decay`` cycle untouched.

This is a deliberately simple rule-based mechanism. There is no
automatic peak detection, no algorithmic ranking heuristic, no
"importance score" computed from prior turns. The pin set is
exactly what the user / agent put in it.

Why this exists: when an LLM consolidates a long conversation
into a summary, salient facts (user's name, preferences, stated
constraints) routinely get dropped or paraphrased into uselessness.
Explicit pins give the consumer a single API hook to keep critical
identity facts fixed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from concinno.persona.schema import PinnedMemory


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PinnedMemoryStore:
    """Mutable list of pinned memories with priority recall.

    Maintains insertion order. Duplicates (by ``content`` exact
    match, case-sensitive) are ignored on add — pinning the same
    fact twice is a no-op, not an error.
    """

    def __init__(self, initial: Iterable[PinnedMemory] | None = None) -> None:
        self._items: list[PinnedMemory] = []
        if initial:
            for m in initial:
                self._items.append(m)

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self):
        return iter(self._items)

    def all(self) -> list[PinnedMemory]:
        return list(self._items)

    def contains(self, content: str) -> bool:
        return any(m.content == content for m in self._items)

    def pin(self, content: str, *, reason: str | None = None) -> PinnedMemory:
        """Pin a memory. Idempotent: re-pinning returns the existing entry."""
        for m in self._items:
            if m.content == content:
                return m
        entry = PinnedMemory(
            content=content,
            pinned_at=_now_iso(),
            reason=reason,
        )
        self._items.append(entry)
        return entry

    def unpin(self, content: str) -> bool:
        """Remove a pinned memory by exact content match. Returns True if removed."""
        for i, m in enumerate(self._items):
            if m.content == content:
                self._items.pop(i)
                return True
        return False

    def search(self, query: str, top_k: int = 5) -> list[PinnedMemory]:
        """Naive substring/keyword search over pinned content.

        Pinned recall is intentionally simple — there is no embedding
        lookup here. The expensive vector search lives in
        :class:`PersonaRAG`. Pinned memory retrieval is the cheap,
        always-correct path for "facts that should never be forgotten".
        """
        if not query:
            return self.all()[:top_k]
        q = query.lower()
        scored: list[tuple[int, PinnedMemory]] = []
        for m in self._items:
            content_lower = m.content.lower()
            score = 0
            if q in content_lower:
                score += 10
            for token in q.split():
                if token and token in content_lower:
                    score += 1
            if score > 0:
                scored.append((score, m))
        scored.sort(key=lambda t: -t[0])
        out = [m for _, m in scored[:top_k]]
        # If nothing matched, fall back to most recent pins so the
        # caller still gets identity anchors in the prompt.
        if not out:
            out = list(reversed(self._items))[:top_k]
        return out


__all__ = ["PinnedMemoryStore"]
