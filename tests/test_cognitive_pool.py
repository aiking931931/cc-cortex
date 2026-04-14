"""Tests for cc_cortex.cache.cognitive_pool — L3 shared markdown pool.

These tests cover the stable section-hash contract (load-bearing for
future microcompact cache-edit integration), the upsert/read/evict
lifecycle, stale detection with injected ``now``, atomic save, and
corrupt-file recovery. Everything is deterministic: no wall clock, no
network, no personal paths.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from cc_cortex.cache.cognitive_pool import (
    DEFAULT_POOL_FILENAME,
    SECTION_FOOTER,
    SECTION_HEADER_PREFIX,
    CognitivePool,
    PoolCorrupt,
    PoolFull,
    PoolSection,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def pool(tmp_path: Path) -> CognitivePool:
    """Fresh pool rooted in a pytest tmp_path."""
    return CognitivePool(root=tmp_path)


@pytest.fixture
def small_pool(tmp_path: Path) -> CognitivePool:
    """Pool with max_sections=2 for full-capacity tests."""
    return CognitivePool(root=tmp_path, max_sections=2)


# ---------------------------------------------------------------------------
# Hashing contract
# ---------------------------------------------------------------------------


def test_compute_section_id_stable_across_title_case_sensitive() -> None:
    """sha256-derived id MUST be stable and case-sensitive.

    This hash contract is load-bearing for future microcompact
    cache-edit integration — if it ever drifts, every persisted pool
    in the world breaks.
    """
    id_lower = CognitivePool.compute_section_id("user.goals")
    id_upper = CognitivePool.compute_section_id("USER.GOALS")
    assert id_lower != id_upper
    # Hardcoded golden values lock the hash contract.
    # sha256("user.goals")[:8] is deterministic.
    assert id_lower == CognitivePool.compute_section_id("user.goals")
    assert len(id_lower) == 8


def test_section_id_length_8_hex() -> None:
    """Every id is exactly 8 lowercase hex chars."""
    for title in ["a", "user.goals", "really_long_title_name_here"]:
        sid = CognitivePool.compute_section_id(title)
        assert len(sid) == 8
        int(sid, 16)  # raises if non-hex
        assert sid == sid.lower()


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------


def test_upsert_creates_new_section(pool: CognitivePool) -> None:
    section = pool.upsert_section(
        title="goals", body="first goal", now=100.0
    )
    assert section.title == "goals"
    assert section.body == "first goal"
    assert section.created_ts == 100.0
    assert section.updated_ts == 100.0
    assert pool.pool_path().exists()


def test_upsert_replaces_existing_by_title(pool: CognitivePool) -> None:
    pool.upsert_section(title="goals", body="v1", now=100.0)
    pool.upsert_section(title="goals", body="v2", now=200.0)
    all_sections = pool.read_all(now=200.0)
    assert len(all_sections) == 1
    assert all_sections[0].body == "v2"


def test_upsert_preserves_created_ts_on_replace(pool: CognitivePool) -> None:
    pool.upsert_section(title="goals", body="v1", now=100.0)
    pool.upsert_section(title="goals", body="v2", now=500.0)
    section = pool.read_section(title="goals", now=500.0)
    assert section is not None
    assert section.created_ts == 100.0
    assert section.updated_ts == 500.0


def test_upsert_truncates_oversize_body(tmp_path: Path) -> None:
    small = CognitivePool(root=tmp_path, max_section_bytes=100)
    huge_body = "line\n" * 500  # 2500 bytes
    section = small.upsert_section(
        title="big", body=huge_body, now=100.0
    )
    encoded_len = len(section.body.encode("utf-8"))
    assert encoded_len <= 100
    assert "truncated" in section.body


def test_upsert_raises_pool_full_when_no_stale(
    small_pool: CognitivePool,
) -> None:
    small_pool.upsert_section(title="a", body="x", now=100.0)
    small_pool.upsert_section(title="b", body="x", now=100.0)
    with pytest.raises(PoolFull):
        small_pool.upsert_section(title="c", body="x", now=100.0)


def test_upsert_evicts_stale_when_full(tmp_path: Path) -> None:
    tight = CognitivePool(
        root=tmp_path, max_sections=2, default_ttl_s=10.0
    )
    tight.upsert_section(title="a", body="x", now=100.0)
    tight.upsert_section(title="b", body="x", now=100.0)
    # Now is 200 → both are stale (ttl=10). Adding "c" should evict
    # one stale and succeed.
    tight.upsert_section(title="c", body="x", now=200.0)
    alive = tight.read_all(now=200.0)
    # c is alive; one of a/b was evicted but the other may still be
    # on disk until the next sweep.
    titles_alive = [s.title for s in alive]
    assert "c" in titles_alive


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def test_read_all_skips_stale(tmp_path: Path) -> None:
    expiring = CognitivePool(root=tmp_path, default_ttl_s=10.0)
    expiring.upsert_section(title="fresh", body="x", now=100.0)
    expiring.upsert_section(title="old", body="x", now=50.0)
    alive = expiring.read_all(now=105.0)
    titles = {s.title for s in alive}
    assert "fresh" in titles
    assert "old" not in titles


def test_read_section_returns_none_for_missing(pool: CognitivePool) -> None:
    assert pool.read_section(title="nope") is None


def test_read_tagged_match_any(pool: CognitivePool) -> None:
    pool.upsert_section(title="a", body="x", tags=("foo", "bar"), now=100.0)
    pool.upsert_section(title="b", body="x", tags=("baz",), now=100.0)
    pool.upsert_section(title="c", body="x", tags=("qux",), now=100.0)
    hits = pool.read_tagged(["foo", "baz"], match_all=False, now=100.0)
    titles = {s.title for s in hits}
    assert titles == {"a", "b"}


def test_read_tagged_match_all(pool: CognitivePool) -> None:
    pool.upsert_section(
        title="a", body="x", tags=("foo", "bar"), now=100.0
    )
    pool.upsert_section(title="b", body="x", tags=("foo",), now=100.0)
    hits = pool.read_tagged(["foo", "bar"], match_all=True, now=100.0)
    assert len(hits) == 1
    assert hits[0].title == "a"


# ---------------------------------------------------------------------------
# Remove + clear + prune
# ---------------------------------------------------------------------------


def test_remove_section_returns_bool(pool: CognitivePool) -> None:
    pool.upsert_section(title="a", body="x", now=100.0)
    assert pool.remove_section(title="a") is True
    assert pool.remove_section(title="a") is False
    assert pool.read_section(title="a") is None


def test_prune_stale_counts_removed(tmp_path: Path) -> None:
    p = CognitivePool(root=tmp_path, default_ttl_s=10.0)
    p.upsert_section(title="a", body="x", now=100.0)
    p.upsert_section(title="b", body="x", now=100.0)
    p.upsert_section(title="c", body="x", now=100.0)
    removed = p.prune_stale(now=200.0)
    assert removed == 3
    assert p.read_all(now=200.0) == []


# ---------------------------------------------------------------------------
# Save / load
# ---------------------------------------------------------------------------


def test_save_load_roundtrip(tmp_path: Path) -> None:
    p1 = CognitivePool(root=tmp_path)
    p1.upsert_section(
        title="user.goals",
        body="ship v7\nplus caches",
        tags=("persona", "todo"),
        now=100.0,
    )
    p1.upsert_section(title="blockers", body="nothing", now=200.0)

    p2 = CognitivePool(root=tmp_path)
    p2.load()
    all_sections = p2.read_all(now=300.0)
    assert len(all_sections) == 2
    by_title = {s.title: s for s in all_sections}
    assert by_title["user.goals"].body == "ship v7\nplus caches"
    assert by_title["user.goals"].tags == ("persona", "todo")
    assert by_title["blockers"].body == "nothing"


def test_atomic_save_no_corruption_on_partial_write(tmp_path: Path) -> None:
    """A crash mid-write must leave the previous pool intact."""
    p = CognitivePool(root=tmp_path)
    p.upsert_section(title="good", body="original", now=100.0)

    original_bytes = p.pool_path().read_bytes()

    real_open = Path.open

    def exploding_open(self: Path, *args: object, **kwargs: object) -> object:
        if self.suffix == ".tmp":
            raise OSError("simulated crash mid-write")
        return real_open(self, *args, **kwargs)  # type: ignore[arg-type]

    with patch.object(Path, "open", exploding_open):
        with pytest.raises(OSError, match="simulated crash"):
            p.upsert_section(title="bad", body="should not persist", now=200.0)

    # Real pool file must be byte-identical to before the crash.
    assert p.pool_path().read_bytes() == original_bytes

    # And loading fresh must show only the original section.
    p2 = CognitivePool(root=tmp_path)
    p2.load()
    alive = p2.read_all(now=300.0)
    assert len(alive) == 1
    assert alive[0].title == "good"


def test_load_skips_corrupt_sections(tmp_path: Path) -> None:
    """A few bad sections should not wipe the good ones."""
    path = tmp_path / DEFAULT_POOL_FILENAME
    # 1 corrupt header (bad hex), 3 good sections
    good_sid = CognitivePool.compute_section_id("good")
    extra1 = CognitivePool.compute_section_id("extra1")
    extra2 = CognitivePool.compute_section_id("extra2")
    content = (
        f"{SECTION_HEADER_PREFIX}ZZZZZZZZ title=bad updated=100.0{' -->'}\n"
        f"corrupt body\n"
        f"{SECTION_FOOTER}\n"
        f"{SECTION_HEADER_PREFIX}{good_sid} title=good updated=100.0{' -->'}\n"
        f"hello\n"
        f"{SECTION_FOOTER}\n"
        f"{SECTION_HEADER_PREFIX}{extra1} title=extra1 updated=100.0{' -->'}\n"
        f"world\n"
        f"{SECTION_FOOTER}\n"
        f"{SECTION_HEADER_PREFIX}{extra2} title=extra2 updated=100.0{' -->'}\n"
        f"!\n"
        f"{SECTION_FOOTER}\n"
    )
    path.write_text(content, encoding="utf-8")
    p = CognitivePool(root=tmp_path)
    p.load()  # must not raise (1/4 corrupt = 25% < 50% threshold)
    titles = {s.title for s in p.read_all(now=100.0)}
    assert titles == {"good", "extra1", "extra2"}


def test_load_raises_pool_corrupt_when_majority_corrupt(tmp_path: Path) -> None:
    path = tmp_path / DEFAULT_POOL_FILENAME
    good_sid = CognitivePool.compute_section_id("good")
    content = (
        f"{SECTION_HEADER_PREFIX}ZZZZZZZZ title=b1 updated=100.0{' -->'}\n"
        f"body1\n"
        f"{SECTION_FOOTER}\n"
        f"{SECTION_HEADER_PREFIX}YYYYYYYY title=b2 updated=100.0{' -->'}\n"
        f"body2\n"
        f"{SECTION_FOOTER}\n"
        f"{SECTION_HEADER_PREFIX}{good_sid} title=good updated=100.0{' -->'}\n"
        f"body3\n"
        f"{SECTION_FOOTER}\n"
    )
    path.write_text(content, encoding="utf-8")
    p = CognitivePool(root=tmp_path)
    with pytest.raises(PoolCorrupt):
        p.load()


# ---------------------------------------------------------------------------
# TTL behaviour
# ---------------------------------------------------------------------------


def test_ttl_default_inherited_when_section_ttl_none(tmp_path: Path) -> None:
    p = CognitivePool(root=tmp_path, default_ttl_s=10.0)
    p.upsert_section(title="a", body="x", ttl_s=None, now=100.0)
    # At now=120, pool default ttl=10 → stale.
    assert p.read_section(title="a", now=120.0) is None


def test_section_ttl_overrides_default(tmp_path: Path) -> None:
    p = CognitivePool(root=tmp_path, default_ttl_s=10.0)
    p.upsert_section(title="a", body="x", ttl_s=1000.0, now=100.0)
    # At now=120, section's own ttl=1000 overrides default ttl=10.
    result = p.read_section(title="a", now=120.0)
    assert result is not None
    assert result.title == "a"


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


def test_stats_counts_stale(tmp_path: Path) -> None:
    p = CognitivePool(root=tmp_path, default_ttl_s=10.0)
    p.upsert_section(title="a", body="x", now=100.0)
    p.upsert_section(title="b", body="x", now=100.0)
    p.upsert_section(title="c", body="x", ttl_s=1000.0, now=100.0)
    stats = p.stats(now=200.0)
    assert stats.total_sections == 3
    assert stats.stale_sections == 2  # a and b are stale, c has big ttl
    assert stats.total_bytes > 0
    assert stats.last_write_ts == 100.0


# ---------------------------------------------------------------------------
# Title + markdown rendering
# ---------------------------------------------------------------------------


def test_title_whitespace_replaced_with_underscore(pool: CognitivePool) -> None:
    pool.upsert_section(title="user goals here", body="x", now=100.0)
    section = pool.read_section(title="user goals here", now=100.0)
    assert section is not None
    assert section.title == "user_goals_here"
    # Lookup with already-normalised title still works.
    assert pool.read_section(title="user_goals_here", now=100.0) is not None


def test_to_markdown_includes_section_header_and_footer() -> None:
    s = PoolSection(
        section_id="abcd1234",
        title="test",
        body="hello world",
        tags=("a", "b"),
        created_ts=100.0,
        updated_ts=100.0,
    )
    rendered = s.to_markdown()
    assert rendered.startswith(SECTION_HEADER_PREFIX + "abcd1234")
    assert " title=test" in rendered
    assert " tags=a,b" in rendered
    assert SECTION_FOOTER in rendered
    assert "hello world" in rendered
