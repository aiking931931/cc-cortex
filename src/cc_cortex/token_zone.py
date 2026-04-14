"""cc_cortex.token_zone — Three-zone token management with model-aware thresholds.

@module token_zone
@responsibility Detect token usage zone (green/yellow/red) using absolute token
    thresholds adapted per model. Generate UX display strings and handoff/compact
    injection text. Track compact count across cycles. Supports persona_mode
    (skip handoff, silent compact) and full mode (800K forced handoff, continue).
@dependencies cc_cortex.token_monitor
@exports Zone, ModelProfile, detect_model, detect_zone_abs, format_ux,
    zone_injection, write_zone_file, read_zone_file, should_gate_tool,
    ZONE_FILE, HANDOFF_TOOLS, MODEL_PROFILES
"""

from __future__ import annotations

import json
import os
import time
from enum import IntEnum
from typing import Optional

# ── Zone file path ─────────────────────────────────────────
ZONE_FILE = os.path.join(os.path.expanduser("~"), ".claude", ".token_zone.json")

# Tools allowed during handoff (red zone gate)
HANDOFF_TOOLS = frozenset({
    "Read", "Write", "Edit", "Glob", "Grep",
    "TodoWrite", "AskUserQuestion",
})

# Tools allowed during critical zone (write handoff only)
CRITICAL_TOOLS = frozenset({"Write", "Edit", "Read", "TodoWrite"})


# ── Model Profiles ────────────────────────────────────────
# quality_zone: research-backed quality threshold (absolute tokens)
# force_handoff: force write handoff snapshot (full mode continues after)
# context_limit: model's actual context window
MODEL_PROFILES: dict[str, dict] = {
    "mythos": {
        "display": "Mythos",
        "context_limit": 2_000_000,
        "quality_zone": 400_000,
        "force_handoff": 1_600_000,
    },
    "opus": {
        "display": "Opus",
        "context_limit": 1_000_000,
        "quality_zone": 800_000,
        "force_handoff": 950_000,
    },
    "sonnet": {
        "display": "Sonnet",
        "context_limit": 1_000_000,
        "quality_zone": 800_000,
        "force_handoff": 950_000,
    },
    "haiku": {
        "display": "Haiku",
        "context_limit": 200_000,
        "quality_zone": 200_000,
        "force_handoff": 170_000,
    },
}

DEFAULT_PROFILE = MODEL_PROFILES["opus"]


# ── Zone Enum ──────────────────────────────────────────────
class Zone(IntEnum):
    GREEN = 0    # < C1 — full quality work
    YELLOW = 1   # C1~C3 — checkpoint reminders
    ORANGE = 2   # C3~C5 — subagent mode + frequent checkpoints
    RED = 3      # >= C5 — force handoff


# ── Checkpoint Thresholds (Opus 1M reference) ──────────────
# These are absolute token thresholds for persistent checkpoint triggers.
# Checkpoint = write handoff snapshot + sediment feedback + update memory.
# NOT "stop working" — just persist state, then continue.
CHECKPOINT_THRESHOLDS = {
    "opus": [
        (200_000, "C1", "early", "軟提醒 + checkpoint"),
        (400_000, "C2", "mid", "軟提醒 + checkpoint"),
        (600_000, "C3", "quality_boundary", "強制 checkpoint + 子代理模式"),
        (800_000, "C4", "danger", "每次對話 checkpoint"),
        (900_000, "C5", "limit", "強制交接"),
    ],
    "sonnet": [
        (200_000, "C1", "early", "軟提醒 + checkpoint"),
        (400_000, "C2", "mid", "軟提醒 + checkpoint"),
        (600_000, "C3", "quality_boundary", "強制 checkpoint + 子代理模式"),
        (800_000, "C4", "danger", "每次對話 checkpoint"),
        (900_000, "C5", "limit", "強制交接"),
    ],
    "haiku": [
        (80_000, "C1", "early", "軟提醒 + checkpoint"),
        (120_000, "C2", "mid", "軟提醒 + checkpoint"),
        (150_000, "C3", "quality_boundary", "強制 checkpoint + 子代理模式"),
        (170_000, "C4", "danger", "每次對話 checkpoint"),
        (185_000, "C5", "limit", "強制交接"),
    ],
}


def get_checkpoint_level(
    tokens: int,
    model_key: str = "opus",
    is_full_mode: bool = False,
) -> dict | None:
    """Determine current checkpoint level based on token count.

    Returns dict with {level, tag, severity, message, is_full_mode} or None if GREEN.
    Full mode: same thresholds but silent (no stderr output to user).
    """
    thresholds = CHECKPOINT_THRESHOLDS.get(model_key, CHECKPOINT_THRESHOLDS["opus"])
    level = None
    for threshold, tag, severity, message in reversed(thresholds):
        if tokens >= threshold:
            level = {
                "threshold": threshold,
                "level": tag,
                "severity": severity,
                "message": message,
                "is_full_mode": is_full_mode,
                "tokens": tokens,
                "tokens_k": tokens // 1000,
            }
            break
    return level


