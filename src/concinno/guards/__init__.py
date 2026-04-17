"""concinno.guards — Unified guard pipeline for Claude Code hooks.

@module guards
@responsibility Three-layer guard architecture (Security / Quality /
    Cognitive) re-exported for convenience
@dependencies concinno.guards.base, .pipeline, .registry
@exports BaseGuard, GuardAction, GuardCategory, GuardContext,
    GuardResult, GuardPipeline, create_default_pipeline
"""

from concinno.guards.base import (
    BaseGuard,
    GuardAction,
    GuardCategory,
    GuardContext,
    GuardResult,
)
from concinno.guards.pipeline import GuardPipeline
from concinno.guards.registry import create_default_pipeline, create_extended_pipeline

__all__ = [
    "BaseGuard",
    "GuardAction",
    "GuardCategory",
    "GuardContext",
    "GuardPipeline",
    "GuardResult",
    "create_default_pipeline",
    "create_extended_pipeline",
]
