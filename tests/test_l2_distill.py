"""Tests for :mod:`cc_cortex.cache.l2_distill`.

These exercise the L2 distill layer against a real
:class:`~cc_cortex.cache.cognitive_pool.CognitivePool` backed by a
``tmp_path`` directory. The LLM distillation pass is faked with a
simple scriptable :class:`FakeDistillSink` that returns canned
:class:`~cc_cortex.cache.l2_distill.DistillResult` batches, one per
``distill`` call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from cc_cortex.cache.cognitive_pool import CognitivePool, PoolSection
from cc_cortex.cache.l2_distill import (
    DistillationFailed,
    DistillCandidate,
    DistillRequest,
    DistillResult,
    EvolveRecord,
    L2Distiller,
    L2Stats,
    RawHit,
)

# ---------------------------------------------------------------------------
# Fixtures and fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeDistillSink:
    """Scriptable sink: pops one :class:`DistillResult` per call.

    Any extra state we need for assertions (the last request seen,
    call count) is captured on this dataclass too so tests can read
    it back without patching.
    """

    batches: list[DistillResult] = field(default_factory=list)
    calls: int = 0
    last_request: DistillRequest | None = None

    def distill(self, req: DistillRequest) -> DistillResult:
        self.calls += 1
        self.last_request = req
        if not self.batches:
            return DistillResult(candidates=[])
        return self.batches.pop(0)


@pytest.fixture()
def pool(tmp_path: Path) -> CognitivePool:
    return CognitivePool(root=tmp_path / "pool", max_sections=50)


@pytest.fixture()
def sink() -> FakeDistillSink:
    return FakeDistillSink()


@pytest.fixture()
def distiller(pool: CognitivePool, sink: FakeDistillSink) -> L2Distiller:
    return L2Distiller(pool=pool, sink=sink, min_confidence=0.3)


def _seed(pool: CognitivePool, title: str, body: str, *, tags: tuple[str, ...] = ()) -> PoolSection:
    """Insert a section directly so tests can set up state without the sink."""
    return pool.upsert_section(title=title, body=body, tags=tags)


# ---------------------------------------------------------------------------
# (1) RawHit dataclass defaults
# ---------------------------------------------------------------------------


def test_raw_hit_dataclass_defaults() -> None:
    hit = RawHit(text="some text", source="feedback_x.md", score=0.9)
    assert hit.text == "some text"
    assert hit.source == "feedback_x.md"
    assert hit.score == pytest.approx(0.9)
    assert hit.metadata == {}
    # A fresh default should not be aliased with any other instance.
    hit.metadata["k"] = "v"
    other = RawHit(text="t", source="s", score=0.0)
    assert other.metadata == {}


# ---------------------------------------------------------------------------
# (2) DistillRequest constructible from a query + hits
# ---------------------------------------------------------------------------


def test_distill_request_builds_from_query() -> None:
    hits = [RawHit(text="abc", source="src", score=1.0)]
    req = DistillRequest(query="what is X?", hits=hits, existing_sections=[])
    assert req.query == "what is X?"
    assert req.hits is hits
    assert req.existing_sections == []
    assert req.max_new_sections == 3
    assert req.max_section_bytes == 4_000


# ---------------------------------------------------------------------------
# (3) DistillCandidate default fields
# ---------------------------------------------------------------------------


def test_distill_candidate_defaults() -> None:
    c = DistillCandidate(title="memory_budget", body="500 tokens")
    assert c.title == "memory_budget"
    assert c.tags == ()
    assert c.supersedes == ()
    assert c.confidence == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# (4) distill() without a sink must raise
# ---------------------------------------------------------------------------


def test_distill_no_sink_raises(pool: CognitivePool) -> None:
    d = L2Distiller(pool=pool, sink=None)
    with pytest.raises(RuntimeError, match="without a DistillSink"):
        d.distill(query="anything", hits=[])


# ---------------------------------------------------------------------------
# (5) Empty sink result yields empty list
# ---------------------------------------------------------------------------


def test_distill_empty_hits_returns_empty(distiller: L2Distiller, sink: FakeDistillSink) -> None:
    sink.batches.append(DistillResult(candidates=[]))
    out = distiller.distill(query="q", hits=[])
    assert out == []
    assert distiller.stats().distill_calls == 1
    assert distiller.stats().candidates_rejected >= 1


# ---------------------------------------------------------------------------
# (6) Low-confidence candidates are rejected before commit
# ---------------------------------------------------------------------------


def test_distill_low_confidence_rejected(
    distiller: L2Distiller, sink: FakeDistillSink, pool: CognitivePool
) -> None:
    sink.batches.append(
        DistillResult(
            candidates=[
                DistillCandidate(title="low", body="body", confidence=0.1),
            ]
        )
    )
    out = distiller.distill(query="q", hits=[])
    assert out == []
    assert distiller.stats().candidates_rejected == 1
    assert distiller.stats().candidates_accepted == 0
    assert pool.read_section(title="low") is None


# ---------------------------------------------------------------------------
# (7) Distill commits a new section end-to-end
# ---------------------------------------------------------------------------


def test_distill_commits_new_section_to_pool(
    distiller: L2Distiller, sink: FakeDistillSink, pool: CognitivePool
) -> None:
    sink.batches.append(
        DistillResult(
            candidates=[
                DistillCandidate(
                    title="king_plan",
                    body="Step 1: do the thing.",
                    tags=("king", "plan"),
                    confidence=0.8,
                )
            ]
        )
    )
    out = distiller.distill(query="king plan", hits=[RawHit("t", "s", 0.1)])
    assert len(out) == 1
    assert out[0].title == "king_plan"
    stored = pool.read_section(title="king_plan")
    assert stored is not None
    assert "Step 1" in stored.body
    assert distiller.stats().sections_upserted == 1


# ---------------------------------------------------------------------------
# (8) max_new_sections caps the sink's output
# ---------------------------------------------------------------------------


def test_distill_multi_candidate_respects_max_new(
    distiller: L2Distiller, sink: FakeDistillSink, pool: CognitivePool
) -> None:
    sink.batches.append(
        DistillResult(
            candidates=[
                DistillCandidate(title=f"s{i}", body=f"b{i}", confidence=0.9)
                for i in range(5)
            ]
        )
    )
    out = distiller.distill(query="q", hits=[], max_new_sections=2)
    assert len(out) == 2
    assert [s.title for s in out] == ["s0", "s1"]
    # Only the first two should have been committed.
    assert pool.read_section(title="s0") is not None
    assert pool.read_section(title="s1") is not None
    assert pool.read_section(title="s2") is None


# ---------------------------------------------------------------------------
# (9) evolve inserting a brand-new candidate → no record
# ---------------------------------------------------------------------------


def test_evolve_insert_new_no_record(distiller: L2Distiller, pool: CognitivePool) -> None:
    cand = DistillCandidate(title="fresh", body="first version")
    section, record = distiller.evolve(cand)
    assert section.title == "fresh"
    assert record is None
    assert distiller.evolve_history() == []
    assert distiller.stats().sections_upserted == 1
    assert distiller.stats().evolutions == 0


# ---------------------------------------------------------------------------
# (10) Same-title, same-body evolve is a no-op
# ---------------------------------------------------------------------------


def test_evolve_same_title_same_hash_is_noop(
    distiller: L2Distiller, pool: CognitivePool
) -> None:
    _seed(pool, "idem", "same body")
    cand = DistillCandidate(title="idem", body="same body")
    section, record = distiller.evolve(cand)
    assert record is None
    assert section.body == "same body"
    # No upsert should have been counted for a pure noop.
    assert distiller.stats().sections_upserted == 0
    assert distiller.stats().evolutions == 0


# ---------------------------------------------------------------------------
# (11) Same-title, different body → rewrite + record
# ---------------------------------------------------------------------------


def test_evolve_same_title_different_body_rewrites_and_records(
    distiller: L2Distiller, pool: CognitivePool
) -> None:
    _seed(pool, "fact", "old truth")
    cand = DistillCandidate(title="fact", body="new truth")
    section, record = distiller.evolve(cand, now=1_000.0)
    assert section.body == "new truth"
    assert record is not None
    assert record.reason == "body_changed"
    assert record.section_title == "fact"
    assert record.old_body_hash != record.new_body_hash
    assert record.timestamp == 1_000.0
    assert distiller.stats().evolutions == 1


# ---------------------------------------------------------------------------
# (12) supersedes deletes a single old section
# ---------------------------------------------------------------------------


def test_evolve_supersedes_deletes_old_section(
    distiller: L2Distiller, pool: CognitivePool
) -> None:
    _seed(pool, "old_name", "old body")
    cand = DistillCandidate(
        title="new_name",
        body="replacement",
        supersedes=("old_name",),
    )
    section, record = distiller.evolve(cand)
    assert section.title == "new_name"
    assert record is not None
    assert record.reason == "supersedes"
    # Old section is gone.
    assert pool.read_section(title="old_name") is None
    assert pool.read_section(title="new_name") is not None


# ---------------------------------------------------------------------------
# (13) supersedes with multiple victims still emits one record
# ---------------------------------------------------------------------------


def test_evolve_supersedes_multiple_sections(
    distiller: L2Distiller, pool: CognitivePool
) -> None:
    _seed(pool, "v1", "body v1")
    _seed(pool, "v2", "body v2")
    _seed(pool, "v3", "body v3")
    cand = DistillCandidate(
        title="consolidated",
        body="merged",
        supersedes=("v1", "v2", "v3"),
    )
    section, record = distiller.evolve(cand)
    assert section.title == "consolidated"
    assert record is not None
    assert record.reason == "supersedes"
    for title in ("v1", "v2", "v3"):
        assert pool.read_section(title=title) is None
    # Only one record even though three victims fell.
    assert len(distiller.evolve_history()) == 1


# ---------------------------------------------------------------------------
# (14) retrieve matches by title tokens
# ---------------------------------------------------------------------------


def test_retrieve_matches_by_title_tokens(
    distiller: L2Distiller, pool: CognitivePool
) -> None:
    _seed(pool, "memory_budget", "body", tags=("memory",))
    _seed(pool, "unrelated_topic", "body")
    out = distiller.retrieve(query="what is the memory budget")
    titles = [s.title for s in out]
    assert "memory_budget" in titles
    assert distiller.stats().retrieve_hits == 1


# ---------------------------------------------------------------------------
# (15) retrieve matches by tags
# ---------------------------------------------------------------------------


def test_retrieve_matches_by_tags(
    distiller: L2Distiller, pool: CognitivePool
) -> None:
    _seed(pool, "zeta", "body", tags=("king", "plan"))
    _seed(pool, "omega", "body", tags=("other",))
    out = distiller.retrieve(query="king roadmap")
    titles = [s.title for s in out]
    assert titles == ["zeta"]


# ---------------------------------------------------------------------------
# (16) retrieve respects max_sections
# ---------------------------------------------------------------------------


def test_retrieve_returns_top_k(
    distiller: L2Distiller, pool: CognitivePool
) -> None:
    for i in range(6):
        _seed(pool, f"plan_{i}", "body", tags=("plan",))
    out = distiller.retrieve(query="plan", max_sections=3)
    assert len(out) == 3


# ---------------------------------------------------------------------------
# (17) retrieve on an empty pool returns empty and counts miss
# ---------------------------------------------------------------------------


def test_retrieve_empty_pool_returns_empty(distiller: L2Distiller) -> None:
    out = distiller.retrieve(query="anything")
    assert out == []
    assert distiller.stats().retrieve_misses == 1
    assert distiller.stats().retrieve_hits == 0


# ---------------------------------------------------------------------------
# (18) retrieve skips stale sections
# ---------------------------------------------------------------------------


def test_retrieve_skips_stale_sections(tmp_path: Path, sink: FakeDistillSink) -> None:
    import time as _time

    pool = CognitivePool(root=tmp_path / "pool2", max_sections=50, default_ttl_s=10.0)
    now = _time.time()
    # Seed a section far in the past so it is stale under TTL=10.
    pool.upsert_section(title="archived", body="body", tags=("king",), now=1_000.0)
    # And one at "now" so it is still fresh when retrieve() calls read_all().
    pool.upsert_section(title="current", body="body", tags=("king",), now=now)
    d = L2Distiller(pool=pool, sink=sink)
    out = d.retrieve(query="king")
    titles = [s.title for s in out]
    assert "archived" not in titles
    assert "current" in titles


# ---------------------------------------------------------------------------
# (19) evolve history records only actual overwrites
# ---------------------------------------------------------------------------


def test_evolve_history_records_only_overwrites(
    distiller: L2Distiller, pool: CognitivePool
) -> None:
    # First insert — no record.
    distiller.evolve(DistillCandidate(title="t", body="body_a"))
    assert distiller.evolve_history() == []
    # Same body — still no record.
    distiller.evolve(DistillCandidate(title="t", body="body_a"))
    assert distiller.evolve_history() == []
    # Different body — one record.
    distiller.evolve(DistillCandidate(title="t", body="body_b"))
    assert len(distiller.evolve_history()) == 1
    # New unrelated title — still only one record.
    distiller.evolve(DistillCandidate(title="other", body="body_c"))
    assert len(distiller.evolve_history()) == 1


# ---------------------------------------------------------------------------
# (20) clear_history empties the log but not the stats
# ---------------------------------------------------------------------------


def test_clear_history_empties_list(
    distiller: L2Distiller, pool: CognitivePool
) -> None:
    _seed(pool, "t", "a")
    distiller.evolve(DistillCandidate(title="t", body="b"))
    assert len(distiller.evolve_history()) == 1
    distiller.clear_history()
    assert distiller.evolve_history() == []
    # Stats remain — they're lifetime counters.
    assert distiller.stats().evolutions == 1


# ---------------------------------------------------------------------------
# (21) L2Stats tracks every counter that matters
# ---------------------------------------------------------------------------


def test_stats_tracks_all_counters(
    distiller: L2Distiller, sink: FakeDistillSink, pool: CognitivePool
) -> None:
    # Distill once with one accepted candidate.
    sink.batches.append(
        DistillResult(
            candidates=[
                DistillCandidate(
                    title="alpha_topic",
                    body="body_a",
                    tags=("alpha",),
                    confidence=0.9,
                ),
                DistillCandidate(title="beta_topic", body="body_b", confidence=0.1),
            ]
        )
    )
    distiller.distill(query="query_alpha_topic", hits=[])
    # Retrieve a hit (long token matches) and a miss.
    distiller.retrieve(query="alpha_topic")
    distiller.retrieve(query="zzz_nothing_matches")

    stats = distiller.stats()
    assert stats.distill_calls == 1
    assert stats.candidates_accepted == 1
    assert stats.candidates_rejected == 1
    assert stats.sections_upserted == 1
    assert stats.retrieve_hits >= 1
    assert stats.retrieve_misses >= 1


# ---------------------------------------------------------------------------
# (22) Sink error is treated as a batch rejection
# ---------------------------------------------------------------------------


def test_distill_sink_error_rejects_all_candidates(
    distiller: L2Distiller, sink: FakeDistillSink, pool: CognitivePool
) -> None:
    sink.batches.append(
        DistillResult(
            candidates=[DistillCandidate(title="x", body="y", confidence=1.0)],
            error="sink: rate limited",
        )
    )
    out = distiller.distill(query="q", hits=[])
    assert out == []
    assert distiller.stats().candidates_rejected >= 1
    assert pool.read_section(title="x") is None


# ---------------------------------------------------------------------------
# (23) Existing sections are passed to the sink for tag-overlap queries
# ---------------------------------------------------------------------------


def test_distill_existing_sections_passed_to_sink(
    distiller: L2Distiller, sink: FakeDistillSink, pool: CognitivePool
) -> None:
    _seed(pool, "anchor_title", "existing body", tags=("anchor", "king"))
    sink.batches.append(DistillResult(candidates=[]))
    distiller.distill(query="tell me about the king anchor", hits=[])
    assert sink.last_request is not None
    existing_titles = [s.title for s in sink.last_request.existing_sections]
    assert "anchor_title" in existing_titles


# ---------------------------------------------------------------------------
# (24) Confidence boundary is inclusive
# ---------------------------------------------------------------------------


def test_min_confidence_boundary_accepted(
    distiller: L2Distiller, sink: FakeDistillSink, pool: CognitivePool
) -> None:
    sink.batches.append(
        DistillResult(
            candidates=[
                DistillCandidate(title="edge", body="body", confidence=0.3),
            ]
        )
    )
    out = distiller.distill(query="q", hits=[])
    assert len(out) == 1
    assert pool.read_section(title="edge") is not None


# ---------------------------------------------------------------------------
# Extras for surface coverage (public classes referenced in spec)
# ---------------------------------------------------------------------------


def test_distillation_failed_is_runtime_error() -> None:
    assert issubclass(DistillationFailed, RuntimeError)


def test_l2_stats_default_zero() -> None:
    s = L2Stats()
    assert s.distill_calls == 0
    assert s.candidates_accepted == 0
    assert s.candidates_rejected == 0
    assert s.sections_upserted == 0
    assert s.evolutions == 0
    assert s.retrieve_hits == 0
    assert s.retrieve_misses == 0


def test_evolve_record_is_dataclass() -> None:
    rec = EvolveRecord(
        section_title="t",
        old_body_hash="a" * 16,
        new_body_hash="b" * 16,
        reason="body_changed",
        timestamp=1.0,
    )
    assert rec.reason == "body_changed"