# ── Model Detection ───────────────────────────────────────

def _parse_model_key(model_str: str) -> str:
    """Extract model family key from model string."""
    s = model_str.lower()
    if "mythos" in s or "capybara" in s:
        return "mythos"
    if "opus" in s:
        return "opus"
    if "sonnet" in s:
        return "sonnet"
    if "haiku" in s:
        return "haiku"
    return "opus"  # default


def detect_model() -> dict:
    """Detect current model profile from environment or settings.

    Priority: ANTHROPIC_MODEL env > settings.local.json > settings.json > default (opus).
    """
    # 1. Environment variable
    model_env = os.environ.get("ANTHROPIC_MODEL", "")
    if model_env:
        return MODEL_PROFILES.get(_parse_model_key(model_env), DEFAULT_PROFILE)

    # 2. Project settings, then global settings
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    paths = []
    if project_dir:
        paths.append(os.path.join(project_dir, ".claude", "settings.local.json"))
    paths.append(os.path.join(os.path.expanduser("~"), ".claude", "settings.json"))

    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            model = data.get("model", "")
            if model:
                return MODEL_PROFILES.get(_parse_model_key(model), DEFAULT_PROFILE)
        except Exception:
            continue

    return DEFAULT_PROFILE


# ── Zone Detection (Absolute Tokens) ─────────────────────

def detect_zone_abs(tokens: int, profile: Optional[dict] = None) -> Zone:
    """Classify token count into zone using checkpoint thresholds.

    GREEN  < C1 (200K Opus)
    YELLOW C1~C3 (200K~600K)
    ORANGE C3~C5 (600K~900K) — subagent mode
    RED    >= C5 (900K) — force handoff

    Args:
        tokens: Current context token count.
        profile: Model profile dict. Auto-detected if None.
    """
    if profile is None:
        profile = detect_model()

    # Use checkpoint thresholds for zone classification
    model_key = _parse_model_key(profile.get("display", "opus"))
    thresholds = CHECKPOINT_THRESHOLDS.get(model_key, CHECKPOINT_THRESHOLDS["opus"])

    # C5+ = RED, C3-C4 = ORANGE, C1-C2 = YELLOW, below C1 = GREEN
    if len(thresholds) >= 5 and tokens >= thresholds[4][0]:
        return Zone.RED
    if len(thresholds) >= 3 and tokens >= thresholds[2][0]:
        return Zone.ORANGE
    if len(thresholds) >= 1 and tokens >= thresholds[0][0]:
        return Zone.YELLOW
    return Zone.GREEN


def detect_zone(pct: float) -> Zone:
    """Legacy: classify token usage percentage into zone.

    Kept for backwards compatibility. Prefer detect_zone_abs().
    """
    if pct >= 85:
        return Zone.RED
    if pct >= 60:
        return Zone.YELLOW
    return Zone.GREEN


# ── UX Display Format ─────────────────────────────────────

def format_ux(
    tokens: int,
    profile: Optional[dict] = None,
    compact_count: int = 0,
) -> str:
    """Format UX status string for display.

    Denominator = quality_zone (per-model). Examples:
        [Opus]   🟢 87/800K           (quality 800K of 1M limit)
        [Sonnet] 🟡 847/800K [C1]     (past quality, pre-forced-handoff)
        [Haiku]  🔴 170/170K [C2] → 強制寫交接
    """
    if profile is None:
        profile = detect_model()

    zone = detect_zone_abs(tokens, profile)
    emoji = {Zone.GREEN: "🟢", Zone.YELLOW: "🟡", Zone.ORANGE: "🟠", Zone.RED: "🔴"}[zone]

    current_k = tokens // 1000
    quality_k = profile["quality_zone"] // 1000

    parts = [f"[{profile['display']}] {emoji} {current_k}/{quality_k}K"]

    if compact_count > 0:
        parts.append(f"[C{compact_count}]")

    if zone == Zone.ORANGE:
        parts.append("→ 子代理模式")
    elif zone == Zone.RED:
        parts.append("→ 強制交接")

    return " ".join(parts)


# ── Zone File I/O ──────────────────────────────────────────

