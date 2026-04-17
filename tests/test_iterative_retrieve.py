"""Tests for concinno.agent.iterative_retrieve — three-layer cascade retrieval."""

from __future__ import annotations

from concinno.agent.iterative_retrieve import (
    CascadeConfig,
    IterativeRetriever,
)
from concinno.cache.cognitive_pool import CognitivePool
from concinno.cache.l2_distill import L2Distiller, RawHit

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeL1Retriever:
    """Controllable L1 retriever for tests."""

    def __init__(self, hits: list[RawHit] | None = None) -> None:
        self.hits = hits or []
        self.calls: list[tuple[str, int]] = []

    def retrieve(self, query: str, *, top_k: int = 5) -> list[RawHit]:
        self.calls.append((query, top_k))
        return list(self.hits)


class FakeEvolutionScheduler:
    """Controllable evolution scheduler for tests."""

    def __init__(self) -> None:
        self.scheduled: list[tuple[str, list[RawHit]]] = []

    def schedule(self, query: str, hits: list[RawHit]) -> None:
        self.scheduled.append((query, hits))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pool(tmp_path: object, sections: list[tuple[str, str, tuple[str, ...]]]) -> CognitivePool:
    """Create a CognitivePool with pre-populated sections.

    Each entry is (title, body, tags).
    """
    from pathlib import Path

    root = Path(str(tmp_path)) / "pool"
    root.mkdir(parents=True, exist_ok=True)
    pool = CognitivePool(root=root)
    for title, body, tags in sections:
        pool.upsert_section(title=title, body=body, tags=tags, now=1000.0)
    return pool


def _make_distiller(pool: CognitivePool) -> L2Distiller:
    """Create an L2Distiller backed by *pool* with no sink."""
    return L2Distiller(pool=pool, sink=None)


