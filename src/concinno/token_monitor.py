"""concinno.token_monitor — Real token usage tracking from Claude Code transcripts.

@module token_monitor
@responsibility Track real token usage from transcript JSONL files via the
    API ``usage`` field. **Fact-based only** — no file-size estimation
    fallback (estimation is unreliable due to compiled output, base64 images,
    cross-session stale caches, and produces misleading numbers that drive
    bad handoff decisions). If the transcript has no usage record, return
    None and let the caller decide how to degrade gracefully.
@dependencies concinno.guards.base
@exports read_real_token_usage, check_threshold, check_budget_gate, TokenGuard
"""

from __future__ import annotations

import json
import os
from typing import Optional

from concinno.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

# ─── Real Token Usage ────────────────────────────────────


def read_real_token_usage(filepath: str) -> Optional[dict]:
    """Read real token usage from the last main-thread API response.

    Fact-based: uses Anthropic API ``usage`` field written by Claude Code
    into the transcript JSONL. Reads the last 50KB (< 1ms) and scans
    backwards, filtering out:

    - ``isSidechain: true``  — subagent messages (wrong context window)
    - ``message.role != "assistant"``  — user/tool_result rows
    - Rows without ``usage.input_tokens``

    This guarantees the usage reflects the **main thread** context window,
    not a sidechain subagent's.

    Returns dict with:
        context_tokens    — window occupancy (input + cache_read + cache_create)
        cost_tokens       — weighted by pricing (cache_read 10%, cache_create 125%)
        output_tokens     — last response output
        cache_read_tokens — cache hit size (used for compact detection)

    Returns None if no eligible usage row found.
    """
    try:
        file_size = os.path.getsize(filepath)
        with open(filepath, "rb") as f:
            # Read 50KB (was 20KB) — cover edge case of many short messages
            f.seek(max(0, file_size - 50_000))
            tail = f.read().decode("utf-8", errors="replace")
        for line in reversed(tail.strip().split("\n")):
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except (json.JSONDecodeError, AttributeError):
                continue
            # Skip subagent (sidechain) rows — they have their own context
            if d.get("isSidechain") is True:
                continue
            msg = d.get("message")
            if not isinstance(msg, dict):
                continue
            # Filter out explicit non-assistant rows (user/tool_result).
            # Accept rows without ``role`` for backward compat — real Claude
            # Code transcripts always set role, but older test fixtures and
            # alternative JSONL shapes may omit it.
            role = msg.get("role")
            if role is not None and role != "assistant":
                continue
            u = msg.get("usage")
            if not isinstance(u, dict) or "input_tokens" not in u:
                continue
            inp = u.get("input_tokens", 0) or 0
            cache_read = u.get("cache_read_input_tokens", 0) or 0
            cache_create = u.get("cache_creation_input_tokens", 0) or 0
            out = u.get("output_tokens", 0) or 0
            context = inp + cache_read + cache_create
            cost = (
                inp + int(cache_read * 0.1)
                + int(cache_create * 1.25) + out
            )
            return {
                "context_tokens": context,
                "cost_tokens": cost,
                "output_tokens": out,
                "cache_read_tokens": cache_read,
            }
    except Exception:
        pass
    return None


# ─── Threshold Check ─────────────────────────────────────


def check_threshold(
    filepath: str,
    thresholds: list[tuple],
    *,
    state_dir: Optional[str] = None,
    session_id: str = "",
) -> Optional[dict]:
    """Check token usage against warning thresholds.

    Fact-based: reads the API ``usage`` field from the transcript.
    No file-size estimation — if the transcript has no usage record,
    returns None (caller must degrade gracefully).

    Args:
        filepath: Path to transcript JSONL file.
        thresholds: List of (threshold, icon, message, repeat?) tuples,
                    sorted descending by threshold.
        state_dir: Directory for persisting warned-level state.
        session_id: Current session ID for state tracking.

    Returns:
        Dict with {threshold, icon, message, est_tokens, est_k} or None.
    """
    real = read_real_token_usage(filepath)
    est_tokens = real["context_tokens"] if real else None
    if est_tokens is None:
        return None

    # Find highest crossed threshold
    crossed = None
    for entry in thresholds:
        t = entry[0]
        if est_tokens >= t:
            crossed = entry
            break

    if not crossed:
        return None

    threshold = crossed[0]
    icon = crossed[1]
    action = crossed[2]
    repeat = crossed[3] if len(crossed) > 3 else False
    est_k = est_tokens // 1000

    # Deduplicate: only warn once per level per session
    if state_dir and session_id and not repeat:
        short_id = session_id[:8]
        state_file = os.path.join(state_dir, f"{short_id}.json")
        try:
            os.makedirs(state_dir, exist_ok=True)
            if os.path.isfile(state_file):
                with open(state_file, "r", encoding="utf-8") as f:
                    state = json.load(f)
                if state.get("last_warned", 0) >= threshold:
                    return None
        except Exception:
            pass

    # Save warned level
    if state_dir and session_id:
        short_id = session_id[:8]
        state_file = os.path.join(state_dir, f"{short_id}.json")
        try:
            os.makedirs(state_dir, exist_ok=True)
            with open(state_file, "w", encoding="utf-8") as f:
                json.dump({"last_warned": threshold, "est_tokens": est_tokens}, f)
        except Exception:
            pass

    # Get cost-weighted tokens if available
    real = read_real_token_usage(filepath)
    cost_k = real["cost_tokens"] // 1000 if real else est_k

    return {
        "threshold": threshold,
        "icon": icon,
        "message": action,
        "est_tokens": est_tokens,
        "est_k": est_k,
        "cost_k": cost_k,
    }