def write_zone_file(
    pct: float,
    input_tokens: int = 0,
    compact_count: int = 0,
) -> None:
    """Write zone state to file (called from status line or hooks)."""
    profile = detect_model()
    zone = detect_zone_abs(input_tokens, profile) if input_tokens else detect_zone(pct)

    data = {
        "zone": zone.name.lower(),
        "zone_level": int(zone),
        "pct": round(pct, 1),
        "input_tokens": input_tokens,
        "compact_count": compact_count,
        "model": profile["display"].lower(),
        "model_display": profile["display"],
        "quality_zone": profile["quality_zone"],
        "force_handoff": profile["force_handoff"],
        "context_limit": profile["context_limit"],
        "ux": format_ux(input_tokens, profile, compact_count) if input_tokens else "",
        "ts": int(time.time()),
    }
    try:
        os.makedirs(os.path.dirname(ZONE_FILE), exist_ok=True)
        with open(ZONE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass


def read_zone_file(max_age_s: int = 120) -> Optional[dict]:
    """Read zone file. Returns None if stale (>max_age_s) or missing."""
    try:
        with open(ZONE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        age = int(time.time()) - data.get("ts", 0)
        if age > max_age_s:
            return None
        return data
    except Exception:
        return None


def increment_compact_count() -> int:
    """Increment compact count in zone file. Returns new count."""
    data = read_zone_file(max_age_s=86400)  # 24h — compact count persists
    count = (data.get("compact_count", 0) if data else 0) + 1

    # Update zone file with new count
    if data:
        data["compact_count"] = count
        data["ts"] = int(time.time())
        try:
            with open(ZONE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception:
            pass

    return count


# ── Injection Text ─────────────────────────────────────────

def _build_injection(tokens: int, profile: dict, compact_count: int) -> Optional[str]:
    """Build injection text based on absolute token zone."""
    zone = detect_zone_abs(tokens, profile)
    ux = format_ux(tokens, profile, compact_count)

    if zone == Zone.GREEN:
        return None

    if zone == Zone.YELLOW:
        return (
            f"{ux}\n"
            "Checkpoint 提醒：整理資料 + 更新交接 + 沉澱 feedback。繼續工作。"
        )

    if zone == Zone.ORANGE:
        return (
            f"{ux}\n"
            "品質邊界：具體實作派子代理（乾淨 context）。"
            "主代理 = 指揮官。每次對話寫 checkpoint。"
        )

    # RED
    return (
        f"{ux}\n"
        "極限區：強制寫交接。所有實作派子代理。"
        "主代理只做：決策 → prompt → 派子代理 → 驗收。"
    )


# Persona mode: ZERO injection. The 表意識 is a stateless rendering layer.
# 潛意識 owns the state and persists it. Auto-compact restores naturally.
_PERSONA_INJECTION: dict = {}  # intentionally empty


def zone_injection(
    pct: float,
    *,
    persona_mode: bool = False,
    input_tokens: int = 0,
) -> Optional[str]:
    """Generate injection text for the current token state.

    Args:
        pct: Token usage percentage (0-100). Used as fallback.
        persona_mode: If True, skip handoff instructions.
        input_tokens: Absolute token count (preferred over pct).

    Returns:
        Injection text string, or None if green zone.
    """
    if persona_mode:
        return None

    # Prefer absolute tokens
    if input_tokens > 0:
        profile = detect_model()
        zone_data = read_zone_file(max_age_s=86400)
        compact_count = zone_data.get("compact_count", 0) if zone_data else 0
        return _build_injection(input_tokens, profile, compact_count)

    # Fallback: percentage-based (legacy)
    zone = detect_zone(pct)
    if zone == Zone.GREEN:
        return None

    _LEGACY_INJECTION = {
        Zone.YELLOW: (
            "⚠️ Token 用量 {pct:.0f}% — 黃區。"
            "完成當前子任務後準備寫交接。不要開始新的大任務。"
        ),
        Zone.RED: (
            "🔴 Token 用量 {pct:.0f}% — 紅區！"
            "立即寫交接摘要。格式：⏸ + 已完成/待做/未解決。"
        ),
    }
    template = _LEGACY_INJECTION.get(zone)
    return template.format(pct=pct) if template else None


def should_gate_tool(
    tool_name: str,
    zone: Zone,
    *,
    persona_mode: bool = False,
    handoff_mode: str = "",
) -> Optional[str]:
    """Check if a tool should be blocked in the current zone.

    Returns deny reason string, or None if allowed.
    """
    if persona_mode:
        return None

    # Full mode: never hard-gate (write handoff via injection, but don't block)
    if handoff_mode == "full":
        return None

    if zone in (Zone.YELLOW, Zone.ORANGE):
        # Yellow/Orange: all tools allowed, checkpoint via injection
        return None

    if zone == Zone.RED:
        # Red: only Agent + coordination tools allowed
        if tool_name in ("Agent", *HANDOFF_TOOLS, "TodoWrite", "AskUserQuestion"):
            return None
        return (
            f"🔴 極限區 — {tool_name} 應由子代理執行。"
            "主代理只做指揮：派 Agent、讀結果、寫交接。"
        )

    return None
