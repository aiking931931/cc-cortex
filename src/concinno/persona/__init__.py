"""concinno.persona — Generic agent persona harness.

Public API::

    from concinno.persona import Persona, PersonaSchema

    p = Persona.load("alice.md")
    reply = p.chat("hello")
    p.consolidate(turn=("hello", reply))
    p.pin_memory("user prefers short replies", reason="user-stated")
    relevant = p.recall("what does the user prefer?", top_k=3)
    p.save("alice_state.jsonl")

This module normalises the "agent reads persona file -> generates reply ->
updates persona state" pattern into a reusable OSS module. Backend is
pluggable (in-process LLM call, future HTTP endpoint, future local
fine-tuned model) so the same SDK call site can swap inference paths
without touching consumer code.

The ``pinned_memories`` mechanism is a generic anti-drift primitive:
explicit user/agent pin -> consolidation skip -> recall priority. This
is plain prior-art (rule-based memory pinning) and is intentionally
unrelated to any algorithmic tension/peak detection scheme.
"""

from __future__ import annotations

from concinno.persona.loader import load_persona_file
from concinno.persona.persona import (
    HTTPBackend,
    InProcessBackend,
    LocalModelBackend,
    Persona,
    PersonaBackend,
)
from concinno.persona.pinned_memories import PinnedMemoryStore
from concinno.persona.prompt import render_system_prompt
from concinno.persona.rag import PersonaRAG
from concinno.persona.schema import (
    EmotionalState,
    PersonaSchema,
    PinnedMemory,
)
from concinno.persona.state import PersonaState, TurnRecord

__all__ = [
    "EmotionalState",
    "HTTPBackend",
    "InProcessBackend",
    "LocalModelBackend",
    "Persona",
    "PersonaBackend",
    "PersonaRAG",
    "PersonaSchema",
    "PersonaState",
    "PinnedMemory",
    "PinnedMemoryStore",
    "TurnRecord",
    "load_persona_file",
    "render_system_prompt",
]
