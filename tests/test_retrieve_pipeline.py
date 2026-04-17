"""Tests for concinno.agent.retrieve_pipeline — ZIQ cascade pipeline glue."""

from __future__ import annotations

from pathlib import Path

from concinno.agent.iterative_retrieve import (
    CascadeConfig,
    IterativeRetriever,
)
from concinno.agent.retrieve_pipeline import (
    CascadePipelineResult,
    ZIQCascadePipeline,
)
from concinno.cache.cognitive_pool import CognitivePool
from concinno.cache.l2_distill import L2Distiller, RawHit
from concinno.ziq_retrieval import ZIQRetrieval

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeL1Retriever:
    """Controllable L1 retriever."""

    def __init__(self, hits: list[RawHit] | None = None) -> None:
        self.hits = hits or []
        self.calls: list[tuple[str, int]] = []

    def retrieve(self, query: str, *, top_k: int = 5) -> list[RawHit]:
        self.calls.append((query, top_k))
        return list(self.hits)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pool(
    tmp_path: object,
    sections: list[tuple[str, str, tuple[str, ...]]],
) -> CognitivePool:
    root = Path(str(tmp_path)) / "pool"
    root.mkdir(parents=True, exist_ok=True)
    pool = CognitivePool(root=root)
    for title, body, tags in sections:
        pool.upsert_section(title=title, body=body, tags=tags, now=1000.0)
    return pool


def _make_pipeline(
    tmp_path: object,
    *,
    pool_sections: list[tuple[str, str, tuple[str, ...]]] | None = None,
    l1_hits: list[RawHit] | None = None,
    config: CascadeConfig | None = None,
) -> tuple[ZIQCascadePipeline, FakeL1Retriever, ZIQRetrieval]:
    pool = _make_pool(tmp_path, pool_sections or [])
    distiller = L2Distiller(pool=pool, sink=None)
    l1 = FakeL1Retriever(hits=l1_hits)
    cascade = IterativeRetriever(
        pool=pool,
        distiller=distiller,
        l1=l1,
        scheduler=None,
        config=config,
    )
    ziq_cache = Path(str(tmp_path)) / "ziq"
    ziq_cache.mkdir(parents=True, exist_ok=True)
    ziq = ZIQRetrieval(cache_dir=str(ziq_cache))
    pipeline = ZIQCascadePipeline(cascade=cascade, ziq=ziq)
    return pipeline, l1, ziq


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_l3_hit_skips_rerank(tmp_path: object) -> None:
    """When L3 sufficiency → no L1, rerank is not applied."""
    pipeline, l1, _ = _make_pipeline(
        tmp_path,
        pool_sections=[("memory_budget", "notes on memory", ("memory",))],
        l1_hits=[RawHit(text="x", source="feedback/x.md", score=0.9)],
    )
    result = pipeline.retrieve("memory budget")
    assert isinstance(result, CascadePipelineResult)
    assert result.cascade.l3_hit is True
    assert result.cascade.l1_hit is False
    assert result.rerank_applied is False
    assert result.reranked == []
    assert l1.calls == []  # L1 never invoked


def test_l2_hit_skips_rerank(tmp_path: object) -> None:
    """When L2 fast-path returns enough sections, rerank is not applied."""
    # Pool holds a section that L2 will find via tag overlap, but
    # L3 keyword filter on title/tags won't match the specific query.
    pipeline, l1, _ = _make_pipeline(
        tmp_path,
        pool_sections=[("alpha_notes", "content", ("shared_tag",))],
        l1_hits=[RawHit(text="x", source="feedback/x.md", score=0.9)],
    )
    # Query that shares a token only with tags → routes through L2
    result = pipeline.retrieve("shared_tag lookup")
    # Either L3 or L2 should have covered this — both skip rerank
    assert result.cascade.l1_hit is False
    assert result.rerank_applied is False
    assert result.reranked == []


def test_l1_fallthrough_runs_rerank(tmp_path: object) -> None:
    """Empty cache → L1 fallthrough → ZIQ rerank runs on raw_hits."""
    pipeline, l1, _ = _make_pipeline(
        tmp_path,
        pool_sections=[],  # empty L3
        l1_hits=[
            RawHit(text="fix A", source="feedback/fix_a.md", score=0.9),
            RawHit(text="note B", source="handoff/session.md", score=0.7),
        ],
    )
    result = pipeline.retrieve("how to fix corruption")
    assert result.cascade.l1_hit is True
    assert result.rerank_applied is True
    assert len(result.reranked) == 2
    for hit in result.reranked:
        assert "ziq_score" in hit
        assert "source_type" in hit
        assert hit["file"] in {
            "feedback/fix_a.md",
            "handoff/session.md",
        }
    assert len(l1.calls) == 1


def test_raw_hit_metadata_preserved_in_rerank_dict(tmp_path: object) -> None:
    """RawHit.metadata fields land at top level of rerank dict."""
    pipeline, _, _ = _make_pipeline(
        tmp_path,
        pool_sections=[],
        l1_hits=[
            RawHit(
                text="body",
                source="rules/rule.md",
                score=0.5,
                metadata={"timestamp": 999, "namespace": "rules"},
            ),
        ],
    )
    result = pipeline.retrieve("query")
    assert result.rerank_applied is True
    hit = result.reranked[0]
    assert hit["timestamp"] == 999
    assert hit["namespace"] == "rules"
    # Core keys are not overwritten by metadata
    assert hit["text"] == "body"
    assert hit["file"] == "rules/rule.md"


def test_l1_empty_hits_no_rerank(tmp_path: object) -> None:
    """L1 returns [] → l1_hit False → rerank_applied False."""
    pipeline, l1, _ = _make_pipeline(
        tmp_path,
        pool_sections=[],
        l1_hits=[],
    )
    result = pipeline.retrieve("nothing will match")
    assert result.cascade.l1_hit is False
    assert result.rerank_applied is False
    assert result.reranked == []
    assert len(l1.calls) == 1


def test_feedback_delegates_to_ziq(tmp_path: object) -> None:
    """pipeline.feedback forwards to ZIQRetrieval.feedback, updating state."""
    pipeline, _, ziq = _make_pipeline(
        tmp_path,
        pool_sections=[],
        l1_hits=[
            RawHit(text="x", source="feedback/fix.md", score=0.8),
        ],
    )
    # Run one retrieve so there's a "last rerank results" for feedback
    pipeline.retrieve("anything")
    states_before = ziq.get_weights()
    pipeline.feedback(["feedback/fix.md"])
    states_after = ziq.get_weights()
    # At least one source-type weight should have moved
    assert states_after != states_before
