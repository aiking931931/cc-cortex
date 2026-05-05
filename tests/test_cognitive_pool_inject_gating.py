"""Regression tests for F3 (2.7.1): cognitive_pool_inject Self-RAG gating.

Before 2.7.1, when the query produced zero positive-scoring sections
``build_pool_context`` fell through to a recency ranking that injected
whatever cross-session chatter happened to be in the pool. This violates
Self-RAG: don't retrieve when you have no signal. The new behaviour
gates the retrieval — empty score set returns ``""``.

Empty queries (``task_prompt=""``) still fall back to pure recency so
callers that deliberately ask for "whatever's fresh" still work.
"""

from __future__ import annotations

import pytest

from concinno.cache import ux_gate
from concinno.cache.cognitive_pool import CognitivePool
from concinno.cognitive_pool_inject import build_pool_context


@pytest.fixture(autouse=True)
def _enable_ux(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Force ux_injection on so the F3 logic is reachable."""
    monkeypatch.setenv("CONCINNO_UX_INJECTION", "1")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    ux_gate.reset_cache()
    yield
    ux_gate.reset_cache()


def test_unrelated_query_returns_empty(tmp_path) -> None:
    """Query with zero positive score → gate returns ''."""
    pool = CognitivePool(root=tmp_path)
    pool.upsert_section(title="unrelated_topic", body="completely different content")
    # Query tokens won't overlap with title or body.
    result = build_pool_context(
        task_prompt="quantum entanglement of dragons",
        pool=pool,
    )
    assert result == ""


def test_empty_query_uses_recency_fallback(tmp_path) -> None:
    """Explicit empty query → recency fallback still works."""
    pool = CognitivePool(root=tmp_path)
    pool.upsert_section(title="topic_a", body="old content aaa bbb")
    pool.upsert_section(title="topic_b", body="new content ccc ddd")
    result = build_pool_context(task_prompt="", pool=pool)
    # Non-empty: recency fallback fired.
    assert result
    assert "topic_a" in result or "topic_b" in result


def test_matching_query_returns_content(tmp_path) -> None:
    """Query that hits title → positive score → section included."""
    pool = CognitivePool(root=tmp_path)
    pool.upsert_section(title="release_plan", body="ship version 2.7.1")
    result = build_pool_context(task_prompt="release 2.7.1", pool=pool)
    assert "release" in result.lower()
