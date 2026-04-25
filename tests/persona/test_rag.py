"""Tests for the per-persona BM25-ish retriever."""

from __future__ import annotations

from concinno.persona.rag import PersonaRAG


def test_empty_rag_returns_empty_search() -> None:
    r = PersonaRAG()
    assert r.search("anything") == []


def test_add_then_search() -> None:
    r = PersonaRAG("alice")
    r.add("the user is named Bob")
    r.add("the user loves jazz piano")
    r.add("today is Tuesday")
    hits = r.search("Bob")
    assert hits
    assert "Bob" in hits[0].text


def test_add_turn_combines_user_and_reply() -> None:
    r = PersonaRAG()
    r.add_turn("what is your favorite color", "I like teal")
    hits = r.search("teal")
    assert hits
    assert "teal" in hits[0].text


def test_top_k_respected() -> None:
    r = PersonaRAG()
    for i in range(10):
        r.add(f"document about cats number {i}")
    hits = r.search("cats", top_k=3)
    assert len(hits) == 3


def test_unrelated_query_returns_empty() -> None:
    r = PersonaRAG()
    r.add("apple banana cherry")
    hits = r.search("xyzzy")
    assert hits == []


def test_reset_clears_index() -> None:
    r = PersonaRAG()
    r.add("one")
    r.add("two")
    assert len(r) == 2
    r.reset()
    assert len(r) == 0
    assert r.search("one") == []


def test_empty_text_skipped() -> None:
    r = PersonaRAG()
    r.add("")
    r.add("   ")
    assert len(r) == 0