def _make_retriever(
    tmp_path: object,
    *,
    pool_sections: list[tuple[str, str, tuple[str, ...]]] | None = None,
    l1: FakeL1Retriever | None = None,
    scheduler: FakeEvolutionScheduler | None = None,
    config: CascadeConfig | None = None,
) -> IterativeRetriever:
    """Build a full IterativeRetriever wired to a tmp_path pool."""
    pool = _make_pool(tmp_path, pool_sections or [])
    distiller = _make_distiller(pool)
    return IterativeRetriever(
        pool=pool,
        distiller=distiller,
        l1=l1,
        scheduler=scheduler,
        config=config,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_l3_sufficient_skips_l2_l1(tmp_path: object) -> None:
    """When L3 has enough sections, L2 and L1 are not called."""
    l1 = FakeL1Retriever(hits=[RawHit(text="x", source="s", score=1.0)])
    r = _make_retriever(
        tmp_path,
        pool_sections=[("memory_budget", "notes on memory", ("memory",))],
        l1=l1,
    )
    result = r.retrieve("memory budget")
    assert result.l3_hit is True
    assert result.l2_hit is False
    assert result.l1_hit is False
    assert len(l1.calls) == 0


def test_l2_sufficient_skips_l1(tmp_path: object) -> None:
    """When L3 misses but L2 hits, L1 is not called."""
    # L3 won't match "xyz_topic" because query tokens don't overlap.
    # But L2 distiller.retrieve does its own keyword match, so we need
    # a section whose title/tags match the query for L2 to hit.
    l1 = FakeL1Retriever(hits=[RawHit(text="x", source="s", score=1.0)])
    pool_sections = [("deploy_config", "deploy info", ("deploy",))]
    # Use l3_min_sections=3 so L3 is insufficient with only 1 hit,
    # forcing the cascade to try L2 (which hits with 1 >= l2_min=1).
    retriever = _make_retriever(
        tmp_path,
        pool_sections=pool_sections,
        l1=l1,
        config=CascadeConfig(l3_min_sections=3, l2_min_sections=1),
    )
    result = retriever.retrieve("deploy config")
    assert result.l3_hit is False
    assert result.l2_hit is True
    assert result.l1_hit is False
    assert len(l1.calls) == 0


def test_l1_fallthrough_when_l3_l2_empty(tmp_path: object) -> None:
    """When L3 and L2 are empty, L1 is called."""
    l1 = FakeL1Retriever(hits=[RawHit(text="found", source="s", score=0.9)])
    r = _make_retriever(tmp_path, l1=l1)
    result = r.retrieve("something obscure")
    assert result.l3_hit is False
    assert result.l2_hit is False
    assert result.l1_hit is True
    assert len(result.raw_hits) == 1


def test_always_check_l1_runs_even_when_l3_hit(tmp_path: object) -> None:
    """With always_check_l1=True, L1 runs even when L3 is sufficient."""
    l1 = FakeL1Retriever(hits=[RawHit(text="extra", source="s", score=0.5)])
    r = _make_retriever(
        tmp_path,
        pool_sections=[("session_notes", "my notes", ("session",))],
        l1=l1,
        config=CascadeConfig(always_check_l1=True),
    )
    result = r.retrieve("session notes")
    assert result.l3_hit is True
    assert result.l1_hit is True
    assert len(l1.calls) == 1


def test_evolution_scheduled_on_l1_hits(tmp_path: object) -> None:
    """When L1 hits and scheduler exists, evolution is scheduled."""
    hits = [RawHit(text="new fact", source="web", score=0.8)]
    l1 = FakeL1Retriever(hits=hits)
    sched = FakeEvolutionScheduler()
    r = _make_retriever(tmp_path, l1=l1, scheduler=sched)
    result = r.retrieve("unknown topic")
    assert result.evolution_scheduled is True
    assert len(sched.scheduled) == 1
    assert sched.scheduled[0][0] == "unknown topic"


def test_no_scheduler_skips_evolution(tmp_path: object) -> None:
    """Without a scheduler, evolution_scheduled stays False."""
    l1 = FakeL1Retriever(hits=[RawHit(text="x", source="s", score=1.0)])
    r = _make_retriever(tmp_path, l1=l1, scheduler=None)
    result = r.retrieve("query here")
    assert result.l1_hit is True
    assert result.evolution_scheduled is False


def test_no_l1_skips_l1_fallthrough(tmp_path: object) -> None:
    """Without an L1 retriever, no L1 fallthrough occurs."""
    r = _make_retriever(tmp_path, l1=None)
    result = r.retrieve("anything")
    assert result.l1_hit is False
    assert result.source_layer == "none"


def test_source_layer_l3_only(tmp_path: object) -> None:
    r = _make_retriever(
        tmp_path,
        pool_sections=[("debug_log", "logs", ("debug",))],
    )
    result = r.retrieve("debug log")
    assert result.source_layer == "L3"


def test_source_layer_l2_only(tmp_path: object) -> None:
    r = _make_retriever(
        tmp_path,
        pool_sections=[("cache_policy", "policy", ("cache",))],
        config=CascadeConfig(l3_min_sections=5, l2_min_sections=1),
    )
    result = r.retrieve("cache policy")
    assert result.source_layer == "L2"


def test_source_layer_l1_only(tmp_path: object) -> None:
    l1 = FakeL1Retriever(hits=[RawHit(text="x", source="s", score=1.0)])
    r = _make_retriever(tmp_path, l1=l1)
    result = r.retrieve("totally new")
    assert result.source_layer == "L1"


def test_source_layer_l3_plus_l1(tmp_path: object) -> None:
    l1 = FakeL1Retriever(hits=[RawHit(text="extra", source="s", score=0.5)])
    r = _make_retriever(
        tmp_path,
        pool_sections=[("routing_table", "routes", ("routing",))],
        l1=l1,
        config=CascadeConfig(always_check_l1=True),
    )
    result = r.retrieve("routing table")
    assert result.source_layer == "L3+L1"


def test_dedup_by_title_prefers_l3(tmp_path: object) -> None:
    """When L3 and L2 return sections with the same title, L3 wins."""
    from pathlib import Path

    root = Path(str(tmp_path)) / "dedup_pool"
    root.mkdir(parents=True, exist_ok=True)
    pool = CognitivePool(root=root)
    pool.upsert_section(title="shared_topic", body="L3 version", tags=("shared",), now=1000.0)
    distiller = L2Distiller(pool=pool, sink=None)

    retriever = IterativeRetriever(
        pool=pool,
        distiller=distiller,
        config=CascadeConfig(l3_min_sections=5, l2_min_sections=1, always_check_l1=False),
    )
    # Both L3 and L2 will find "shared_topic" but L3 threshold is 5
    # so L3 is insufficient → L2 is tried and hits. Dedup means only
    # one copy in the result.
    result = retriever.retrieve("shared topic")
    titles = [s.title for s in result.sections]
    assert titles.count("shared_topic") == 1


def test_stats_tracks_layer_counts(tmp_path: object) -> None:
    l1 = FakeL1Retriever(hits=[RawHit(text="x", source="s", score=1.0)])
    sched = FakeEvolutionScheduler()
    r = _make_retriever(tmp_path, l1=l1, scheduler=sched)
    # First query: L1 fallthrough + evolution
    r.retrieve("unknown query")
    s = r.stats()
    assert s.queries == 1
    assert s.l1_fallthrough == 1
    assert s.evolutions_scheduled == 1
    assert s.l3_sufficient == 0
    assert s.l2_sufficient == 0


def test_reset_stats_clears(tmp_path: object) -> None:
    l1 = FakeL1Retriever(hits=[RawHit(text="x", source="s", score=1.0)])
    r = _make_retriever(tmp_path, l1=l1)
    r.retrieve("query")
    r.reset_stats()
    s = r.stats()
    assert s.queries == 0
    assert s.l1_fallthrough == 0


def test_config_min_sections_threshold(tmp_path: object) -> None:
    """L3 with 1 hit is insufficient when l3_min_sections=2."""
    l1 = FakeL1Retriever(hits=[RawHit(text="x", source="s", score=1.0)])
    r = _make_retriever(
        tmp_path,
        pool_sections=[("one_topic", "body", ("topic",))],
        l1=l1,
        config=CascadeConfig(l3_min_sections=2, l2_min_sections=2),
    )
    result = r.retrieve("one topic")
    assert result.l3_hit is False
    # L2 also returns 1 hit, below threshold of 2
    assert result.l2_hit is False
    assert result.l1_hit is True


def test_empty_query_returns_empty(tmp_path: object) -> None:
    """An empty query produces no keyword tokens → no L3/L2 hits."""
    r = _make_retriever(
        tmp_path,
        pool_sections=[("some_data", "data", ("some",))],
    )
    result = r.retrieve("")
    assert result.l3_hit is False
    assert result.l2_hit is False
    assert len(result.sections) == 0


def test_cascade_with_all_layers_populated(tmp_path: object) -> None:
    """When always_check_l1 is True, all three layers contribute."""
    l1 = FakeL1Retriever(hits=[RawHit(text="l1 data", source="s", score=0.7)])
    sched = FakeEvolutionScheduler()
    r = _make_retriever(
        tmp_path,
        pool_sections=[("alpha_config", "alpha", ("alpha",))],
        l1=l1,
        scheduler=sched,
        config=CascadeConfig(always_check_l1=True),
    )
    result = r.retrieve("alpha config")
    assert result.l3_hit is True
    assert result.l1_hit is True
    assert result.evolution_scheduled is True
    assert len(result.sections) >= 1
    assert len(result.raw_hits) == 1


def test_l1_top_k_passed_through(tmp_path: object) -> None:
    """The l1_top_k config value is passed to L1.retrieve."""
    l1 = FakeL1Retriever()
    r = _make_retriever(
        tmp_path,
        l1=l1,
        config=CascadeConfig(l1_top_k=42),
    )
    r.retrieve("any query")
    assert len(l1.calls) == 1
    assert l1.calls[0][1] == 42


def test_schedule_evolution_false_skips(tmp_path: object) -> None:
    """With schedule_evolution=False, no evolution even with L1 hits."""
    l1 = FakeL1Retriever(hits=[RawHit(text="x", source="s", score=1.0)])
    sched = FakeEvolutionScheduler()
    r = _make_retriever(
        tmp_path,
        l1=l1,
        scheduler=sched,
        config=CascadeConfig(schedule_evolution=False),
    )
    result = r.retrieve("query")
    assert result.l1_hit is True
    assert result.evolution_scheduled is False
    assert len(sched.scheduled) == 0


def test_result_dataclass_fields(tmp_path: object) -> None:
    """RetrievalResult has all expected fields."""
    r = _make_retriever(tmp_path)
    result = r.retrieve("test")
    assert hasattr(result, "sections")
    assert hasattr(result, "raw_hits")
    assert hasattr(result, "source_layer")
    assert hasattr(result, "l3_hit")
    assert hasattr(result, "l2_hit")
    assert hasattr(result, "l1_hit")
    assert hasattr(result, "evolution_scheduled")
    assert isinstance(result.sections, list)
    assert isinstance(result.raw_hits, list)
    assert isinstance(result.source_layer, str)
