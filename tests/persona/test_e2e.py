"""End-to-end Persona smoke test: load -> chat -> consolidate -> recall.

Uses the ``echo`` backend so no LLM credentials are required. The
test exercises every public API on Persona to catch wiring breaks
that escape the per-module unit tests.
"""

from __future__ import annotations

from pathlib import Path

from concinno.persona import (
    EmotionalState,
    HTTPBackend,
    InProcessBackend,
    LocalModelBackend,
    Persona,
    PersonaBackend,
    PersonaSchema,
    PinnedMemory,
)

PERSONA_MD = """---
name: alice
personality: friendly, curious
voice: casual, brief
memory_seed:
  - "born in Tokyo 1995"
  - "loves jazz piano"
pinned_memories:
  - content: "user is Bob"
    pinned_at: "2026-04-25T00:00:00Z"
    reason: "first introduction"
emotional_state:
  default: neutral
  intensity: 0.5
  decay_rate: 0.9
---
"""


def _persona(tmp_path: Path) -> tuple[Path, Path]:
    persona = tmp_path / "alice.md"
    persona.write_text(PERSONA_MD, encoding="utf-8")
    state = tmp_path / "alice.jsonl"
    return persona, state


def test_load_chat_consolidate_recall_full_loop(tmp_path: Path) -> None:
    persona_path, state_path = _persona(tmp_path)
    backend = InProcessBackend(provider="echo")
    p = Persona.load(persona_path, backend=backend)

    # Schema parsed correctly.
    assert p.schema.name == "alice"
    assert any(m.content == "user is Bob" for m in p.schema.pinned_memories)
    assert "user is Bob" in [m.content for m in p.pins.all()]

    # First chat turn.
    reply = p.chat("hello, my name is Bob")
    assert "hello, my name is Bob" in reply  # echo backend echoes the user msg
    assert len(p.state.turns()) == 1

    # Recall sees the just-spoken turn AND the pinned identity fact.
    hits = p.recall("Bob", top_k=3)
    assert hits
    assert any("Bob" in h.text for h in hits)

    # Pin a new memory mid-session.
    p.pin_memory("user prefers short replies", reason="user-stated")
    assert any(m.content == "user prefers short replies" for m in p.pinned())

    # Consolidate -> should append a marker, not delete pins.
    summary = p.consolidate()
    assert "checkpoint" in summary
    assert any(m.content == "user prefers short replies" for m in p.pinned())

    # Save state, then reload + verify replay re-applies pins.
    p.save(state_path)
    p2 = Persona.load(persona_path, state=state_path, backend=backend)
    assert any(m.content == "user prefers short replies" for m in p2.pinned())
    assert any(r.kind == "consolidate" for r in p2.state.records)


def test_unpin_persists_through_replay(tmp_path: Path) -> None:
    persona_path, state_path = _persona(tmp_path)
    backend = InProcessBackend(provider="echo")
    p = Persona.load(persona_path, backend=backend)

    p.pin_memory("temp fact")
    assert any(m.content == "temp fact" for m in p.pinned())
    p.unpin_memory("temp fact")
    assert not any(m.content == "temp fact" for m in p.pinned())

    p.save(state_path)
    p2 = Persona.load(persona_path, state=state_path, backend=backend)
    assert not any(m.content == "temp fact" for m in p2.pinned())


def test_pinned_memory_survives_consolidation(tmp_path: Path) -> None:
    """Anti-drift contract: pinned facts MUST survive consolidate cycles."""
    persona_path, state_path = _persona(tmp_path)
    p = Persona.load(persona_path, backend=InProcessBackend(provider="echo"))

    p.pin_memory("critical identity fact")
    for _ in range(5):
        p.chat("noise turn")
        p.consolidate()
    assert any(m.content == "critical identity fact" for m in p.pinned())


def test_decay_emotion_clamps_intensity(tmp_path: Path) -> None:
    p = Persona(
        schema=PersonaSchema(
            name="x",
            emotional_state=EmotionalState(intensity=1.0, decay_rate=0.5),
        ),
        backend=InProcessBackend(provider="echo"),
    )
    em = p.decay_emotion()
    assert em.intensity == 0.5
    em = p.decay_emotion()
    assert em.intensity == 0.25


def test_track2_and_track3_backends_are_stub_classes() -> None:
    """Track 2 / 3 backends importable, but raise on chat() (per spec)."""
    h = HTTPBackend("https://example/v1/persona/x/turn")
    try:
        h.chat("sys", [], "hi")
    except NotImplementedError:
        pass
    else:
        raise AssertionError("HTTPBackend.chat should raise NotImplementedError")

    local = LocalModelBackend("aiking/concinno-persona-8b-v1")
    try:
        local.chat("sys", [], "hi")
    except NotImplementedError:
        pass
    else:
        raise AssertionError("LocalModelBackend.chat should raise NotImplementedError")


def test_persona_backend_abstract_chat_required() -> None:
    """Subclassing PersonaBackend without chat() should fail to instantiate."""

    class Broken(PersonaBackend):
        pass

    try:
        Broken()  # type: ignore[abstract]
    except TypeError:
        return
    raise AssertionError("PersonaBackend without chat() should not be instantiable")


def test_pinned_immutable_dict_shape(tmp_path: Path) -> None:
    """Sanity: pinned_memories survive a round-trip through PinnedMemory schema."""
    pm = PinnedMemory(content="x", pinned_at="2026-04-25T00:00:00Z", reason="r")
    p = Persona(
        schema=PersonaSchema(name="x", pinned_memories=[pm]),
        backend=InProcessBackend(provider="echo"),
    )
    pinned = p.pinned()
    assert pinned[0].content == "x"
    assert pinned[0].reason == "r"
