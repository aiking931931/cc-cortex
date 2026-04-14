"""cc_cortex.fewshot — Generic solved-case store with Jaccard retrieval.

@module fewshot
@responsibility Store pre-solved cases and retrieve the top-K most similar
    ones for a new query, using dependency-free Jaccard overlap on
    content-word tokens. Intended as a few-shot exemplar bank for agents
    facing a new task that resembles prior solved work.
@dependencies stdlib only (json, re, dataclasses, pathlib, typing)
@exports FewshotCase, FewshotBank, load_bank, retrieve_fewshot,
    DEFAULT_STOP_WORDS

Design notes:
  - Pure stdlib. No numpy, no sklearn, no embedding model. Good enough
    for small-to-medium banks (dozens to a few hundred cases) where
    keyword overlap is a reasonable similarity proxy.
  - Jaccard score = |A ∩ B| / |A ∪ B|. Deterministic, symmetric, cheap.
  - Pluggable tokenizer so CJK / domain-specific callers can override
    the default ASCII word regex without forking the retriever.
  - Bank is instance-scoped. No process-wide cache — callers that need
    caching can hold their own ``FewshotBank`` instance.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

__all__ = [
    "DEFAULT_STOP_WORDS",
    "FewshotCase",
    "FewshotBank",
    "load_bank",
    "retrieve_fewshot",
]

DEFAULT_STOP_WORDS: frozenset[str] = frozenset(
    {
        "the", "a", "an", "of", "to", "in", "on", "at", "for", "with",
        "by", "from", "is", "are", "was", "were", "be", "been", "being",
        "this", "that", "these", "those", "and", "or", "but", "not", "if",
        "then", "else", "so", "as", "it", "its", "you", "your", "i", "we",
        "our", "us", "they", "them", "their", "he", "she", "his", "her",
        "him", "me", "my", "mine", "do", "does", "did", "doing", "done",
        "have", "has", "had", "having", "can", "could", "should", "would",
        "may", "might", "must", "shall", "will", "about", "into", "over",
        "under", "out", "up", "down", "off", "per", "via", "than", "also",
        "when", "which", "while", "where", "how", "what", "who", "whom",
        "why", "there", "here", "any", "all", "some", "no", "yes", "one",
        "two", "more", "most", "other", "such", "only", "own", "same",
        "very", "just", "now",
    }
)

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def _default_tokenize(
    text: str,
    *,
    stop_words: frozenset[str],
    min_token_len: int,
) -> set[str]:
    """Lowercase content-word tokens for Jaccard scoring.

    Filters out stop words and tokens shorter than ``min_token_len``.
    Only the default ASCII path; CJK / custom callers should pass a
    ``tokenizer`` into :class:`FewshotBank`.
    """
    out: set[str] = set()
    for match in _WORD_RE.findall(text):
        low = match.lower()
        if len(low) < min_token_len:
            continue
        if low in stop_words:
            continue
        out.add(low)
    return out


@dataclass
class FewshotCase:
    """A single solved case stored in a :class:`FewshotBank`."""

    id: str
    description: str
    response: str
    tags: tuple[str, ...] = ()
    score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


class FewshotBank:
    """In-memory store of solved cases with Jaccard retrieval.

    The bank is intentionally simple: a list of cases plus a tokenizer.
    Retrieval scores every case against the query, filters by
    ``min_score`` and ``required_tags``, then returns the top ``k``.
    """

    def __init__(
        self,
        cases: Sequence[FewshotCase] = (),
        *,
        stop_words: frozenset[str] | None = None,
        min_token_len: int = 3,
        tokenizer: Callable[[str], set[str]] | None = None,
    ) -> None:
        self._cases: list[FewshotCase] = []
        seen: set[str] = set()
        for case in cases:
            if case.id in seen:
                raise ValueError(f"duplicate id: {case.id}")
            seen.add(case.id)
            self._cases.append(case)
        self._stop_words: frozenset[str] = (
            stop_words if stop_words is not None else DEFAULT_STOP_WORDS
        )
        self._min_token_len = min_token_len
        self._tokenizer: Callable[[str], set[str]] = (
            tokenizer if tokenizer is not None else self._default_tokenizer
        )

    def _default_tokenizer(self, text: str) -> set[str]:
        return _default_tokenize(
            text,
            stop_words=self._stop_words,
            min_token_len=self._min_token_len,
        )

    # ── Construction / persistence ──────────────────────────────

    @classmethod
    def load(cls, path: str | Path, **kw: Any) -> "FewshotBank":
        """Load a bank from a JSON list-of-dict file.

        Each dict requires ``id``, ``description``, ``response``.
        Optional ``tags`` (list becomes tuple) and ``metadata`` (dict)
        are passed through. Unknown keys are collected into
        ``metadata``. For backward compatibility with legacy solved
        batches, dicts missing ``id`` but providing ``task_id`` will
        have their ``id`` synthesized from ``task_id``.
        """
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(str(p))
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"malformed bank file: {exc}") from exc
        if not isinstance(raw, list):
            raise ValueError(
                "malformed bank file: top-level JSON must be a list"
            )
        cases: list[FewshotCase] = []
        known = {"id", "description", "response", "tags", "metadata"}
        for entry in raw:
            if not isinstance(entry, dict):
                raise ValueError(
                    "malformed bank file: entries must be objects"
                )
            case_id = entry.get("id")
            if case_id is None and "task_id" in entry:
                case_id = entry["task_id"]
            if case_id is None:
                raise ValueError(
                    "malformed bank file: entry missing id/task_id"
                )
            description = entry.get("description", "")
            response = entry.get("response", "")
            tags_raw = entry.get("tags", [])
            tags: tuple[str, ...] = tuple(tags_raw) if tags_raw else ()
            metadata: dict[str, Any] = dict(entry.get("metadata", {}))
            for key, value in entry.items():
                if key in known or key == "task_id":
                    continue
                metadata.setdefault(key, value)
            cases.append(
                FewshotCase(
                    id=str(case_id),
                    description=str(description),
                    response=str(response),
                    tags=tags,
                    metadata=metadata,
                )
            )
        return cls(cases, **kw)

    def save(self, path: str | Path) -> None:
        """Persist the bank to ``path`` as a JSON list-of-dict."""
        out: list[dict[str, Any]] = []
        for case in self._cases:
            out.append(
                {
                    "id": case.id,
                    "description": case.description,
                    "response": case.response,
                    "tags": list(case.tags),
                    "metadata": case.metadata,
                }
            )
        Path(path).write_text(
            json.dumps(out, sort_keys=True, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    # ── Mutation ────────────────────────────────────────────────

    def add(self, case: FewshotCase) -> None:
        """Append a case. Raises ``ValueError`` on duplicate id."""
        for existing in self._cases:
            if existing.id == case.id:
                raise ValueError(f"duplicate id: {case.id}")
        self._cases.append(case)

    # ── Retrieval ───────────────────────────────────────────────

    def retrieve(
        self,
        query: str,
        k: int = 2,
        *,
        min_score: float = 0.0,
        required_tags: Sequence[str] = (),
    ) -> list[FewshotCase]:
        """Return the top-``k`` cases most similar to ``query``.

        Scores every case by Jaccard overlap, filters by ``min_score``
        and ``required_tags`` (case must contain *all* required tags),
        sorts descending, and returns the first ``k``. Each returned
        case has its ``score`` attribute mutated in place.

        Empty bank → ``[]``. Empty query → first ``k`` cases untouched,
        no scoring or filter (sanity-check path).
        """
        if not self._cases:
            return []
        if k <= 0:
            return []
        if query == "":
            return list(self._cases[:k])

        q_tokens = self._tokenizer(query)
        required = tuple(required_tags)
        scored: list[tuple[float, int, FewshotCase]] = []
        for idx, case in enumerate(self._cases):
            if required and not all(t in case.tags for t in required):
                continue
            d_tokens = self._tokenizer(case.description)
            union = q_tokens | d_tokens
            if not union:
                score = 0.0
            else:
                score = len(q_tokens & d_tokens) / len(union)
            if score < min_score:
                continue
            scored.append((score, idx, case))

        scored.sort(key=lambda item: (-item[0], item[1]))
        top = scored[:k]
        out: list[FewshotCase] = []
        for score, _idx, case in top:
            case.score = score
            out.append(case)
        return out

    # ── Dunder ──────────────────────────────────────────────────

    def __len__(self) -> int:
        return len(self._cases)

    def __iter__(self) -> Iterator[FewshotCase]:
        return iter(self._cases)


def load_bank(path: str | Path) -> FewshotBank:
    """Thin functional wrapper around :meth:`FewshotBank.load`."""
    return FewshotBank.load(path)


def retrieve_fewshot(
    query: str, bank: FewshotBank, k: int = 2
) -> list[FewshotCase]:
    """Thin functional wrapper around :meth:`FewshotBank.retrieve`."""
    return bank.retrieve(query, k)
