"""concinno.agent.retrieve_pipeline — ZIQ cascade pipeline orchestrator.

@module agent.retrieve_pipeline
@responsibility Glue layer between the three-layer cascade retriever
    (:class:`~concinno.agent.iterative_retrieve.IterativeRetriever`)
    and the FTRL source-weight reranker
    (:class:`~concinno.ziq_retrieval.ZIQRetrieval`).

    The two subsystems have different responsibilities and must not be
    merged:

    - ``IterativeRetriever`` = retrieval cascade (L3 cognitive_pool keyword
      search → L2 distiller fast-path → L1 caller-supplied RAG). It
      decides *where* to get candidates from and returns either cached
      sections or raw L1 hits.
    - ``ZIQRetrieval`` = FTRL source-weight rerank layer. It adjusts
      existing L1 result scores using learned per-source-type weights
      and records implicit feedback.

    :class:`ZIQCascadePipeline` composes them: run the cascade first,
    and *only when L1 fallthrough produced raw hits* hand those hits
    to the ZIQ reranker. L3/L2 cache-only results are returned as-is
    because FTRL weights operate on source types extracted from file
    paths, which the in-memory pool sections do not cleanly expose.

@dependencies concinno.agent.iterative_retrieve, concinno.ziq_retrieval
@exports CascadePipelineResult, ZIQCascadePipeline
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from concinno.agent.iterative_retrieve import (
    IterativeRetriever,
    RetrievalResult,
)

if TYPE_CHECKING:
    from typing import Any

    from concinno.cache.l2_distill import RawHit
    from concinno.ziq_retrieval import ZIQRetrieval

__all__ = [
    "CascadePipelineResult",
    "ZIQCascadePipeline",
]


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass
class CascadePipelineResult:
    """Unified result from the cascade + ZIQ rerank pipeline.

    Attributes:
        cascade: The raw :class:`RetrievalResult` from the cascade stage.
            Callers needing layer-level hit flags or the section list
            should read from this.
        reranked: The ZIQ-reranked L1 hits as ``list[dict]`` with added
            ``ziq_score`` and ``source_type`` keys. Empty when L1 did
            not fall through (cache-only result).
        rerank_applied: ``True`` iff the ZIQ reranker ran on this query.
    """

    cascade: RetrievalResult
    reranked: list[dict[str, Any]] = field(default_factory=list)
    rerank_applied: bool = False


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def _raw_hit_to_dict(hit: RawHit) -> dict[str, Any]:
    """Adapter: ``RawHit`` → ZIQRetrieval.rerank's ``list[dict]`` shape.

    ZIQRetrieval.rerank expects dicts with ``text`` / ``file`` / ``score``
    keys. ``RawHit.source`` is the free-form provenance string, which
    maps to ``file`` (FTRL source-type classifier reads this field).
    Any ``metadata`` fields are shallow-merged at the top level so
    downstream callers can still read them.
    """
    payload: dict[str, Any] = {
        "text": hit.text,
        "file": hit.source,
        "score": hit.score,
    }
    for key, value in hit.metadata.items():
        payload.setdefault(key, value)
    return payload


class ZIQCascadePipeline:
    """Compose :class:`IterativeRetriever` with :class:`ZIQRetrieval`.

    Flow per :meth:`retrieve` call:

    1. Run the three-layer cascade.
    2. If the cascade reached L1 and returned raw hits → convert them
       to the rerank dict shape and pass through ``ziq.rerank``. The
       reranker mutates its own FTRL state store as a side effect.
    3. Return a :class:`CascadePipelineResult` carrying both the raw
       cascade result and the (optionally) reranked L1 hits.

    ``feedback`` delegates to :meth:`ZIQRetrieval.feedback`, letting
    the caller report which sources were actually consumed downstream.

    Args:
        cascade: The retrieval cascade orchestrator.
        ziq: The FTRL reranker instance.

    Example:
        >>> pipeline = ZIQCascadePipeline(cascade=cascade, ziq=ziq)
        >>> result = pipeline.retrieve("how does cache_break_detector work")
        >>> if result.rerank_applied:
        ...     for hit in result.reranked:
        ...         print(hit["file"], hit["ziq_score"])
        >>> pipeline.feedback(["src/concinno/cache/cache_break_detector.py"])
    """

    def __init__(
        self,
        *,
        cascade: IterativeRetriever,
        ziq: ZIQRetrieval,
    ) -> None:
        self._cascade = cascade
        self._ziq = ziq

    # ---- public API ------------------------------------------------------

    def retrieve(self, query: str) -> CascadePipelineResult:
        """Run cascade → rerank pipeline for a single query.

        The reranker only runs when the cascade's L1 layer produced
        hits. Cache-only results (L3 or L2 sufficient) return an empty
        ``reranked`` list and ``rerank_applied=False``.
        """
        cascade_result = self._cascade.retrieve(query)

        if not cascade_result.l1_hit or not cascade_result.raw_hits:
            return CascadePipelineResult(
                cascade=cascade_result,
                reranked=[],
                rerank_applied=False,
            )

        rerank_input = [_raw_hit_to_dict(h) for h in cascade_result.raw_hits]
        reranked = self._ziq.rerank(rerank_input)

        return CascadePipelineResult(
            cascade=cascade_result,
            reranked=reranked,
            rerank_applied=True,
        )

    def feedback(self, used_files: list[str]) -> None:
        """Report which result sources were actually consumed downstream.

        Delegates verbatim to :meth:`ZIQRetrieval.feedback`, which
        updates FTRL weights for the next query's rerank step. No-op
        from the cascade layer's perspective — the cognitive_pool and
        l2_distiller maintain their own separate usage accounting via
        their own sinks.
        """
        self._ziq.feedback(used_files)
