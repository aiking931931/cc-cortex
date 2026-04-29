"""concinno.step_back — Two-tier gate buffer: Step-Back then Hard Deny.

@module step_back
@responsibility Configurable two-tier gate: first trigger allows with a Step-Back
    self-correction prompt, second trigger hard-denies with user notification.
@dependencies concinno.constants
@exports wrap_gate, clear_state, record_global_failure, clear_global_failures

First trigger:  allow + Step-Back prompt (Claude self-corrects)
Second trigger: hard deny + user-visible notification

Every gate can be configured independently:
  mode = "step_back_first" | "hard_deny" | "off"

Usage:
    from concinno.step_back import wrap_gate, clear_state

    result = wrap_gate("sentinel_gate", original_deny, session_id, state_dir,
                       reason="too many repeated edits on same file")
    # result is None (off), allow-dict (step-back), or deny-dict (hard)
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Optional

from concinno.constants import make_deny
from concinno.hooks.relay_helpers import with_feature_prefix

# ── Step-Back Prompt Templates ─────────────────────────────────
#
# Templates carry a ``{verbatim}`` placeholder filled at format time
# by :func:`with_feature_prefix` so the relay layer can self-brand
# the visible message with ``[Concinno: <gate>]`` (or strip it
# under verbose / silent / off modes). The non-visible body above
# the blank line is internal LLM guidance and stays unbranded.

_STEP_BACK_TEMPLATE = (
    "⚠ [{gate}] {reason}\n"
    "Stop. Change direction and retry. Next identical trigger will hard-deny.\n"
    "\n"
    "{verbatim}"
)

_HARD_DENY_TEMPLATE = (
    "⛔ [{gate}] consecutive trigger — blocked\n"
    "\n"
    "Reason: {reason}\n"
    "Execution stopped. Change direction, switch tools, or ask for next step.\n"
    "💡 Two consecutive failures detected — run /compact or /clear to reset context.\n"
    "Polluted context causes cascading failures. Fresh start almost always works better.\n"
    "\n"
    "{verbatim}"
)

_HARD_DENY_IMMEDIATE_TEMPLATE = (
    "⛔ [{gate}] blocked\n"
    "\n"
    "Reason: {reason}\n"
    "Execution stopped. Change direction, switch tools, or ask for next step.\n"
    "\n"
    "{verbatim}"
)


def _render_step_back(gate: str, reason: str) -> str:
    """Render the visible step-back relay line through the brand layer."""
    return with_feature_prefix(
        gate,
        f"⚠ {gate}: {reason} — paused, changing approach",
    )


def _render_hard_deny(gate: str, reason: str) -> str:
    """Render the visible hard-deny relay line through the brand layer."""
    return with_feature_prefix(
        gate,
        f"⛔ Blocked: {reason} — awaiting your direction",
    )

# Valid modes
MODES = ("step_back_first", "hard_deny", "off")


# ── State Management ───────────────────────────────────────────

def _state_path(session_id: str, state_dir: str) -> str:
    safe_id = (session_id or "unknown")[:16].replace("/", "_").replace("\\", "_")
    d = os.path.join(state_dir, "step_back")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{safe_id}.json")


def _read_state(session_id: str, state_dir: str) -> dict:
    path = _state_path(session_id, state_dir)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _write_state(session_id: str, state_dir: str, state: dict) -> None:
    path = _state_path(session_id, state_dir)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception:
        pass


def clear_state(session_id: str, state_dir: str) -> None:
    """Reset step-back tracking. Call when pipeline passes all gates."""
    path = _state_path(session_id, state_dir)
    try:
        if os.path.isfile(path):
            os.remove(path)
    except Exception:
        pass


def record_global_failure(session_id: str, state_dir: str, gate_name: str) -> int:
    """Record a gate failure and return the consecutive failure count."""
    state = _read_state(session_id, state_dir)
    count = state.get("global_consecutive_failures", 0) + 1
    state["global_consecutive_failures"] = count
    state["last_failed_gate"] = gate_name
    state["last_failure_ts"] = time.time()
    _write_state(session_id, state_dir, state)
    return count


def clear_global_failures(session_id: str, state_dir: str) -> None:
    """Reset global failure count. Call when a tool call passes all gates."""
    state = _read_state(session_id, state_dir)
    if state.get("global_consecutive_failures", 0) > 0:
        state["global_consecutive_failures"] = 0
        _write_state(session_id, state_dir, state)


# ── Stderr Helpers ─────────────────────────────────────────────

def _emit_step_back_stderr(gate: str, reason: str) -> None:
    """Yellow ANSI step-back notification → stderr (user sees in VS Code)."""
    try:
        sys.stderr.write(
            f"\033[93m⚠ Step-Back: {gate} — {reason}\033[0m\n"
        )
        sys.stderr.flush()
    except Exception:
        pass


def _emit_hard_deny_stderr(gate: str, reason: str) -> None:
    """Red ANSI hard-deny notification → stderr (user sees in VS Code)."""
    try:
        sys.stderr.write(
            f"\033[91m⛔ Blocked: {gate} — {reason}\033[0m\n"
        )
        sys.stderr.flush()
    except Exception:
        pass


# ── Core API ───────────────────────────────────────────────────

def wrap_gate(
    gate_name: str,
    deny_result: dict,
    session_id: str,
    state_dir: str,
    *,
    mode: str = "step_back_first",
    reason: str = "",
    user_reason_zh: str = "",
) -> Optional[dict]:
    """Two-tier gate buffer.

    Args:
        gate_name: Gate identifier (e.g. "sentinel_gate").
        deny_result: Original make_deny() output from the gate.
        session_id: Current session ID.
        state_dir: Base directory for state files.
        mode: "step_back_first" | "hard_deny" | "off".
        reason: Human-readable reason for user-visible messages.
        user_reason_zh: Deprecated alias for reason (backward compat).

    Returns:
        None if mode="off" (gate disabled).
        allow-dict with step-back additionalContext (first trigger).
        deny-dict with enhanced messages (second trigger or hard_deny mode).
    """
    if mode not in MODES:
        mode = "step_back_first"

    # Off → skip gate entirely
    if mode == "off":
        return None

    msg = reason or user_reason_zh or deny_result.get("reason", "unknown")

    # Hard deny → always block, but enhance with user-visible messages
    if mode == "hard_deny":
        ctx = _HARD_DENY_IMMEDIATE_TEMPLATE.format(
            gate=gate_name, reason=msg,
            verbatim=_render_hard_deny(gate_name, msg),
        )
        _emit_hard_deny_stderr(gate_name, msg)
        original_reason = deny_result.get("reason", gate_name)
        return make_deny(
            original_reason,
            additionalContext=ctx,
        )

    # step_back_first → check consecutive state
    state = _read_state(session_id, state_dir)
    last_gate = state.get("last_gate", "")

    # Track global consecutive failures across all gates
    global_count = record_global_failure(session_id, state_dir, gate_name)

    if last_gate == gate_name:
        # Second consecutive same-gate trigger → hard deny
        deny_ctx = _HARD_DENY_TEMPLATE.format(
            gate=gate_name, reason=msg,
            verbatim=_render_hard_deny(gate_name, msg),
        )
        _emit_hard_deny_stderr(gate_name, msg)
        # Clear same-gate state after deny (reset for next cycle)
        _write_state(session_id, state_dir, {
            "last_gate": "",
            "last_ts": time.time(),
            "global_consecutive_failures": global_count,
            "last_failed_gate": gate_name,
            "last_failure_ts": time.time(),
        })
        original_reason = deny_result.get("reason", gate_name)
        return make_deny(
            original_reason,
            additionalContext=deny_ctx,
        )

    # First trigger → soft deny (Claude stops, rethinks, retries)
    _write_state(session_id, state_dir, {
        "last_gate": gate_name,
        "last_ts": time.time(),
        "global_consecutive_failures": global_count,
        "last_failed_gate": gate_name,
        "last_failure_ts": time.time(),
    })

    # Global 2+ failures across any gates → add compact suggestion
    if global_count >= 2:
        compact_visible = with_feature_prefix(
            gate_name,
            f"⚠ {gate_name}: {msg} "
            f"— {global_count} consecutive failures, consider /compact",
        )
        compact_ctx = (
            f"⚠ [{gate_name}] {msg}\n"
            "Stop. Change direction and retry.\n"
            "\n"
            f"💡 {global_count} consecutive failures detected across gates — "
            "run /compact or /clear to reset context.\n"
            "Polluted context causes cascading failures. "
            "Fresh start almost always works better.\n"
            "\n"
            f"{compact_visible}"
        )
        _emit_step_back_stderr(gate_name, msg)
        original_reason = deny_result.get("reason", gate_name)
        return make_deny(
            original_reason,
            additionalContext=compact_ctx,
        )

    step_ctx = _STEP_BACK_TEMPLATE.format(
        gate=gate_name, reason=msg,
        verbatim=_render_step_back(gate_name, msg),
    )
    _emit_step_back_stderr(gate_name, msg)
    original_reason = deny_result.get("reason", gate_name)
    return make_deny(
        original_reason,
        additionalContext=step_ctx,
    )
