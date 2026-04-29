"""concinno.ziq_hook_ignore_rate — 軌 B 件 3: per-hook FTRL ignore-rate.

@module ziq_hook_ignore_rate
@responsibility Per-hook online learner that tracks the rate at which the
    LLM *accepts* (acts on) vs *ignores* a hook fire. Outcome signal is
    the next-turn user-correction state per the F7 fix in the 2026-04-29
    commander 4-channel verdict — using next-turn user correction (=0.0
    reward) vs silence/acknowledgement (=1.0 reward) avoids the Goodhart
    inflation that "behavior shifted" framing would create.
@dependencies concinno.ziq_emit_helpers, concinno.ziq_outcome_bus
@exports record_emit, record_user_accept_signal, hook_accept_rate,
    sync_outcome_to_bus, ftrl_state_path, NAMESPACE, is_disabled

Why this exists
---------------
Per the 2026-04-29 commander 4-channel verdict §3 軌 B 件 3 + Hermes
4-cap §E.1 reconciliation, the **fifth** ZIQ outcome namespace
``ziq.outcome.hook_ignore_rate`` is shared across multiple producers:

* 軌 A ``verbatim_relay`` — display vs strip outcome
* 軌 B ``dedup_layer`` — dedup hit / miss
* 軌 B ``auto_demote`` — tier-demote / tier-restore transitions
* §6 ``WiredoSubagentVerifyGuard`` — verifier pass / fail / overrule
* §C reliability FTRL — per-agent reliability prior
* 軌 C future — hook fire-rate optimizer

This module owns the state file
``~/.concinno/state/ziq_hook_ignore_rate.json`` (override via env
``CONCINNO_ZIQ_HOOK_IGNORE_RATE_PATH``) and the per-feature FTRL weight
update on every emit. Producers stream outcomes through
:func:`record_emit` (or via :mod:`concinno.ziq_emit_helpers` which
already wires the bus); :func:`record_user_accept_signal` collapses the
prompt-submit observation (next-turn user correction = 0.0 reward, else
1.0) into per-feature weight updates for every emit currently waiting
on its accept verdict.

Six-condition gate (ZIQ applicability per `kb_ziq`)
---------------------------------------------------
1. finite options ✓ — 4 tiers × N hook surfaces = bounded (auto-demote)
2. structural prior ✓ — SPS = LLM behaviour-shift signal pattern
3. measurable outcome ✓ — user-accept binary signal (per F7 fix)
4. Markov ✓ — per-hook independent (no cross-feature coupling)
5. stable environment ✓ — hook fire pattern relatively stable per session
6. sufficient sample ✓ — high-frequency hooks easily 100+ fire / session

All 6 pass → FTRL is an appropriate learner for this surface.

Hard switch
-----------
``CONCINNO_HABITUATION_DISABLED=1`` disables the whole 軌 B together;
``CONCINNO_ZIQ_HOOK_IGNORE_RATE_DISABLED=1`` disables only this layer.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

__all__ = [
    "NAMESPACE",
    "ftrl_state_path",
    "hook_accept_rate",
    "is_disabled",
    "pending_emits",
    "record_emit",
    "record_user_accept_signal",
    "sync_outcome_to_bus",
]


#: Shared ZIQ outcome namespace per Hermes 4-cap §E.1 reconciliation.
NAMESPACE: str = "ziq.outcome.hook_ignore_rate"


# ── Hyperparameters ────────────────────────────────────────────


# FTRL EMA-style decay — per-feature weight smoothes over recent samples.
# alpha=0.1 keeps the learner responsive to fresh signal without
# over-fitting any single fire. decay=0.99 lets ancient samples bleed
# out so a feature that *used* to be ignored gets a fair shake when its
# implementation improves. Values mirror Hermes 4-cap §F default.
_DEFAULT_ALPHA: float = 0.1
_DEFAULT_DECAY: float = 0.99

# Pending-emit window — how long an emit waits for a user-accept verdict
# before defaulting to "ignored" (assumption: silence past TTL = LLM did
# not act on it, so the warning failed to steer behaviour).
_DEFAULT_PENDING_TTL_SECONDS: float = 1800.0  # 30 min


# ── Hard switches ───────────────────────────────────────────────


def is_disabled() -> bool:
    """Return True when 軌 B 件 3 FTRL is disabled at the env layer."""
    if os.environ.get("CONCINNO_HABITUATION_DISABLED", "").strip() in {
        "1", "true", "yes", "on",
    }:
        return True
    if os.environ.get(
        "CONCINNO_ZIQ_HOOK_IGNORE_RATE_DISABLED", "",
    ).strip() in {
        "1", "true", "yes", "on",
    }:
        return True
    return False


# ── State file ──────────────────────────────────────────────────


def ftrl_state_path() -> Path:
    """Resolve the FTRL state file. Override via env for tests."""
    override = os.environ.get(
        "CONCINNO_ZIQ_HOOK_IGNORE_RATE_PATH", "",
    ).strip()
    if override:
        return Path(override)
    return Path.home() / ".concinno" / "state" / "ziq_hook_ignore_rate.json"


def _empty_state() -> dict[str, Any]:
    return {
        "weights": {},          # feature -> EMA accept-rate
        "sample_count": {},     # feature -> total samples seen
        "pending": [],          # list of pending {feature, ts, value}
        "last_update": "",
        "alpha": _DEFAULT_ALPHA,
        "decay": _DEFAULT_DECAY,
    }


def _load() -> dict[str, Any]:
    p = ftrl_state_path()
    if not p.exists():
        return _empty_state()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_state()
    if not isinstance(raw, dict):
        return _empty_state()
    # Backfill missing keys for forward-compat.
    for k, v in _empty_state().items():
        raw.setdefault(k, v)
    return raw


def _save(data: dict[str, Any]) -> None:
    p = ftrl_state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp, p)
    except OSError:
        return


# ── Hyperparameter resolution ───────────────────────────────────


def _resolve_alpha() -> float:
    return _resolve_float(
        env_key="CONCINNO_HABITUATION_FTRL_ALPHA",
        cfg_key="alpha",
        default=_DEFAULT_ALPHA,
    )


def _resolve_decay() -> float:
    return _resolve_float(
        env_key="CONCINNO_HABITUATION_FTRL_DECAY",
        cfg_key="decay",
        default=_DEFAULT_DECAY,
    )


def _resolve_float(*, env_key: str, cfg_key: str, default: float) -> float:
    """Pull a float from FEATURE_META + env, ignoring malformed values."""
    value = default
    try:  # pragma: no cover - exercised via integration
        from concinno.core.config import get_config

        cfg_val = get_config().feature(
            "habituation_ignore_rate_ftrl", cfg_key,
        )
        if isinstance(cfg_val, (int, float)) and 0.0 < float(cfg_val) <= 1.0:
            value = float(cfg_val)
    except Exception:
        pass
    raw = os.environ.get(env_key, "").strip()
    if raw:
        try:
            v = float(raw)
            if 0.0 < v <= 1.0:
                value = v
        except ValueError:
            pass
    return value


# ── Public API ──────────────────────────────────────────────────


def record_emit(feature_name: str, *, session_id: str = "") -> None:
    """Record that a hook just emitted; queue a pending accept-verdict.

    The verdict resolves later — either when
    :func:`record_user_accept_signal` is called for the next turn (the
    signal we trust per F7), or when the pending entry's TTL expires
    and we default to "ignored". The pending list is kept short by
    flushing TTL'd entries on every call.
    """
    if is_disabled() or not feature_name:
        return
    try:
        data = _load()
        pending = data.get("pending", [])
        if not isinstance(pending, list):
            pending = []
        # Trim TTL'd pendings into "ignored" (reward 0.0) before recording.
        pending = _flush_ttl_pendings(data, pending)
        pending.append({
            "feature": feature_name,
            "session_id": session_id or "",
            "ts": time.time(),
        })
        data["pending"] = pending
        _save(data)
    except Exception:
        return


def record_user_accept_signal(
    *,
    user_corrected: bool,
    session_id: str = "",
) -> None:
    """Resolve every pending emit using the next-turn user-correction signal.

    Per F7 fix the outcome is the **user**-side signal (not LLM behavior
    shift): if the user came back and corrected, every warning that fired
    in the previous turn earned a 0.0 reward (it failed to steer). If the
    user stayed silent / acknowledged, every warning earned 1.0.

    This is the single hot-path called from
    :mod:`concinno.hooks.on_prompt_submit` once per turn. The
    ``session_id`` filters the resolution to the current session so
    cross-session prompts do not mis-attribute outcomes.
    """
    if is_disabled():
        return
    reward = 0.0 if user_corrected else 1.0
    try:
        data = _load()
        pending = data.get("pending", [])
        if not isinstance(pending, list) or not pending:
            return
        alpha = _resolve_alpha()
        decay = _resolve_decay()
        weights = data.setdefault("weights", {})
        if not isinstance(weights, dict):
            weights = {}
            data["weights"] = weights
        sample_count = data.setdefault("sample_count", {})
        if not isinstance(sample_count, dict):
            sample_count = {}
            data["sample_count"] = sample_count

        kept: list[dict[str, Any]] = []
        for entry in pending:
            if not isinstance(entry, dict):
                continue
            feature = entry.get("feature")
            entry_session = entry.get("session_id", "")
            if not isinstance(feature, str) or not feature:
                continue
            if session_id and entry_session and entry_session != session_id:
                # Different session — keep waiting.
                kept.append(entry)
                continue
            # Apply FTRL EMA update for this feature.
            old = float(weights.get(feature, 0.5))
            new = decay * old + alpha * reward
            # Clamp to [0, 1] so a numeric drift does not poison consumers.
            new = max(0.0, min(1.0, new))
            weights[feature] = new
            sample_count[feature] = int(sample_count.get(feature, 0)) + 1

        data["pending"] = kept
        data["last_update"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(),
        )
        _save(data)
        # Mirror the per-feature outcome into the shared ZIQ bus so any
        # registered FTRL consumer (auto-demote tuner, future tier
        # auto-router) gets the signal in real time.
        try:
            sync_outcome_to_bus(weights.keys(), reward)
        except Exception:
            pass
    except Exception:
        return


def _flush_ttl_pendings(
    data: dict[str, Any],
    pending: list[dict[str, Any]],
    ttl_seconds: float = _DEFAULT_PENDING_TTL_SECONDS,
) -> list[dict[str, Any]]:
    """Drop pendings older than ``ttl_seconds`` and treat them as ignored.

    Modifies ``data`` weights/sample_count in place. Returns the trimmed
    pending list — caller is responsible for assigning it back into
    ``data["pending"]``.
    """
    now = time.time()
    alpha = _resolve_alpha()
    decay = _resolve_decay()
    weights = data.setdefault("weights", {})
    sample_count = data.setdefault("sample_count", {})
    if not isinstance(weights, dict):
        weights = {}
        data["weights"] = weights
    if not isinstance(sample_count, dict):
        sample_count = {}
        data["sample_count"] = sample_count
    fresh: list[dict[str, Any]] = []
    for entry in pending:
        if not isinstance(entry, dict):
            continue
        ts = entry.get("ts")
        feature = entry.get("feature")
        if not isinstance(ts, (int, float)) or not isinstance(feature, str):
            continue
        age = now - float(ts)
        if age > ttl_seconds:
            # TTL expired with no user-accept signal → treat as ignore.
            old = float(weights.get(feature, 0.5))
            new = decay * old + alpha * 0.0
            weights[feature] = max(0.0, min(1.0, new))
            sample_count[feature] = int(sample_count.get(feature, 0)) + 1
        else:
            fresh.append(entry)
    return fresh


def hook_accept_rate(feature_name: str) -> float:
    """Return the EMA accept-rate (0.0–1.0) for ``feature_name``.

    Returns ``0.5`` (uninformed prior) when no samples exist yet; this
    is the same value used inside the EMA seed so a zero-sample feature
    behaves indistinguishably from a freshly-seeded one.
    """
    if is_disabled() or not feature_name:
        return 0.5
    try:
        weights = _load().get("weights", {})
        if not isinstance(weights, dict):
            return 0.5
        return float(weights.get(feature_name, 0.5))
    except Exception:
        return 0.5


def pending_emits() -> list[dict[str, Any]]:
    """Return the current pending-verdict queue (dev / test helper)."""
    if is_disabled():
        return []
    try:
        pending = _load().get("pending", [])
        return list(pending) if isinstance(pending, list) else []
    except Exception:
        return []


def sync_outcome_to_bus(
    feature_names: Iterable[str],
    reward: float,
) -> None:
    """Mirror per-feature reward into the shared ZIQ outcome bus.

    Best-effort; the bus may be hard-killed via
    ``CONCINNO_ZIQ_BUS_DISABLED=1`` and that is fine — the local FTRL
    state still updates, only the bus subscribers miss the event.
    """
    try:
        from concinno.ziq_outcome_bus import Outcome, get_bus

        bus = get_bus()
        for feat in feature_names:
            if not isinstance(feat, str) or not feat:
                continue
            try:
                bus.emit(
                    Outcome(
                        tunable=NAMESPACE,
                        value=feat,
                        reward=float(reward),
                        source="concinno.ziq_hook_ignore_rate",
                        metadata={"feature": feat},
                    )
                )
            except Exception:
                continue
    except Exception:
        return
