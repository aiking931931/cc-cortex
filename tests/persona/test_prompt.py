"""Tests for prompt rendering."""

from __future__ import annotations

from concinno.persona.pinned_memories import PinnedMemoryStore
from concinno.persona.prompt import render_recall_context, render_system_prompt
from concinno.persona.schema import EmotionalState, PersonaSchema, PinnedMemory


def test_minimal_prompt_includes_name() -> None:
    s = PersonaSchema(name="alice")
    text = render_system_prompt(s)
    assert "alice" in text
    assert "neutral" in text  # emotional baseline default


def test_pins_appear_in_prompt() -> None:
    s = PersonaSchema(name="alice")
    pins = PinnedMemoryStore()
    pins.pin("user is Bob")
    pins.pin("user prefers short replies")
    text = render_system_prompt(s, pins)
    assert "user is Bob" in text
    assert "user prefers short replies" in text
    assert "Pinned facts" in text


def test_schema_pins_appear_when_no_store() -> None:
    s = PersonaSchema(
        name="alice",
        pinned_memories=[
            PinnedMemory(content="seeded fact", pinned_at="2026-04-25T00:00:00Z"),
        ],
    )
    text = render_system_prompt(s)
    assert "seeded fact" in text


def test_personality_and_voice_render() -> None:
    s = PersonaSchema(
        name="alice",
        personality="curious",
        voice="uses emoji",
    )
    text = render_system_prompt(s)
    assert "curious" in text
    assert "uses emoji" in text


def test_memory_seed_renders() -> None:
    s = PersonaSchema(name="alice", memory_seed=["born in Tokyo", "loves jazz"])
    text = render_system_prompt(s)
    assert "born in Tokyo" in text
    assert "loves jazz" in text


def test_recall_context_omits_when_empty() -> None:
    assert render_recall_context([]) == ""


def test_recall_context_renders_hits() -> None:
    block = render_recall_context(["fact A", "fact B"])
    assert "fact A" in block
    assert "fact B" in block


def test_extra_context_appears() -> None:
    s = PersonaSchema(name="alice")
    text = render_system_prompt(s, extra_context="user just woke up")
    assert "user just woke up" in text


def test_emotional_state_intensity_in_prompt() -> None:
    s = PersonaSchema(
        name="alice",
        emotional_state=EmotionalState(default="positive", intensity=0.8),
    )
    text = render_system_prompt(s)
    assert "positive" in text
    assert "0.80" in text or "0.8" in text
