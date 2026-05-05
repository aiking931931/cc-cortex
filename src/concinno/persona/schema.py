"""Pydantic schema for persona files (IP-safe naming).

The schema deliberately uses generic terms (``emotional_state``,
``pinned_memories``, ``decay_rate``) that are common across consumer
chat / dialog systems. Algorithm-specific parameters from any
proprietary tension/peak/anchor calculation system are out of scope
and are NOT to be added to this schema.

``model_config = ConfigDict(extra="forbid")`` is set so any future
attempt to silently sneak proprietary field names into the OSS schema
will raise at validation time and get caught by CI.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class EmotionalState(BaseModel):
    """Generic emotional baseline.

    Decay-based, not field-based. ``decay_rate`` is a plain
    multiplicative factor applied per consolidation cycle. No
    field/wave/resonance semantics are claimed.
    """

    model_config = ConfigDict(extra="forbid")

    default: Literal["neutral", "positive", "negative"] = "neutral"
    intensity: float = Field(default=0.5, ge=0.0, le=1.0)
    decay_rate: float = Field(default=0.95, ge=0.0, le=1.0)


class PinnedMemory(BaseModel):
    """A memory the user or agent has explicitly pinned.

    Pinned memories are excluded from consolidation summarisation and
    are returned with priority by ``Persona.recall``. This is a
    rule-based primitive (explicit pin + skip + priority), distinct
    from any automatic peak-detection pinning algorithm.
    """

    model_config = ConfigDict(extra="forbid")

    content: str
    pinned_at: str  # ISO 8601 timestamp
    reason: str | None = None


class PersonaSchema(BaseModel):
    """Top-level persona definition loaded from a YAML/MD frontmatter.

    Forbids extra fields by design — the OSS schema surface is
    deliberately small and any new field must be added here
    explicitly so reviewers can inspect it.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    personality: str = ""  # free-text personality description
    voice: str = ""        # free-text voice/tone description
    memory_seed: list[str] = Field(default_factory=list)
    pinned_memories: list[PinnedMemory] = Field(default_factory=list)
    emotional_state: EmotionalState = Field(default_factory=EmotionalState)


__all__ = ["EmotionalState", "PinnedMemory", "PersonaSchema"]
