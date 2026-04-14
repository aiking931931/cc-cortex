"""cc_cortex.cognitive — Cognitive layer for Claude Code.

@module cognitive
@responsibility Learn patterns, adapt thresholds, track decisions
@dependencies cc_cortex.constants, cc_cortex.guards.base

Four components:
1. SessionProfile — Classify session type, track work patterns, file domains
2. DecisionJournal — Record AI decisions + outcomes for self-improvement
3. AdaptiveThresholds — Adjust sentinel/guard thresholds from learned patterns
4. CognitiveEngine — Orchestrator that integrates sentinel, knowledge, evolution

Persistence: ~/.claude/cognitive/ (JSON files, one per component)
"""

from ._base import DEFAULT_THRESHOLDS, THRESHOLD_BOUNDS, classify_file_domain
from .cli import (
    cli_journal,
    cli_profiles,
    cli_promote,
    cli_promotions,
    cli_reset_thresholds,
    cli_status,
    cli_thresholds,
)
from .engine import (
    CognitiveEngine,
    CognitiveGuard,
    check_pre_tool,
    check_session_start,
    check_stop,
    on_post_tool,
    on_stop,
)
from .journal import DecisionJournal
from .profiles import PROFILES, ProductProfile, get_profile
from .router import (
    CognitiveLevel,
    CognitiveRoute,
    ComplexityDomain,
    ModelTier,
    classify_complexity,
    detect_asset_types,
    detect_model_tier,
    format_route_context,
    route,
)
from .session_profile import SessionProfile
from .thresholds import AdaptiveThresholds

# Backward compat alias (tests import the underscore-prefixed name)
_classify_file_domain = classify_file_domain

__all__ = [
    # Core components
    "SessionProfile",
    "DecisionJournal",
    "AdaptiveThresholds",
    "CognitiveEngine",
    "CognitiveGuard",
    # CBUA router
    "ComplexityDomain",
    "ModelTier",
    "CognitiveLevel",
    "CognitiveRoute",
    "classify_complexity",
    "detect_model_tier",
    "detect_asset_types",
    "format_route_context",
    "route",
    # CBUA profiles
    "ProductProfile",
    "PROFILES",
    "get_profile",
    # Hook entry points
    "check_session_start",
    "check_pre_tool",
    "check_stop",
    "on_stop",
    "on_post_tool",
    # CLI
    "cli_status",
    "cli_thresholds",
    "cli_journal",
    "cli_profiles",
    "cli_reset_thresholds",
    "cli_promotions",
    "cli_promote",
    # Constants
    "DEFAULT_THRESHOLDS",
    "THRESHOLD_BOUNDS",
]
