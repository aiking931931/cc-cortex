"""Cognitive router — C0 Perception + complexity classification + capability routing.

@module cognitive.router
@responsibility Classify task complexity (Cynefin-inspired), detect model capability tier,
    route to appropriate cognitive depth. Core of CBUA (Cognitive-Behavioral Unified Architecture).
@dependencies None (zero external deps for fast path)
@exports ComplexityDomain, ModelTier, CognitiveRoute, classify_complexity, detect_model_tier, route
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import Enum

# ── Enums ─────────────────────────────────────────────────

class ComplexityDomain(Enum):
    """Cynefin-inspired complexity domains."""
    SIMPLE = "simple"           # Known cause-effect, pattern match
    COMPLICATED = "complicated" # Analyzable, multi-step, expert knowledge
    COMPLEX = "complex"         # Uncertain cause-effect, needs exploration
    CHAOTIC = "chaotic"         # No clear cause-effect, stabilize first


class ModelTier(Enum):
    """Model capability tiers for adaptive cognitive depth."""
    T1_STRONG = "t1"   # Opus, Claude 4+ — skip scaffolding, invest in verification
    T2_MEDIUM = "t2"   # Sonnet — full templates, budget-limited deep exploration
    T3_WEAK = "t3"     # Haiku, small models — maximum scaffolding, simplified reasoning


class CognitiveLevel(Enum):
    """Cognitive layer levels (C0-C5)."""
    C0_PERCEIVE = "c0"
    C1_FAST = "c1"
    C2_STRUCTURED = "c2"
    C3_DEEP = "c3"
    C4_META = "c4"       # Always-on background
    C5_CORRECT = "c5"    # On failure


# ── Data models ───────────────────────────────────────────

@dataclass
class CognitiveRoute:
    """Routing decision from C0 perception."""
    complexity: ComplexityDomain
    tier: ModelTier
    entry_level: CognitiveLevel
    reasoning_budget_pct: int     # % of total token budget for reasoning
    action_budget_pct: int        # % for action
    meta_budget_pct: int          # % for metacognition
    scaffolding: str              # "none" | "minimal" | "standard" | "maximum"
    asset_types: list[str] = field(default_factory=list)  # detected asset types
    signals: dict = field(default_factory=dict)  # classification signals for transparency


# ── Complexity classification ─────────────────────────────

# Markers that indicate higher complexity
_COMPLEX_MARKERS = re.compile(
    r"(?:不確定|探索|研究|可能|也許|試試|不知道|新的|從沒|第一次"
    r"|uncertain|explore|research|maybe|experiment|unknown|novel|first.time)",
    re.IGNORECASE,
)

_CHAOTIC_MARKERS = re.compile(
    r"(?:崩潰|緊急|壞了|全掛|rescue|crash|emergency|broken|urgent|down|critical)",
    re.IGNORECASE,
)

# Known patterns = simpler tasks
_SIMPLE_PATTERNS = re.compile(
    # NOTE: 交接/deploy removed — these are Complicated+ tasks
    # (交接 = cross-session handoff, deploy = irreversible production push)
    r"(?:修改|改名|加個|刪掉|更新|rename|fix.typo|add.import|remove|update.version"
    r"|commit|push|格式)",
    re.IGNORECASE,
)

# Asset type detection from user message
_ASSET_PATTERNS: dict[str, re.Pattern] = {
    "code": re.compile(r"(?:代碼|程式|function|class|module|api|hook|guard|test|lint|型別)", re.I),
    "image": re.compile(r"(?:圖片|照片|image|photo|生圖|kontext|頭像|avatar|截圖)", re.I),
    "video": re.compile(r"(?:影片|video|舞蹈|dance|動畫|animation|kling)", re.I),
    "audio": re.compile(r"(?:音訊|audio|語音|voice|音樂|music|tts|stt)", re.I),
    "document": re.compile(r"(?:文件|document|word|docx|書|book|翻譯|translation|md)", re.I),
}


def classify_complexity(
    user_message: str,
    tool_count_estimate: int = 0,
    has_known_pattern: bool = False,
) -> tuple[ComplexityDomain, dict]:
    """Classify task complexity using heuristic signals.

    Returns (domain, signals_dict) for transparency.

    Signals used (max 3, per R2 red-team):
    1. Explicit markers in user message
    2. Estimated step count
    3. Whether a known solution pattern exists
    """
    signals: dict = {}

    # Signal 1: Explicit markers
    if _CHAOTIC_MARKERS.search(user_message):
        signals["markers"] = "chaotic"
    elif _COMPLEX_MARKERS.search(user_message):
        signals["markers"] = "complex"
    elif _SIMPLE_PATTERNS.search(user_message):
        signals["markers"] = "simple"
    else:
        signals["markers"] = "neutral"

    # Signal 2: Estimated steps (from tool count or message length heuristic)
    estimated_steps = tool_count_estimate or _estimate_steps(user_message)
    signals["estimated_steps"] = estimated_steps

    # Signal 3: Known pattern match
    signals["has_known_pattern"] = has_known_pattern

    # Classification logic (majority vote)
    if signals["markers"] == "chaotic":
        return ComplexityDomain.CHAOTIC, signals

    if signals["markers"] == "simple" and estimated_steps <= 3:
        return ComplexityDomain.SIMPLE, signals

    if has_known_pattern and estimated_steps <= 5:
        # R2 fix: known pattern match → downgrade to Simple even if message is long
        return ComplexityDomain.SIMPLE, signals

    if estimated_steps <= 3 and signals["markers"] == "neutral":
        return ComplexityDomain.SIMPLE, signals

    if signals["markers"] == "complex" or estimated_steps > 10:
        return ComplexityDomain.COMPLEX, signals

    return ComplexityDomain.COMPLICATED, signals


def _estimate_steps(message: str) -> int:
    """Heuristic step count estimation from message content."""
    # Count action verbs as step indicators
    actions = re.findall(
        r"(?:然後|接著|再來|之後|最後|還要|另外|同時|並且"
        r"|then|next|also|after.that|finally|additionally|\d+[\.\)、])",
        message,
        re.IGNORECASE,
    )
    # Base: 1 step + counted transitions
    return max(1, 1 + len(actions))


# ── Model tier detection ──────────────────────────────────

# Model name → tier mapping
_MODEL_TIERS: dict[str, ModelTier] = {
    # T1 Strong
    "opus": ModelTier.T1_STRONG,
    "claude-opus": ModelTier.T1_STRONG,
    "claude-4": ModelTier.T1_STRONG,
    # T2 Medium
    "sonnet": ModelTier.T2_MEDIUM,
    "claude-sonnet": ModelTier.T2_MEDIUM,
    "claude-3.5": ModelTier.T2_MEDIUM,
    # T3 Weak
    "haiku": ModelTier.T3_WEAK,
    "claude-haiku": ModelTier.T3_WEAK,
}


def detect_model_tier(model_name: str = "") -> ModelTier:
    """Detect model capability tier from model name or environment.

    Falls back to T2 (safe middle ground) if unknown.
    """
    name = model_name or os.environ.get("CLAUDE_MODEL", "")
    name_lower = name.lower()

    # Direct match
    for pattern, tier in _MODEL_TIERS.items():
        if pattern in name_lower:
            return tier

    # Fallback: T2 is safe default (per R1 red-team: unknown → conservative)
    return ModelTier.T2_MEDIUM


# ── Asset type detection ──────────────────────────────────

def detect_asset_types(message: str) -> list[str]:
    """Detect asset types from user message content."""
    types = []
    for asset_type, pattern in _ASSET_PATTERNS.items():
        if pattern.search(message):
            types.append(asset_type)
    return types or ["code"]  # Default to code if nothing detected


# ── Main router ───────────────────────────────────────────

# Budget allocation per complexity × tier (from red-team R3 sweet spot)
_BUDGET_TABLE: dict[ComplexityDomain, tuple[int, int, int]] = {
    # (reasoning%, action%, meta%)
    # R3 revised: Simple reasoning 5→15 (prevent garbage output from under-thinking)
    # Complicated reasoning 25→30, meta 15→20 (enable direction questioning)
    ComplexityDomain.SIMPLE: (15, 75, 10),
    ComplexityDomain.COMPLICATED: (30, 50, 20),
    ComplexityDomain.COMPLEX: (35, 40, 25),
    ComplexityDomain.CHAOTIC: (40, 25, 35),
}

_ENTRY_LEVEL: dict[ComplexityDomain, CognitiveLevel] = {
    ComplexityDomain.SIMPLE: CognitiveLevel.C1_FAST,
    ComplexityDomain.COMPLICATED: CognitiveLevel.C2_STRUCTURED,
    ComplexityDomain.COMPLEX: CognitiveLevel.C3_DEEP,
    ComplexityDomain.CHAOTIC: CognitiveLevel.C3_DEEP,
}

_SCAFFOLDING: dict[tuple[ComplexityDomain, ModelTier], str] = {
    # Simple
    (ComplexityDomain.SIMPLE, ModelTier.T1_STRONG): "none",
    (ComplexityDomain.SIMPLE, ModelTier.T2_MEDIUM): "minimal",
    (ComplexityDomain.SIMPLE, ModelTier.T3_WEAK): "standard",
    # Complicated
    (ComplexityDomain.COMPLICATED, ModelTier.T1_STRONG): "minimal",
    (ComplexityDomain.COMPLICATED, ModelTier.T2_MEDIUM): "standard",
    (ComplexityDomain.COMPLICATED, ModelTier.T3_WEAK): "maximum",
    # Complex
    (ComplexityDomain.COMPLEX, ModelTier.T1_STRONG): "standard",
    (ComplexityDomain.COMPLEX, ModelTier.T2_MEDIUM): "standard",
    (ComplexityDomain.COMPLEX, ModelTier.T3_WEAK): "maximum",
    # Chaotic
    (ComplexityDomain.CHAOTIC, ModelTier.T1_STRONG): "standard",
    (ComplexityDomain.CHAOTIC, ModelTier.T2_MEDIUM): "maximum",
    (ComplexityDomain.CHAOTIC, ModelTier.T3_WEAK): "maximum",
}


def route(
    user_message: str,
    model_name: str = "",
    tool_count_estimate: int = 0,
    has_known_pattern: bool = False,
) -> CognitiveRoute:
    """Main routing function: classify → detect tier → allocate budget → return route.

    This is the C0 Perception entry point for the CBUA architecture.
    ~50 tokens overhead (classification only, no heavy processing).
    """
    complexity, signals = classify_complexity(
        user_message, tool_count_estimate, has_known_pattern,
    )
    tier = detect_model_tier(model_name)
    asset_types = detect_asset_types(user_message)

    reasoning, action, meta = _BUDGET_TABLE[complexity]
    entry = _ENTRY_LEVEL[complexity]
    scaffolding = _SCAFFOLDING.get((complexity, tier), "standard")

    return CognitiveRoute(
        complexity=complexity,
        tier=tier,
        entry_level=entry,
        reasoning_budget_pct=reasoning,
        action_budget_pct=action,
        meta_budget_pct=meta,
        scaffolding=scaffolding,
        asset_types=asset_types,
        signals=signals,
    )


# ── Convenience: format route as context string ───────────

def format_route_context(r: CognitiveRoute) -> str:
    """Format a CognitiveRoute as a concise context string for injection.

    Only injected when non-trivial (Complicated+).
    Simple tasks get no injection (per 認知守恆 — don't waste tokens on simple tasks).
    """
    if r.complexity == ComplexityDomain.SIMPLE:
        return ""

    parts = [
        f"[CBUA] {r.complexity.value}",
        f"tier={r.tier.value}",
        f"entry={r.entry_level.value}",
        f"budget=R{r.reasoning_budget_pct}/A{r.action_budget_pct}/M{r.meta_budget_pct}",
    ]
    if r.scaffolding != "none":
        parts.append(f"scaffolding={r.scaffolding}")
    if r.asset_types != ["code"]:
        parts.append(f"assets={','.join(r.asset_types)}")

    return " | ".join(parts)
