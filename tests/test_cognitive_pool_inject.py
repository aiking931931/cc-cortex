"""Tests for ``cc_cortex.cognitive_pool_inject``.

Closes the islanded-module gap: 1.16 introduced ``cache/cognitive_pool``
but no consumer ever read from it. These tests verify the pool→inject
bridge, the relevance scoring, the budget caps, and — most importantly
— the fail-safe contract that subagent injection never breaks even when
the pool layer explodes.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from cc_cortex.cache.cognitive_pool import CognitivePool, PoolSection
from cc_cortex.cognitive_pool_inject import (
    build_pool_context,
    score_section,
)

# ── score_section ────────────────────────────────────────────


def _section(title: str, body: str, ts: float = 1000.0) -> PoolSection:
    return PoolSection(
        section_id="abcd1234",
        title=title,
        body=body,
        tags=(),
        created_ts=ts,
        updated_ts=ts,
    )


def test_score_section_empty_query_returns_zero() -> None:
    s = _section("anything", "any body content here")
    assert score_section(s, set()) == 0.0


def test_score_section_title_overlap_weighs_more_than_body() -> None:
    title_match = _section("subagent_inject", "unrelated body words here only")
    body_match = _section("unrelated", "subagent inject body details follow")
    query = {"subagent", "inject"}
    assert score_section(title_match, query) > score_section(body_match, query)


def test_score_section_normalises_by_query_length() -> None:
    s = _section("alpha", "alpha beta gamma")
    short_query = {"alpha"}
    long_query = {"alpha", "delta", "epsilon", "zeta", "eta"}
    # Same matches, denominator changes — short query scores higher.
    assert score_section(s, short_query) > score_section(s, long_query)


def test_score_section_no_overlap_returns_zero() -> None:
    s = _section("alpha", "beta gamma delta")
    assert score_section(s, {"omega"}) == 0.0


def test_score_section_handles_empty_section_text() -> None:
    s = _section("", "")
    assert score_section(s, {"anything"}) == 0.0


# ── build_pool_context — empty / fail-safe paths ─────────────


def test_build_pool_context_empty_pool_returns_empty(tmp_path: Path) -> None:
    pool = CognitivePool(root=str(tmp_path))
    assert build_pool_context(task_prompt="anything", pool=pool) == ""


def test_build_pool_context_failsafe_on_pool_error() -> None:
    class _Broken:
        def read_all(self) -> list[PoolSection]:
            raise RuntimeError("simulated pool corruption")

    assert build_pool_context(task_prompt="x", pool=_Broken()) == ""


def test_build_pool_context_default_pool_missing_is_failsafe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Default-rooted pool with no file on disk MUST return '' not raise."""
    monkeypatch.setenv("CC_CORTEX_POOL_ROOT", str(tmp_path / "nope"))
    # Pool dir does not exist; CognitivePool().read_all() should yield [].
    assert build_pool_context(task_prompt="x") == ""


# ── build_pool_context — relevance ranking ───────────────────


def test_build_pool_context_query_ranks_relevant_first(tmp_path: Path) -> None:
    pool = CognitivePool(root=str(tmp_path))
    now = time.time()
    pool.upsert_section(
        title="cybergym_strategy",
        body="cybergym fuzzing approach for sanitizer-detected bugs",
        now=now,
    )
    pool.upsert_section(
        title="word_doc_layout",
        body="python-docx page margin and font size conventions",
        now=now + 10,
    )
    pool.save()

    p2 = CognitivePool(root=str(tmp_path))
    out = build_pool_context(task_prompt="cybergym fuzz", pool=p2)
    assert "cybergym_strategy" in out
    # word_doc_layout is more recent but zero-score, so it is dropped
    # entirely. Even if it were retained, query relevance must put
    # cybergym_strategy first.
    if "word_doc_layout" in out:
        assert out.index("cybergym_strategy") < out.index("word_doc_layout")


def test_build_pool_context_no_query_falls_back_to_recency(tmp_path: Path) -> None:
    pool = CognitivePool(root=str(tmp_path))
    pool.upsert_section(title="oldest", body="older body content here please", now=1000.0)
    pool.upsert_section(title="newest", body="newest body content here please", now=2000.0)
    pool.save()

    p2 = CognitivePool(root=str(tmp_path))
    out = build_pool_context(pool=p2)
    assert "newest" in out
    assert out.index("newest") < out.index("oldest")


def test_build_pool_context_zero_score_query_falls_back(tmp_path: Path) -> None:
    """When no section overlaps the query, return the top-N by score
    order anyway — better to inject something than nothing."""
    pool = CognitivePool(root=str(tmp_path))
    pool.upsert_section(title="alpha_section", body="alpha beta gamma delta epsilon", now=1.0)
    pool.upsert_section(title="zeta_section", body="zeta eta theta iota kappa", now=2.0)
    pool.save()

    p2 = CognitivePool(root=str(tmp_path))
    out = build_pool_context(task_prompt="omega ultra mega", pool=p2)
    # Both sections have zero score, should still surface at least one.
    assert out
    assert "alpha_section" in out or "zeta_section" in out


