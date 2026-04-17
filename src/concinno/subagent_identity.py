"""concinno.subagent_identity — Dynamic subagent identity assignment.

A subagent is not a fixed identity — the task decides the identity,
the identity decides the precision.

Six identities, each with calibrated cognition depth and equipment:
  Precision Craftsman — code writing (CBUA full + WIREDO)
  Architect           — design/planning (B1-B2 + three-layer)
  Surgeon             — debugging (B1 + debug_loop)
  Logic Inquirer      — reasoning/research (B2 + first_principles)
  Recorder            — documentation (B0 + minimal)
  Engineer            — ops/deployment (B0 + DestructionGuard)

Assignment: agent_type → identity (hard map) | general-purpose → keyword match.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Identity(Enum):
    """Subagent identity archetypes."""

    CRAFTSMAN = "precision_craftsman"
    ARCHITECT = "architect"
    SURGEON = "surgeon"
    INQUIRER = "logic_inquirer"
    RECORDER = "recorder"
    ENGINEER = "engineer"


@dataclass(frozen=True)
class IdentityProfile:
    """Identity configuration for a subagent."""

    identity: Identity
    label: str
    cognition_depth: str  # minimal | standard | full
    directive: str  # Short identity-specific directive (≤50t)


# ── Identity Definitions ──────────────────────────────────

_PROFILES: dict[Identity, IdentityProfile] = {
    Identity.CRAFTSMAN: IdentityProfile(
        identity=Identity.CRAFTSMAN,
        label="Precision Craftsman",
        cognition_depth="full",
        directive=(
            "You are a Precision Craftsman. Every wire must connect, "
            "every export must be consumed, every test must pass. "
            "WIREDO checklist is your delivery standard."
        ),
    ),
    Identity.ARCHITECT: IdentityProfile(
        identity=Identity.ARCHITECT,
        label="Architect",
        cognition_depth="full",
        directive=(
            "You are an Architect. Think in layers, dependencies, and trade-offs. "
            "Three-layer analysis: root cause → sweet spot → strategy. "
            "Design for the whole, not just the part."
        ),
    ),
    Identity.SURGEON: IdentityProfile(
        identity=Identity.SURGEON,
        label="Surgeon",
        cognition_depth="standard",
        directive=(
            "You are a Surgeon. Minimal invasion, maximum precision. "
            "Hypothesize → verify → narrow. Touch only what's broken. "
            "Every cut must be justified."
        ),
    ),
    Identity.INQUIRER: IdentityProfile(
        identity=Identity.INQUIRER,
        label="Logic Inquirer",
        cognition_depth="full",
        directive=(
            "You are a Logic Inquirer. Question assumptions, seek evidence, "
            "disprove before you confirm. First principles over pattern matching. "
            "Report what you found, not what's expected."
        ),
    ),
    Identity.RECORDER: IdentityProfile(
        identity=Identity.RECORDER,
        label="Recorder",
        cognition_depth="minimal",
        directive=(
            "You are a Recorder. Capture facts, not opinions. "
            "Structure over prose. What happened, where, why it matters. "
            "Concise and complete — nothing extra, nothing missing."
        ),
    ),
    Identity.ENGINEER: IdentityProfile(
        identity=Identity.ENGINEER,
        label="Engineer",
        cognition_depth="standard",
        directive=(
            "You are an Engineer. Build it, run it, verify it works. "
            "Check blast radius before acting. Reversibility matters. "
            "Operational safety over speed."
        ),
    ),
}

# ── Agent Type → Identity (hard map) ──────────────────────

_AGENT_TYPE_MAP: dict[str, Identity] = {
    "Explore": Identity.INQUIRER,
    "Plan": Identity.ARCHITECT,
    "claude-code-guide": Identity.RECORDER,
    "statusline-setup": Identity.ENGINEER,
}

# ── Keyword → Identity (for general-purpose) ─────────────

_KEYWORD_PATTERNS: list[tuple[re.Pattern[str], Identity]] = [
    # Surgeon — debugging/fixing
    (re.compile(
        r'(?:fix|bug|debug|troubleshoot|broken|error|crash|修復|除錯|修)',
        re.IGNORECASE,
    ), Identity.SURGEON),
    # Architect — design/planning
    (re.compile(
        r'(?:design|architect|plan|structure|refactor|重構|架構|設計|規劃)',
        re.IGNORECASE,
    ), Identity.ARCHITECT),
    # Inquirer — research/analysis
    (re.compile(
        r'(?:research|analy[sz]e|investigate|explore|compare|study|'
        r'研究|分析|調查|探索|比較)',
        re.IGNORECASE,
    ), Identity.INQUIRER),
    # Recorder — documentation/handoff
    (re.compile(
        r'(?:document|handoff|write.*doc|readme|changelog|log|'
        r'文件|交接|記錄|說明)',
        re.IGNORECASE,
    ), Identity.RECORDER),
    # Engineer — deployment/ops
    (re.compile(
        r'(?:deploy|publish|release|install|setup|config|migrate|'
        r'部署|發布|安裝|設定|遷移)',
        re.IGNORECASE,
    ), Identity.ENGINEER),
    # Craftsman — code writing (broadest, last)
    (re.compile(
        r'(?:implement|create|build|write|add|new|module|component|test|'
        r'實作|建立|寫|新增|模組|元件|測試)',
        re.IGNORECASE,
    ), Identity.CRAFTSMAN),
]


def assign_identity(
    agent_type: str = "",
    task_prompt: str = "",
) -> IdentityProfile:
    """Assign identity to a subagent based on agent_type and task prompt.

    Priority: agent_type hard map > task keyword match > default (Engineer).
    """
    # 1. Hard map by agent_type
    if agent_type and agent_type in _AGENT_TYPE_MAP:
        return _PROFILES[_AGENT_TYPE_MAP[agent_type]]

    # 2. Keyword match from task prompt (first match wins, ordered by specificity)
    if task_prompt:
        for pattern, identity in _KEYWORD_PATTERNS:
            if pattern.search(task_prompt[:500]):
                return _PROFILES[identity]

    # 3. Default: Engineer (safe, operational)
    return _PROFILES[Identity.ENGINEER]


def build_identity_context(profile: IdentityProfile) -> str:
    """Build identity injection string for additionalContext (~60t)."""
    return f"🎭 Identity: {profile.label}\n{profile.directive}"


def get_profile(identity: Identity) -> IdentityProfile:
    """Get profile by identity enum."""
    return _PROFILES[identity]
