"""concinno.persona.cognition — Server-side cognition primitives.

@module concinno.persona.cognition
@responsibility Public exports for the persona cognition pipeline. Each
    module is a cleanroom Python implementation of the contract described
    in ``concinno-persona-track2-spec-2026-04-25.md`` §2.2 — no transpile
    of any TS source, only behavioural equivalence.

Track 2 Module A starter (4.3.0-week1) ships :class:`IntentRouter` and its
input / output dataclasses. Modules B-D (background_layer / background_gate
/ output_gate) land in subsequent week builds.

This package is **standalone library** — not yet wired to any HTTP endpoint.
Endpoint integration ships in week 2-3 per the parent plan.
"""

from __future__ import annotations

from concinno.persona.cognition.intent_router import (
    BackgroundTask,
    DispatchDecision,
    IntentRouteInput,
    IntentRouteOutput,
    IntentRouter,
    MessageSignals,
    ProcessingLayer,
)

__all__ = [
    "BackgroundTask",
    "DispatchDecision",
    "IntentRouteInput",
    "IntentRouteOutput",
    "IntentRouter",
    "MessageSignals",
    "ProcessingLayer",
]