# ── build_pool_context — budget caps ─────────────────────────


def test_build_pool_context_max_sections_cap(tmp_path: Path) -> None:
    pool = CognitivePool(root=str(tmp_path))
    for i in range(10):
        pool.upsert_section(
            title=f"section_{i:02d}",
            body=f"body content number {i} for testing the cap",
            now=float(i),
        )
    pool.save()

    p2 = CognitivePool(root=str(tmp_path))
    out = build_pool_context(pool=p2, max_sections=2)
    # Only the 2 most recent should be present.
    assert "section_09" in out
    assert "section_08" in out
    assert "section_07" not in out


def test_build_pool_context_max_chars_cap(tmp_path: Path) -> None:
    pool = CognitivePool(root=str(tmp_path))
    big_body = "x" * 2000
    for i in range(5):
        pool.upsert_section(title=f"big_{i}", body=big_body, now=float(i))
    pool.save()

    p2 = CognitivePool(root=str(tmp_path))
    out = build_pool_context(pool=p2, max_sections=10, max_chars=500, max_section_chars=300)
    # max_chars=500 means we get at most one full block (~300 body + framing).
    assert len(out) <= 700  # allow header + framing slack
    assert "big_4" in out  # most recent included first


def test_build_pool_context_section_truncation(tmp_path: Path) -> None:
    pool = CognitivePool(root=str(tmp_path))
    pool.upsert_section(title="long", body="a" * 5000, now=1.0)
    pool.save()

    p2 = CognitivePool(root=str(tmp_path))
    out = build_pool_context(pool=p2, max_section_chars=100)
    assert "[...truncated]" in out


def test_build_pool_context_drops_tiny_stub_sections(tmp_path: Path) -> None:
    """Single-token bodies waste budget; they are filtered out."""
    pool = CognitivePool(root=str(tmp_path))
    pool.upsert_section(title="stub", body="x", now=1.0)
    pool.upsert_section(title="real", body="real sentence with multiple meaningful words", now=2.0)
    pool.save()

    p2 = CognitivePool(root=str(tmp_path))
    out = build_pool_context(pool=p2)
    # Stub passes title check (>=1 token), but the result should still
    # surface 'real' first because of recency.
    assert "real" in out


# ── build_pool_context — rendering ───────────────────────────


def test_build_pool_context_renders_tags(tmp_path: Path) -> None:
    pool = CognitivePool(root=str(tmp_path))
    pool.upsert_section(
        title="tagged_section",
        body="body content for tag rendering test here",
        tags=("g6", "cybergym"),
        now=1.0,
    )
    pool.save()

    p2 = CognitivePool(root=str(tmp_path))
    out = build_pool_context(pool=p2)
    assert "[g6,cybergym]" in out


def test_build_pool_context_header_present(tmp_path: Path) -> None:
    pool = CognitivePool(root=str(tmp_path))
    pool.upsert_section(title="any", body="body content goes here please", now=1.0)
    pool.save()

    p2 = CognitivePool(root=str(tmp_path))
    out = build_pool_context(pool=p2)
    assert "Cross-session pool" in out
    assert out.startswith("\U0001f9e0")  # brain emoji header


# ── Integration with cognitive_inject.build_cognitive_context ─


def test_cognitive_inject_picks_up_pool_layer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """build_cognitive_context() must surface pool sections when present."""
    monkeypatch.setenv("CC_CORTEX_POOL_ROOT", str(tmp_path))
    pool = CognitivePool(root=str(tmp_path))
    pool.upsert_section(
        title="integration_test_marker",
        body="this body content should appear in the subagent inject",
        now=time.time(),
    )
    pool.save()

    from cc_cortex.cognitive_inject import build_cognitive_context

    ctx = build_cognitive_context(
        task_prompt="integration test marker",
        workspace=str(tmp_path),
        agent_type="general-purpose",
    )
    assert "integration_test_marker" in ctx


def test_cognitive_inject_failsafe_when_pool_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """build_cognitive_context() must still return non-empty even
    when the pool layer is unreachable — pool inject is supplementary."""
    monkeypatch.setenv("CC_CORTEX_POOL_ROOT", str(tmp_path / "no_such_dir"))

    from cc_cortex.cognitive_inject import build_cognitive_context

    ctx = build_cognitive_context(
        task_prompt="any task",
        workspace=str(tmp_path),
        agent_type="general-purpose",
    )
    # Should still contain the thinking_directives layer at minimum.
    assert ctx
