"""Lightweight retrieval over a persona's chat history.

Track 1 ships with a self-contained BM25-style retriever so the
persona module has zero new optional deps. Consumers who want a
heavier vector / dense backend can subclass :class:`PersonaRAG`
and override :meth:`add` / :meth:`search`.

Why no Concinno ZIQRetrieval / STAREngine wiring here:

* ``ZIQRetrieval`` and ``STAREngine`` are coupled to project-level
  cache directories and namespaces designed for code knowledge
  bases, not per-persona conversational history.
* Pulling them in would force every Persona consumer to set up a
  cache dir + namespaces, breaking the "load a persona file and
  start chatting" UX.
* A 200-line BM25-ish scorer keeps the dependency graph flat and
  the code reviewable.

Future Track 2 / Track 3 backends can swap in heavier retrieval
without changing the public API.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[一-鿿]")


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


@dataclass
class _IndexedTurn:
    """One indexed entry in the persona RAG."""

    idx: int
    text: str
    tokens: list[str] = field(default_factory=list)
    tf: Counter = field(default_factory=Counter)


@dataclass
class RAGHit:
    """A retrieval result."""

    score: float
    text: str
    idx: int


class PersonaRAG:
    """BM25-ish retriever over a persona's chat turns.

    Uses a simplified BM25 (k1=1.5, b=0.75) computed lazily so adds
    stay O(1) at index time. ``search`` re-derives doc-frequency
    stats on demand — fast enough for the typical persona conversation
    (≤ a few thousand turns).
    """

    def __init__(self, persona_name: str = "default") -> None:
        self.persona_name = persona_name
        self._docs: list[_IndexedTurn] = []

    def __len__(self) -> int:
        return len(self._docs)

    def reset(self) -> None:
        self._docs.clear()

    def add(self, text: str) -> None:
        """Add a chat turn or memory string to the index."""
        if not text or not text.strip():
            return
        tokens = _tokenize(text)
        if not tokens:
            return
        self._docs.append(
            _IndexedTurn(
                idx=len(self._docs),
                text=text,
                tokens=tokens,
                tf=Counter(tokens),
            )
        )

    def add_turn(self, user: str, reply: str) -> None:
        """Convenience: index user query + assistant reply as a paired entry."""
        joined = (user or "").strip()
        if reply:
            joined = (joined + "\n" + reply).strip()
        if joined:
            self.add(joined)

    def search(self, query: str, top_k: int = 5) -> list[RAGHit]:
        """Return top-k matches by BM25-ish score. Empty list if no docs / no query."""
        if not self._docs or not query:
            return []
        q_tokens = _tokenize(query)
        if not q_tokens:
            return []

        # Document frequency for the query terms.
        n = len(self._docs)
        df: Counter = Counter()
        for term in set(q_tokens):
            for d in self._docs:
                if term in d.tf:
                    df[term] += 1

        avgdl = sum(len(d.tokens) for d in self._docs) / max(1, n)
        k1, b = 1.5, 0.75

        scored: list[RAGHit] = []
        for d in self._docs:
            score = 0.0
            dl = len(d.tokens)
            for term in q_tokens:
                f = d.tf.get(term, 0)
                if f == 0:
                    continue
                idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
                num = f * (k1 + 1)
                den = f + k1 * (1 - b + b * dl / max(1, avgdl))
                score += idf * num / max(1e-9, den)
            if score > 0:
                scored.append(RAGHit(score=score, text=d.text, idx=d.idx))
        scored.sort(key=lambda h: -h.score)
        return scored[:top_k]


__all__ = ["PersonaRAG", "RAGHit"]
