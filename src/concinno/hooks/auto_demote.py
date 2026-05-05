"""concinno.hooks.auto_demote — 軌 B 件 2: per-hook tier auto-demote.

@module auto_demote
@responsibility When a hook has fired N consecutive times without the LLM
    acting on it (the next-turn user-correction signal stays silent
    despite the warning), automatically lower the surface tier so the
    chronic-fail signal stops competing with novel ones for LLM
    attention budget.
@dependencies (stdlib only) — json, os, pathlib, time
@exports current_tier, record_ignore, record_accept, reset, demote_state_path,
    is_disabled, TIERS

Why this exists
---------------
Per the 2026-04-29 commander 4-channel verdict §3 軌 B 件 2: dedup (件 1)
removes verbatim duplicates, but the same hook re-emitting *different*
messages 10× in a row is still habituation. Auto-demote is the second
layer — once we observe N consecutive *ignored* fires (LLM saw the
warning but did not change behaviour), we silently lower the tier:

- ``CRITICAL`` (initial) — full ``[SHOW USER VERBATIM]`` injection
- ``HIGH``                — same channel, but caller may shorten body
- ``NORMAL``              — display only, no panic prefix
- ``SILENT_LOG``          — write to stderr log only, do not inject

Each ``record_accept`` resets the counter (the warning *did* steer
behaviour, so the surface is earning its tier). Per L0 鐵律 #6
ZIQ-vs-manual priority, the threshold ``ignore_threshold`` is
ZIQ-tunable (FTRL learns the per-feature optimum) but the user can
still pin it via ``cfg.feature('habituation_auto_demote',
'ignore_threshold')``.

State file
----------
``~/.concinno/state/hook_demote_state.json`` (override via env
``CONCINNO_HOOK_DEMOTE_STATE_PATH`` for tests) — schema::

    {
      "<feature>": {
        "tier": "CRITICAL" | "HIGH" | "NORMAL" | "SILENT_LOG",
        "consecutive_ignores": int,
        "last_update": <unix-seconds>
      },
      ...
    }

Hard switch
-----------
Env ``CONCINNO_HABITUATION_DISABLED=1`` disables the whole 軌 B (件 1
+ 件 2 + 件 3 together). Env ``CONCINNO_HOOK_AUTO_DEMOTE_DISABLED=1``
disables only this layer.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

__all__ = [
    "TIERS",
    "current_tier",
    "demote_state_path",
    "is_disabled",
    "record_accept",
    "record_ignore",
    "reset",
]


# Tier ladder — index = severity, lower index = louder.
TIERS: tuple[str, ...] = ("CRITICAL", "HIGH", "NORMAL", "SILENT_LOG")
_TIER_INDEX: dict[str, int] = {t: i for i, t in enumerate(TIERS)}

# Default consecutive-ignore threshold before a single tier step. Per
# verdict §3 件 2 the canonical value is 3; ZIQ FTRL may auto-tune it
# per-feature in 4.7+. The threshold is read fresh on every call so an
# operator config flip takes effect on the next emit.
_DEFAULT_THRESHOLD: int = 3


# ── Hard switches ───────────────────────────────────────────────


def is_disabled() -> bool:
    """Return True when 軌 B Habituation auto-demote is disabled.

    Two env flags both opt out:

    1. ``CONCINNO_HABITUATION_DISABLED`` — kills the whole 軌 B.
    2. ``CONCINNO_HOOK_AUTO_DEMOTE_DISABLED`` — kills only this layer.
    """
    if os.environ.get("CONCINNO_HABITUATION_DISABLED", "").strip() in {
        "1", "true", "yes", "on",
    }:
        return True
    if os.environ.get("CONCINNO_HOOK_AUTO_DEMOTE_DISABLED", "").strip() in {
        "1", "true", "yes", "on",
    }:
        return True
    return False


# ── State file ──────────────────────────────────────────────────


def demote_state_path() -> Path:
    """Resolve the demote-state file. Override via env for tests."""
    override = os.environ.get("CONCINNO_HOOK_DEMOTE_STATE_PATH", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".concinno" / "state" / "hook_demote_state.json"


def _load() -> dict[str, Any]:
    p = demote_state_path()
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return raw


def _save(data: dict[str, Any]) -> None:
    p = demote_state_path()
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


# ── Threshold resolution ────────────────────────────────────────


def _resolve_threshold() -> int:
    """Resolve the consecutive-ignore threshold.

    Order (later wins): hard-coded default → FEATURE_META → user config →
    env override. Soft imports keep the module standalone for tests that
    bypass the config layer entirely.
    """
    threshold = _DEFAULT_THRESHOLD

    # FEATURE_META + user config layer (lazy — never crash on partial install).
    try:  # pragma: no cover - covered indirectly via integration tests
        from concinno.core.config import get_config

        cfg_val = get_config().feature(
            "habituation_auto_demote", "ignore_threshold",
        )
        if isinstance(cfg_val, (int, float)) and cfg_val > 0:
            threshold = int(cfg_val)
    except Exception:
        pass

    # Env override has the highest priority — operator's emergency knob.
    raw = os.environ.get("CONCINNO_HABITUATION_IGNORE_THRESHOLD", "").strip()
    if raw:
        try:
            v = int(raw)
            if v > 0:
                threshold = v
        except ValueError:
            pass
    return threshold


# ── Public API ──────────────────────────────────────────────────


def current_tier(feature_name: str) -> str:
    """Return the current tier for ``feature_name`` (default ``CRITICAL``).

    Args:
        feature_name: Source feature key (matches FEATURE_META where one
            exists; free-form strings are accepted for ad-hoc features).

    Returns:
        One of :data:`TIERS`. ``"CRITICAL"`` when:

        * 軌 B is disabled (``is_disabled()`` returns True), or
        * the feature has no recorded state yet, or
        * the state file is unreadable.
    """
    if is_disabled() or not feature_name:
        return "CRITICAL"
    try:
        entry = _load().get(feature_name)
        if not isinstance(entry, dict):
            return "CRITICAL"
        tier = entry.get("tier")
        if isinstance(tier, str) and tier in _TIER_INDEX:
            return tier
        return "CRITICAL"
    except Exception:
        return "CRITICAL"


def record_ignore(feature_name: str) -> str:
    """Record an LLM-ignored fire and return the (possibly new) tier.

    Increments the per-feature ``consecutive_ignores`` counter. When it
    reaches the resolved threshold, the tier steps down one rung
    (``CRITICAL → HIGH → NORMAL → SILENT_LOG``) and the counter resets
    to 0 — so the next ``threshold`` ignores will demote *again*, never
    flapping.

    No-op when 軌 B is disabled or the feature name is empty.

    Returns:
        The tier *after* the update — callers can branch on this to
        decide whether to skip emit, shorten body, etc.
    """
    if is_disabled() or not feature_name:
        return current_tier(feature_name)
    threshold = _resolve_threshold()
    try:
        data = _load()
        entry = data.get(feature_name)
        if not isinstance(entry, dict):
            entry = {"tier": "CRITICAL", "consecutive_ignores": 0}
        tier = entry.get("tier", "CRITICAL")
        if tier not in _TIER_INDEX:
            tier = "CRITICAL"
        ignores = int(entry.get("consecutive_ignores", 0)) + 1

        if ignores >= threshold:
            idx = _TIER_INDEX[tier]
            new_idx = min(idx + 1, len(TIERS) - 1)
            tier = TIERS[new_idx]
            ignores = 0  # reset after demotion

        entry["tier"] = tier
        entry["consecutive_ignores"] = ignores
        entry["last_update"] = time.time()
        data[feature_name] = entry
        _save(data)
        return tier
    except Exception:
        return current_tier(feature_name)


def record_accept(feature_name: str) -> str:
    """Record an LLM-accepted fire (the warning steered behaviour).

    Resets the consecutive-ignore counter to 0. Does **not** automatically
    promote the tier back up — once chronic ignore has been observed, the
    surface stays at the demoted tier until the operator explicitly
    :func:`reset` it. Promotion is intentional: yo-yo demote/promote
    would cost more attention than it saved.

    Returns:
        The current tier (unchanged by accept — only the counter resets).
    """
    if is_disabled() or not feature_name:
        return current_tier(feature_name)
    try:
        data = _load()
        entry = data.get(feature_name)
        if not isinstance(entry, dict):
            entry = {"tier": "CRITICAL", "consecutive_ignores": 0}
        entry["consecutive_ignores"] = 0
        entry["last_update"] = time.time()
        data[feature_name] = entry
        _save(data)
        return entry.get("tier", "CRITICAL") or "CRITICAL"
    except Exception:
        return current_tier(feature_name)


def reset(feature_name: str | None = None) -> None:
    """Manually clear demote state.

    Args:
        feature_name: When given, drop only that feature's row. When
            ``None``, wipe the whole state file (dev / test helper).
    """
    try:
        if feature_name is None:
            data: dict[str, Any] = {}
        else:
            data = _load()
            data.pop(feature_name, None)
        _save(data)
    except Exception:
        return