# ─── Token Budget Gate (PreToolUse) ─────────────────────


def _find_transcript(session_id: str) -> str:
    """Delegate to unified transcript lookup in core.path_utils."""
    from concinno.core.path_utils import find_transcript
    return find_transcript(session_id)


def check_budget_gate(
    session_id: str,
    tool_name: str,
    *,
    agent_threshold: int = 0,
    critical_threshold: int = 0,
) -> Optional[dict]:
    """Enforce token budget via PreToolUse deny (Agent tool only).

    Model-aware defaults from MODEL_PROFILES:
      agent = min(quality_zone, 0.85 * force_handoff)
      critical = 0.95 * force_handoff
    Opus/Sonnet 1M → 200K/760K | Haiku 200K → 144K/161K.
    feature_config `token_gate` overrides defaults.
    """
    if tool_name != "Agent":
        return None

    # Full mode: don't block Agent spawns — sub-agents are the correct
    # token management tool for large tasks. Blocking them in full mode
    # seals the escape hatch.
    try:
        from concinno.handoff_engine import get_handoff_mode
        if get_handoff_mode() in ("full", "competition"):
            return None
    except (ImportError, Exception):
        pass

    # Model-aware defaults (Opus 1M vs Haiku 200K differ dramatically)
    if not agent_threshold or not critical_threshold:
        try:
            from concinno.token_zone import detect_model
            _profile = detect_model()
            _quality = _profile["quality_zone"]
            _force = _profile["force_handoff"]
            _default_agent = min(_quality, int(_force * 0.85))
            _default_critical = int(_force * 0.95)
        except Exception:
            # Last-resort fallback (Haiku 200K era values)
            _default_agent = 140_000
            _default_critical = 160_000

        try:
            from concinno.feature_config import get_feature
            info = get_feature("token_gate")
            params = info.get("params", {})
            if not agent_threshold:
                agent_threshold = params.get(
                    "agent_threshold", {}
                ).get("current", _default_agent)
            if not critical_threshold:
                critical_threshold = params.get(
                    "critical_threshold", {}
                ).get("current", _default_critical)
        except Exception:
            agent_threshold = agent_threshold or _default_agent
            critical_threshold = critical_threshold or _default_critical

    transcript = _find_transcript(session_id)
    if not transcript:
        return None

    usage = read_real_token_usage(transcript)
    if not usage:
        return None

    context = usage["context_tokens"]
    context_k = context // 1000
    cost_k = usage["cost_tokens"] // 1000

    from concinno.constants import make_deny

    if context >= critical_threshold:
        return make_deny(
            f"Token Gate: {context_k}K tokens — CRITICAL. "
            f"Agent spawn blocked. Write handoff now.",
            additionalContext=(
                f"Context: {context_k}K | Cost: {cost_k}K\n"
                "Session approaching limit. "
                "Complete current task and write handoff."
            ),
        )

    if context >= agent_threshold:
        return make_deny(
            f"Token Gate: {context_k}K tokens — Agent spawn blocked.",
            additionalContext=(
                f"Context: {context_k}K | Cost: {cost_k}K\n"
                "Finish current work without spawning agents. "
                "Write handoff if more work needed."
            ),
        )

    return None


# ── BaseGuard adapter ───────────────────────────────────────────


class TokenGuard(BaseGuard):
    """Enforce token budget via PreToolUse deny.

    Dual-source zone detection:
    1. Zone file from status line (preferred — real-time, precise)
    2. Transcript API usage (fallback — for Agent spawn blocking)

    Gate behavior by zone:
    - GREEN: pass all
    - YELLOW: pass all (injection handled by on-prompt-submit)
    - ORANGE: block Agent spawns
    - RED: block all except handoff tools (Read/Write/Edit/Glob/Grep/TodoWrite)
    """

    name = "token_guard"
    category = GuardCategory.QUALITY
    step_back_reason = "token budget near limit"

    def check(self, ctx: GuardContext) -> GuardResult | None:
        """Zone-aware token gate."""
        # Check persona_mode — persona sessions skip hard gates
        try:
            from concinno.token_zone import (
                Zone,
                read_zone_file,
                should_gate_tool,
            )
            persona = self._is_persona_mode()

            # Source 1: zone file from status line
            zone_data = read_zone_file(max_age_s=120)
            if zone_data:
                zone_level = zone_data.get("zone_level", 0)
                zone = Zone(zone_level)
                pct = zone_data.get("pct", 0)
                reason = should_gate_tool(
                    ctx.tool_name, zone, persona_mode=persona,
                )
                if reason:
                    return GuardResult.deny(
                        reason,
                        context=f"Token usage: {pct:.0f}% | Zone: {zone.name}",
                    )
                return None
        except (ImportError, Exception):
            pass

        # Source 2: fallback to transcript-based Agent blocking
        result = check_budget_gate(ctx.session_id, ctx.tool_name)
        if result is None:
            return None
        if result.get("permissionDecision") == "deny":
            return GuardResult.deny(
                result.get("reason", self.name),
                context=result.get("additionalContext", ""),
            )
        return None

    @staticmethod
    def _is_persona_mode() -> bool:
        """Check cc_config.json for persona_mode flag."""
        try:
            hooks_dir = os.path.join(
                os.environ.get("CLAUDE_PROJECT_DIR", ""),
                ".claude", "hooks",
            )
            cfg_path = os.path.join(hooks_dir, "cc_config.json")
            if os.path.isfile(cfg_path):
                with open(cfg_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                return cfg.get("token_guard", {}).get("persona_mode", False)
        except Exception:
            pass
        return False
