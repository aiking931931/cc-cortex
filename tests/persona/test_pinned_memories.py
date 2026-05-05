"""Tests for the pinned-memory store."""

from __future__ import annotations

from concinno.persona.pinned_memories import PinnedMemoryStore
from concinno.persona.schema import PinnedMemory


def test_pin_then_search_finds_match() -> None:
    s = PinnedMemoryStore()
    s.pin("user is Bob", reason="intro")
    hits = s.search("Bob")
    assert any(h.content == "user is Bob" for h in hits)


def test_pin_idempotent() -> None:
    s = PinnedMemoryStore()
    a = s.pin("X")
    b = s.pin("X")
    assert a.content == b.content
    assert len(s) == 1


def test_unpin_removes_entry() -> None:
    s = PinnedMemoryStore()
    s.pin("X")
    assert s.unpin("X") is True
    assert s.unpin("X") is False  # second call is no-op
    assert len(s) == 0


def test_initial_population() -> None:
    s = PinnedMemoryStore(
        initial=[
            PinnedMemory(content="A", pinned_at="2026-04-25T00:00:00Z"),
            PinnedMemory(content="B", pinned_at="2026-04-25T00:00:01Z"),
        ]
    )
    assert len(s) == 2
    assert s.contains("A")
    assert s.contains("B")


def test_search_falls_back_to_recent() -> None:
    s = PinnedMemoryStore()
    s.pin("first")
    s.pin("second")
    s.pin("third")
    hits = s.search("nothing matches", top_k=2)
    # Falls back to most-recent pins so identity anchors stay in prompt.
    assert hits


def test_iter_returns_all_in_order() -> None:
    s = PinnedMemoryStore()
    s.pin("alpha")
    s.pin("beta")
    items = list(s)
    assert items[0].content == "alpha"
    assert items[1].content == "beta"


def test_consolidation_skip_contract() -> None:
    """Pinned items survive any number of recall + decay cycles untouched.

    The store doesn't expose decay itself — the contract is "the
    store never auto-removes". This test pins an item and verifies
    multiple search() calls don't drop it.
    """
    s = PinnedMemoryStore()
    s.pin("identity fact")
    for _ in range(20):
        s.search("identity")
    assert s.contains("identity fact")
