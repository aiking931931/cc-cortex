"""concinno.tools.security — security utility tools.

Exports:
    KeyRotator        — orchestrator: rotate API keys via provider mgmt APIs.
    KeyRotationResult — TypedDict describing a single rotation outcome.
    RotationPlugin    — Protocol all provider plugins must satisfy.
    BaseRotationPlugin — Abstract base with shared audit-log + smoke-test helpers.
    DeepgramRotationPlugin  — Full create+revoke via Deepgram Project Keys API.
    ElevenLabsRotationPlugin — Full create+revoke via ElevenLabs Service-Account API.
    AnthropicRotationPlugin — Deactivate-only (Admin API can't create new keys).

Feature gate: ``key_rotation_automation`` (default OFF).  Enable via::

    concinno features set key_rotation_automation enabled true

or env ``CONCINNO_KEY_ROTATION_AUTOMATION_ENABLED=1``.
"""

from __future__ import annotations

from .key_rotator import (
    AnthropicRotationPlugin,
    BaseRotationPlugin,
    DeepgramRotationPlugin,
    ElevenLabsRotationPlugin,
    KeyRotationResult,
    KeyRotator,
    RotationPlugin,
)

__all__ = [
    "KeyRotator",
    "KeyRotationResult",
    "RotationPlugin",
    "BaseRotationPlugin",
    "DeepgramRotationPlugin",
    "ElevenLabsRotationPlugin",
    "AnthropicRotationPlugin",
]
