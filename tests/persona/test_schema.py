"""Pydantic validation tests for the persona schema."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from concinno.persona.schema import EmotionalState, PersonaSchema, PinnedMemory


def test_minimal_persona_only_needs_name() -> None:
    p = PersonaSchema(name="alice")
    assert p.name == "alice"
    assert p.personality == ""
    assert p.voice == ""
    assert p.memory_seed == []
    assert p.pinned_memories == []
    assert p.emotional_state.default == "neutral"


def test_emotional_state_clamps_intensity() -> None:
    with pytest.raises(ValidationError):
        EmotionalState(intensity=1.5)
    with pytest.raises(ValidationError):
        EmotionalState(intensity=-0.1)
    es = EmotionalState(intensity=0.5, decay_rate=0.9)
    assert es.intensity == 0.5
    assert es.decay_rate == 0.9


def test_pinned_memory_round_trip() -> None:
    pm = PinnedMemory(content="x", pinned_at="2026-04-25T00:00:00Z", reason="test")
    assert pm.content == "x"
    assert pm.reason == "test"


def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        PersonaSchema.model_validate({"name": "alice", "weird_field": True})


def test_pinned_memory_extra_forbidden() -> None:
    # Use a generic forbidden-extra field name. We explicitly avoid
    # writing any reserved marketing token here to keep the IP-safe
    # naming gate (test_ip_safe_naming) clean.
    with pytest.raises(ValidationError):
        PinnedMemory.model_validate(
            {"content": "x", "pinned_at": "x", "extra_field_xyz": 1}
        )


def test_full_persona_round_trip() -> None:
    src = {
        "name": "alice",
        "personality": "curious",
        "voice": "casual",
        "memory_seed": ["born in Tokyo"],
        "pinned_memories": [
            {"content": "user is Bob", "pinned_at": "2026-04-25T00:00:00Z"}
        ],
        "emotional_state": {"default": "positive", "intensity": 0.7, "decay_rate": 0.9},
    }
    p = PersonaSchema.model_validate(src)
    assert p.name == "alice"
    assert p.memory_seed == ["born in Tokyo"]
    assert len(p.pinned_memories) == 1
    assert p.emotional_state.default == "positive"
