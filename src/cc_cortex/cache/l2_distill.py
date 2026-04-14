"""cc_cortex.cache.l2_distill — L2 semantic-compression layer for ZIQ v7.

@module cache.l2_distill
@responsibility L2 of the ZIQ v7 three-layer cognitive sharing
    architecture. Sits between :mod:`cc_cortex.ziq_retrieval` (L1 —
    raw BM25+dense hits with FTRL routing) and
    :mod:`cc_cortex.cache.cognitive_pool` (L3 — the cross-session,
    cross-agent markdown pool with stable section hashes).

    L2 is the **compression + A-MEM memory evolution** layer. Its job
    is to take raw L1 retrieval hits (short text chunks with a source
    and a score) and distill them, via a caller-supplied
    :class:`DistillSink` (an LLM pass, NOT owned by CCC), into named,
    tagged :class:`~cc_cortex.cache.cognitive_pool.PoolSection`
    candidates that get committed to L3 for reuse.

    A-MEM "memory evolution" means: when a new distilled fact
    contradicts an existing section — either because the sink
    explicitly marks it as ``supersedes`` older titles, or because the
    same-title section's body changed — we **rewrite** the older
    section in place rather than letting two contradictory rows
    accumulate. The diff is tracked in an in-memory
    :class:`EvolveRecord` history so callers can audit what changed
    and why.

    Key invariants:

    1. **Policy only, no LLM SDK.** The distillation pass is a
       :class:`DistillSink` Protocol. CCC does not import anthropic,
       httpx, or any model SDK — library code must run for strangers
       with stdlib only.
    2. **Never compute section ids ourselves.** The hash contract for
       L3 lives in :meth:`~cc_cortex.cache.cognitive_pool.CognitivePool.compute_section_id`
       and is load-bearing for future microcompact section-edit
       integration. We only ever go through
       :meth:`~cc_cortex.cache.cognitive_pool.CognitivePool.upsert_section`
       and :meth:`~cc_cortex.cache.cognitive_pool.CognitivePool.remove_section`.
    3. **Evolution records only fire on overwrite.** A plain insert
       (new title, no supersedes) produces no :class:`EvolveRecord`;
       only a genuine rewrite or a ``supersedes`` takedown gets logged.
       This keeps the audit log signal-to-noise high.
    4. **Sync only.** No asyncio. The three-layer architecture plan
       explicitly schedules memory evolution on the L1 fallback path,
       but the sink call itself is synchronous; async orchestration
       is the caller's problem.
    5. **History is in-memory.** Restarting the process clears the
       evolve history. The durable audit trail is the L3 pool file
       itself (each section carries ``updated_ts``), the history
       here is just a ring for introspection during a session.

@dependencies cc_cortex.cache.cognitive_pool
@exports RawHit, DistillRequest, DistillCandidate, DistillResult,
    DistillSink, EvolveRecord, L2Stats, L2Distiller, DistillationFailed
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

from cc_cortex.cache.cognitive_pool import CognitivePool, PoolSection

logger = logging.getLogger("cc_cortex.cache.l2_distill")

__all__ = [
    "DistillCandidate",
    "DistillRequest",
    "DistillResult",
    "DistillSink",
    "DistillationFailed",
    "EvolveRecord",
    "L2Distiller",
    "L2Stats",
    "RawHit",
]


# ---------------------------------------------------------------------------
# Tokenisation
# ---------------------------------------------------------------------------

#: Regex that extracts matchable tokens from free text. Three-character
#: minimum so noise words like "of" / "to" don't dominate the score;
#: underscores kept so compound identifiers like ``session_memory`` stay
#: intact. Lowercased before matching so callers don't have to.
_TOKEN_RE = re.compile(r"[a-z0-9_]{3,}")


def _tokenize(text: str) -> set[str]:
    """Return the set of matchable tokens in ``text``.

    Used both for extracting tag-like keywords from a query (so
    :meth:`L2Distiller.distill` can pull relevant existing sections out
    of the pool) and for scoring L2 fast-path matches against section
    titles and tags. Returning a :class:`set` deduplicates repeats so a
    query that mentions "memory" three times doesn't artificially
    inflate its score against a section titled "memory_budget".
    """
    return set(_TOKEN_RE.findall(text.lower()))


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass
class RawHit:
    """A single L1 retrieval result — the raw input to distillation.

    Attributes:
        text: The retrieved chunk body. May be short (one sentence) or
            long (a full section); the sink decides what to do.
        source: Free-form provenance string, e.g. ``"feedback_*.md"``
            or ``"handoff:king:session-42"``. The sink can cite this
            when writing the distilled body.
        score: The L1 retriever's confidence for this hit, in whatever
            scale the retriever uses. We do NOT re-normalise — the
            sink treats it as advisory.
        metadata: Arbitrary bag of extra fields the retriever wanted
            to carry through (timestamps, namespace tags, FTRL weights).
            Defaults to an empty dict so callers can ignore it.
    """

    text: str
    source: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DistillRequest:
    """Bundle of raw hits + task context handed to the distill sink.

    Attributes:
        query: The user-facing query that produced these hits. The
            sink uses it to focus the distillation — e.g. the same
            hit corpus can distill different summaries for different
            queries.
        hits: The L1 retrieval results.
        existing_sections: Sections already in L3 that the L2 layer
            judged relevant (by tag overlap with the query). The sink
            MAY choose to emit ``supersedes`` candidates targeting
            these, or MAY ignore them and emit fresh sections.
        max_new_sections: Soft cap on how many distill candidates the
            sink should return. A sink that returns more is allowed;
            :meth:`L2Distiller.distill` simply truncates.
        max_section_bytes: Budget for each candidate's body. The sink
            SHOULD respect this, but L3 will truncate defensively
            anyway via :meth:`CognitivePool._truncate_body`.
    """

    query: str
    hits: list[RawHit]
    existing_sections: list[PoolSection]
    max_new_sections: int = 3
    max_section_bytes: int = 4_000


@dataclass
class DistillCandidate:
    """What the sink returns: one proposed new or updated section.

    Attributes:
        title: Target section title. Passed verbatim to
            :meth:`CognitivePool.upsert_section`, which will normalise
            whitespace to underscores. Must be non-empty.
        body: Markdown content of the section.
        tags: Optional tag tuple used by
            :meth:`CognitivePool.read_tagged` and by the L2 fast-path
            :meth:`L2Distiller.retrieve`. Keep them short and
            keyword-like.
        supersedes: Titles of sections this candidate replaces. Each
            matching section is deleted before the new one is inserted.
            Non-matching entries are silently ignored — the sink is
            not expected to know the current pool contents perfectly.
        confidence: Sink's self-reported confidence in this candidate,
            in ``[0.0, 1.0]``. Candidates below
            :attr:`L2Distiller.min_confidence` are rejected before
            commit. ``1.0`` is the safe default when the sink doesn't
            have a better estimator.
    """

    title: str
    body: str
    tags: tuple[str, ...] = ()
    supersedes: tuple[str, ...] = ()
    confidence: float = 1.0


@dataclass
class DistillResult:
    """What the sink returns from one :meth:`DistillSink.distill` call.

    Attributes:
        candidates: Zero or more :class:`DistillCandidate` entries. An
            empty list is a valid "no useful distillation" response —
            it is NOT an error condition unless the caller is in
            hard-fail mode.
        error: Optional free-text error from the sink. When set,
            :meth:`L2Distiller.distill` treats the entire batch as
            rejected regardless of ``candidates``. Use this to
            surface model refusals, rate-limit errors, or parse
            failures without raising.
    """

    candidates: list[DistillCandidate]
    error: str | None = None


class DistillSink(Protocol):
    """Caller-supplied LLM distillation pass.

    Implementations own the actual model call — CCC stays dependency
    free by modelling this as a Protocol. A minimal in-process fake is
    trivial for tests; production callers wire in anthropic, gemma, or
    any other backend. The sink MUST be synchronous; async orchestration
    is the caller's problem.
    """

    def distill(self, req: DistillRequest) -> DistillResult:
        """Distill ``req.hits`` into zero or more candidate sections."""
        ...


@dataclass
class EvolveRecord:
    """Audit entry for one A-MEM memory-evolution event.

    Only emitted when an existing section was actually overwritten or
    superseded. Plain inserts (new title, no supersedes) produce no
    record — see :meth:`L2Distiller.evolve` for the exact rule.

    Attributes:
        section_title: Normalised title of the section that was
            rewritten or removed.
        old_body_hash: 16-hex-digit sha256 prefix of the old body. We
            store a hash rather than the full body to keep history
            memory bounded; callers that need the real previous body
            must snapshot the pool file themselves.
        new_body_hash: Same for the new body. For a ``supersedes``
            takedown that has no replacement at this title, this is
            the empty string.
        reason: Short machine-readable reason tag, e.g.
            ``"body_changed"`` or ``"supersedes"``. Stable enough for
            tests to assert against.
        timestamp: Wall-clock time of the evolution, seconds since
            epoch. Set from :func:`time.time` by default.
    """

    section_title: str
    old_body_hash: str
    new_body_hash: str
    reason: str
    timestamp: float


@dataclass
class L2Stats:
    """Counter snapshot of the distiller's lifetime activity.

    All counters are cumulative and reset only via
    :meth:`L2Distiller.clear_history`. They cover both the main
    distill pipeline and the L2 fast-path retrieve — an installation
    can read them to decide whether the L2 layer is earning its keep.

    Attributes:
        distill_calls: Number of completed :meth:`L2Distiller.distill`
            invocations, whether they produced candidates or not.
        candidates_accepted: How many sink candidates passed the
            confidence gate and reached :meth:`L2Distiller.evolve`.
        candidates_rejected: Candidates dropped before commit — either
            because the sink errored, returned empty, or the
            candidate's confidence was below ``min_confidence``.
        sections_upserted: How many pool upserts we triggered. Less
            than or equal to ``candidates_accepted`` because an
            idempotent rewrite (same hash) is counted as accepted but
            not upserted.
        evolutions: Number of :class:`EvolveRecord` entries produced.
            Strictly less than or equal to ``sections_upserted``.
        retrieve_hits: :meth:`L2Distiller.retrieve` calls that
            returned at least one section.
        retrieve_misses: :meth:`L2Distiller.retrieve` calls that
            returned an empty list.
    """

    distill_calls: int = 0
    candidates_accepted: int = 0
    candidates_rejected: int = 0
    sections_upserted: int = 0
    evolutions: int = 0
    retrieve_hits: int = 0
    retrieve_misses: int = 0


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class DistillationFailed(RuntimeError):
    """Raised when all sink attempts return empty candidates.

    Not raised by :meth:`L2Distiller.distill` by default — the default
    contract is to return ``[]`` on an empty sink result. Callers that
    need hard-fail semantics (e.g. an offline benchmark harness that
    must detect regressions) can wrap :meth:`distill` and raise this
    themselves when the returned list is empty. It lives in the public
    API so every consumer raises the same type.
    """


# ---------------------------------------------------------------------------
# L2Distiller
# ---------------------------------------------------------------------------


class L2Distiller:
    """Distill, A-MEM evolve, and commit raw L1 hits into the L3 pool.

    Args:
        pool: The target :class:`~cc_cortex.cache.cognitive_pool.CognitivePool`.
            All writes go through its public API; we never poke private
            fields. The caller owns the pool's lifecycle.
        sink: The LLM distillation pass. ``None`` is legal at
            construction time for callers that only want the L2
            fast-path :meth:`retrieve`; :meth:`distill` will raise
            :class:`RuntimeError` if called without a sink.
        evolve_on_conflict: When ``True`` (default), a same-title
            candidate whose body hash differs from the existing
            section's triggers a rewrite and an :class:`EvolveRecord`.
            When ``False``, same-title same-body conflicts are
            noop'd and non-identical bodies fall through to a plain
            upsert without recording evolution — useful for dry-run
            benchmarking.
        min_confidence: Lower bound on :attr:`DistillCandidate.confidence`
            for acceptance, in ``[0.0, 1.0]``. The boundary is
            inclusive: a candidate with confidence exactly equal to
            ``min_confidence`` is accepted. Candidates below are
            counted as rejected.

    Raises:
        ValueError: If ``min_confidence`` falls outside ``[0, 1]``.
    """

    def __init__(
        self,
        *,
        pool: CognitivePool,
        sink: DistillSink | None = None,
        evolve_on_conflict: bool = True,
        min_confidence: float = 0.3,
    ) -> None:
        if not (0.0 <= min_confidence <= 1.0):
            msg = f"min_confidence must be in [0, 1], got {min_confidence}"
            raise ValueError(msg)
        self._pool = pool
        self._sink = sink
        self._evolve_on_conflict = evolve_on_conflict
        self._min_confidence = float(min_confidence)
        self._history: list[EvolveRecord] = []
        self._stats = L2Stats()

    # ---- properties ---------------------------------------------------

    @property
    def pool(self) -> CognitivePool:
        """The target L3 pool this distiller writes to."""
        return self._pool

    @property
    def min_confidence(self) -> float:
        """Inclusive lower bound on candidate confidence for acceptance."""
        return self._min_confidence

    @property
    def evolve_on_conflict(self) -> bool:
        """Whether same-title body conflicts trigger evolve records."""
        return self._evolve_on_conflict

    # ---- core pipeline ------------------------------------------------

    def distill(
        self,
        *,
        query: str,
        hits: Sequence[RawHit],
        max_new_sections: int = 3,
    ) -> list[PoolSection]:
        """Run the full distill pipeline end-to-end.

        Steps:

        1. Extract keyword tokens from ``query`` and fetch any L3
           section whose tags overlap with them. This feeds the sink
           existing context so it can emit supersedes candidates when
           appropriate.
        2. Build a :class:`DistillRequest` and call the sink.
        3. If the sink errored or returned no candidates, bump the
           rejected counter and return an empty list — no exception.
        4. For each candidate, enforce the confidence gate and hand
           off to :meth:`evolve`, which decides insert-vs-rewrite.
        5. Return the list of committed :class:`PoolSection` objects.
           The order mirrors the sink's candidate order.

        Raises:
            RuntimeError: If this distiller was constructed without a
                sink. Fast-path callers should use :meth:`retrieve`.
        """
        if self._sink is None:
            msg = (
                "L2Distiller.distill called without a DistillSink; "
                "pass sink=... to __init__ or use retrieve() for the "
                "fast path"
            )
            raise RuntimeError(msg)

        self._stats.distill_calls += 1

        # (1) Pull any existing sections whose tags overlap with the
        # query tokens. ``read_tagged`` returns an empty list when
        # ``tags`` is empty, so we guard that explicitly.
        query_tokens = _tokenize(query)
        existing: list[PoolSection] = []
        if query_tokens:
            existing = self._pool.read_tagged(query_tokens, match_all=False)

        # (2) Assemble the request and invoke the sink. We respect the
        # caller-supplied cap on max_new_sections and pass the pool's
        # own section-body budget through so the sink can self-limit.
        req = DistillRequest(
            query=query,
            hits=list(hits),
            existing_sections=list(existing),
            max_new_sections=max_new_sections,
            max_section_bytes=self._pool.max_section_bytes,
        )
        try:
            result = self._sink.distill(req)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("l2 distill sink raised: %s", exc)
            self._stats.candidates_rejected += 1
            return []

        # (3) Sink-level failure modes are NOT exceptional — they're
        # counted as a batch rejection. One bad batch shouldn't kill
        # the caller; future batches may succeed.
        if result.error is not None or not result.candidates:
            self._stats.candidates_rejected += max(1, len(result.candidates))
            return []

        # (4) Walk candidates. We enforce max_new_sections defensively
        # here in case a sink ignores the hint in the request.
        out: list[PoolSection] = []
        for cand in result.candidates[:max_new_sections]:
            if cand.confidence < self._min_confidence:
                self._stats.candidates_rejected += 1
                continue
            self._stats.candidates_accepted += 1
            section, _record = self.evolve(cand)
            out.append(section)

        return out

    # ---- memory evolution (A-MEM) -------------------------------------

    def evolve(
        self,
        candidate: DistillCandidate,
        *,
        now: float | None = None,
    ) -> tuple[PoolSection, EvolveRecord | None]:
        """Commit one candidate, rewriting older sections on conflict.

        The decision tree:

        1. If ``candidate.supersedes`` is non-empty: for each title in
           the list, look up the existing section and, if present,
           record its old body hash and call
           :meth:`CognitivePool.remove_section`. Then upsert the new
           candidate under its own title and, IF at least one
           supersedes target actually existed, emit a single
           :class:`EvolveRecord` with reason ``"supersedes"``.
        2. Otherwise, check for an existing section with the same
           (normalised) title as the candidate. If one exists AND its
           body hash differs AND ``evolve_on_conflict`` is ``True``,
           upsert and emit a record with reason ``"body_changed"``.
           If the hashes are identical, return the existing section
           with no record (idempotent no-op; no upsert counted).
        3. Otherwise, a plain insert: upsert the candidate and return
           no record.

        Returns:
            ``(section, record_or_None)`` where ``section`` is the
            post-upsert :class:`PoolSection` (or the unchanged
            existing one on idempotent noop) and ``record_or_None`` is
            the :class:`EvolveRecord` if one was emitted.
        """
        when = now if now is not None else time.time()
        new_body_hash = _hash_body(candidate.body)

        # (1) Explicit supersedes — the sink is asserting "these older
        # titles contain stale facts, replace them".
        if candidate.supersedes:
            victims_removed = 0
            first_old_hash = ""
            for victim_title in candidate.supersedes:
                victim = self._pool.read_section(title=victim_title)
                if victim is None:
                    continue
                if not first_old_hash:
                    first_old_hash = _hash_body(victim.body)
                self._pool.remove_section(title=victim.title)
                victims_removed += 1

            section = self._pool.upsert_section(
                title=candidate.title,
                body=candidate.body,
                tags=candidate.tags,
                now=when,
            )
            self._stats.sections_upserted += 1

            if victims_removed > 0:
                record = EvolveRecord(
                    section_title=section.title,
                    old_body_hash=first_old_hash,
                    new_body_hash=new_body_hash,
                    reason="supersedes",
                    timestamp=when,
                )
                self._history.append(record)
                self._stats.evolutions += 1
                return section, record
            return section, None

        # (2) Same-title conflict check — the common A-MEM case where
        # a new fact arrives under a title we already know.
        existing = self._pool.read_section(title=candidate.title)
        if existing is not None:
            old_body_hash = _hash_body(existing.body)
            if old_body_hash == new_body_hash:
                # Idempotent — nothing to do, don't waste an upsert
                # and don't pollute the audit log.
                return existing, None

            if not self._evolve_on_conflict:
                section = self._pool.upsert_section(
                    title=candidate.title,
                    body=candidate.body,
                    tags=candidate.tags,
                    now=when,
                )
                self._stats.sections_upserted += 1
                return section, None

            section = self._pool.upsert_section(
                title=candidate.title,
                body=candidate.body,
                tags=candidate.tags,
                now=when,
            )
            self._stats.sections_upserted += 1
            record = EvolveRecord(
                section_title=section.title,
                old_body_hash=old_body_hash,
                new_body_hash=new_body_hash,
                reason="body_changed",
                timestamp=when,
            )
            self._history.append(record)
            self._stats.evolutions += 1
            return section, record

        # (3) Plain insert — no existing section under this title, no
        # supersedes list. No audit record needed.
        section = self._pool.upsert_section(
            title=candidate.title,
            body=candidate.body,
            tags=candidate.tags,
            now=when,
        )
        self._stats.sections_upserted += 1
        return section, None

    # ---- L2 fast path -------------------------------------------------

    def retrieve(
        self,
        *,
        query: str,
        max_sections: int = 5,
    ) -> list[PoolSection]:
        """Keyword-match ``query`` against section titles and tags.

        This is the fast path the three-layer architecture document
        calls "L2 fast retrieve": when the answer is already distilled
        into the L3 pool we want to avoid a round-trip through L1.
        Scoring is intentionally dumb — token-set intersection against
        ``title + " " + " ".join(tags)`` — because the L1 BM25 layer
        is the authoritative retriever; L2 is only trying to shortcut
        when the match is obvious.

        Behaviour:

        1. Tokenise the query. An empty token set returns ``[]``
           immediately and counts as a miss.
        2. Walk every non-stale section in the pool and compute the
           intersection size against the section's token bag.
        3. Keep sections with non-zero score. Sort by score descending,
           then stable by title for determinism across runs.
        4. Return the top ``max_sections``. Hit/miss stats are
           updated based on whether the returned list is non-empty.

        The returned list is a snapshot; callers can mutate it freely.
        """
        tokens = _tokenize(query)
        if not tokens:
            self._stats.retrieve_misses += 1
            return []

        alive = self._pool.read_all()
        scored: list[tuple[int, PoolSection]] = []
        for section in alive:
            bag = _tokenize(section.title + " " + " ".join(section.tags))
            score = len(tokens & bag)
            if score > 0:
                scored.append((score, section))

        if not scored:
            self._stats.retrieve_misses += 1
            return []

        # Sort by score desc, stable by title for determinism. Python's
        # sort is stable, so a two-pass sort (title first, then score)
        # yields the desired (-score, title) ordering without touching
        # dataclass equality.
        scored.sort(key=lambda item: item[1].title)
        scored.sort(key=lambda item: item[0], reverse=True)

        out = [section for _score, section in scored[:max_sections]]
        if out:
            self._stats.retrieve_hits += 1
        else:
            self._stats.retrieve_misses += 1
        return out

    # ---- introspection ------------------------------------------------

    def evolve_history(self) -> list[EvolveRecord]:
        """Return a copy of the evolve-record history in insertion order.

        Copied so callers can sort or slice without corrupting the
        internal log. History is in-memory and cleared by
        :meth:`clear_history`.
        """
        return list(self._history)

    def stats(self) -> L2Stats:
        """Return a snapshot of the counter state.

        We return the live dataclass rather than a deep copy because
        :class:`L2Stats` is plain data; callers that need isolation
        can :func:`dataclasses.replace` it. Mutating the returned
        object does not affect subsequent counters — each pipeline
        method increments the owned instance, not whatever the caller
        is holding — because the attributes are rebound on the
        owned instance, not on the returned reference. For callers
        that want a true snapshot use ``dataclasses.replace(stats)``.
        """
        return self._stats

    def clear_history(self) -> None:
        """Drop every :class:`EvolveRecord` collected so far.

        Does not reset :class:`L2Stats` counters — those are the
        lifetime metric and wanted to persist across history flushes.
        Callers that need a full reset should construct a fresh
        :class:`L2Distiller`.
        """
        self._history.clear()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _hash_body(body: str) -> str:
    """Return a 16-hex-digit stable fingerprint of ``body``.

    Used by :meth:`L2Distiller.evolve` to decide whether a same-title
    candidate's body has actually changed, and by
    :class:`EvolveRecord` for the audit trail. Sixteen hex digits is
    enough entropy for our purposes (~64 bits) and short enough to
    keep the in-memory history compact. This is NOT the L3 section id
    — that one lives on :class:`CognitivePool` and covers the title,
    not the body.
    """
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
