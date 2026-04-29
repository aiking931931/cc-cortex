"""concinno.hooks.dedup_layer — 軌 B 件 1: hook-message dedup layer.

@module dedup_layer
@responsibility Suppress repeated identical hook injections inside a single
    Claude Code session so the LLM is not habituated to the same warning
    text appearing N times in a row. Same ``(feature, msg)`` pair from a
    given ``session_id`` is allowed through exactly once until the session
    boundary clears the cache.
@dependencies (stdlib only) — hashlib, json, os, time, pathlib
@exports should_dedup, mark_emitted, clear_session, dedup_state_path,
    is_disabled

Why this exists
---------------
Per the 2026-04-29 commander 4-channel verdict §3 軌 B, the root cause of
"the LLM ignores hook warnings" is **not** the routing channel — it is
**habituation**: the same warning text injected 50× in one session
becomes background noise. Channel routing alone cannot solve this; the
fix is dedup at the producer side (件 1) + auto-demote on chronic ignore
(件 2) + FTRL learning of true accept-rate (件 3).

This module is 件 1: the cheapest layer. Same hook + same exact message
text within the same Claude Code session ID hits :func:`should_dedup`
returning True for every call past the first, so the caller skips the
emit entirely. The producer-side cooldown means the bus never sees the
duplicate — saving downstream FTRL state churn too.

Design choices
--------------
- **Content-hash**: SHA-256 of the *normalised* message body (stripped
  of relay prefixes via :func:`_normalise`). Two callers that emit the
  same warning under different relay modes still collapse correctly.
- **Session-scoped**: state is keyed by ``(session_id, feature, hash)``.
  A new session ID flushes its slot — no cross-session leakage.
- **Time-bounded fallback**: when the caller cannot supply a session id
  (very rare), we fall back to a 5-minute rolling window keyed only on
  ``(feature, hash)``. This is safer than letting de-dup degrade to a
  no-op (which would re-introduce the habituation bug).
- **Best effort**: every disk write swallows OSError. A dedup miss is a
  cosmetic regression (one extra warning shown), never a crash.

Hard switch
-----------
Env ``CONCINNO_HABITUATION_DISABLED=1`` disables 件 1 + 件 2 + 件 3
together (one flag for the whole 軌 B). Tests use
``CONCINNO_HOOK_DEDUP_DISABLED=1`` to disable just this layer in
isolation.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

__all__ = [
    "clear_session",
    "dedup_state_path",
    "is_disabled",
    "mark_emitted",
    "should_dedup",
]


# Default 5-minute rolling window for the session-less fallback path.
_FALLBACK_TTL_SECONDS: float = 300.0

# How many distinct (session, feature, hash) entries we keep in memory
# before evicting the oldest. 4096 is generous — a typical Concinno hot
# session emits <500 distinct warnings.
_MAX_ENTRIES: int = 4096


# ── Hard switches ───────────────────────────────────────────────


def is_disabled() -> bool:
    """Return True when 軌 B Habituation is disabled at the env layer.

    Two env flags both opt out:

    1. ``CONCINNO_HABITUATION_DISABLED`` — kills the whole 軌 B (件 1
       + 件 2 + 件 3 together).
    2. ``CONCINNO_HOOK_DEDUP_DISABLED`` — kills only 件 1 (used by tests
       that want to assert auto-demote / FTRL behaviour without dedup
       collapsing the test fixtures).

    Both are read fresh on every call so a test ``monkeypatch.setenv``
    flips behaviour without re-importing.
    """
    if os.environ.get("CONCINNO_HABITUATION_DISABLED", "").strip() in {
        "1", "true", "yes", "on",
    }:
        return True
    if os.environ.get("CONCINNO_HOOK_DEDUP_DISABLED", "").strip() in {
        "1", "true", "yes", "on",
    }:
        return True
    return False


# ── State file ──────────────────────────────────────────────────


def dedup_state_path() -> Path:
    """Resolve the on-disk state file. Override via env for tests."""
    override = os.environ.get("CONCINNO_HOOK_DEDUP_STATE_PATH", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".concinno" / "state" / "hook_dedup_session.json"


def _load_state() -> dict[str, Any]:
    p = dedup_state_path()
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return raw


def _save_state(data: dict[str, Any]) -> None:
    p = dedup_state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp, p)
    except OSError:
        # Read-only home / permission error → silently degrade. Hook
        # warnings stay correct, dedup just stops persisting.
        return


# ── Hashing ─────────────────────────────────────────────────────


_VERBATIM_TOKEN = "[SHOW USER VERBATIM]"


def _normalise(msg: str) -> str:
    """Strip relay-layer wrappers so two modes hash identically.

    The verbatim_relay layer can render the same payload under
    ``[SHOW USER VERBATIM] [Concinno: feat] body`` or just ``body``.
    Both must collapse for dedup to be useful — we hash on the raw body
    only.
    """
    if not msg:
        return ""
    s = msg.strip()
    if s.startswith(_VERBATIM_TOKEN):
        s = s[len(_VERBATIM_TOKEN):].lstrip()
    if s.startswith("[Concinno:"):
        end = s.find("]")
        if end != -1:
            s = s[end + 1:].lstrip()
    return s


def _hash(feature_name: str, msg: str) -> str:
    h = hashlib.sha256()
    h.update(feature_name.encode("utf-8"))
    h.update(b"\x00")
    h.update(_normalise(msg).encode("utf-8"))
    return h.hexdigest()[:32]


def _entry_key(session_id: str, feature_name: str, msg_hash: str) -> str:
    sid = session_id or "_no_session_"
    return f"{sid[:32]}::{feature_name}::{msg_hash}"


# ── Eviction ────────────────────────────────────────────────────


def _evict_if_full(data: dict[str, Any]) -> None:
    """Drop oldest entries when the cache exceeds ``_MAX_ENTRIES``."""
    if len(data) <= _MAX_ENTRIES:
        return
    # Sort by stored timestamp (oldest first) and evict until under cap.
    items = sorted(
        data.items(),
        key=lambda kv: float(kv[1].get("ts", 0.0))
        if isinstance(kv[1], dict)
        else 0.0,
    )
    overflow = len(data) - _MAX_ENTRIES
    for k, _v in items[:overflow]:
        data.pop(k, None)


# ── Public API ──────────────────────────────────────────────────


def should_dedup(
    feature_name: str,
    msg: str,
    *,
    session_id: str = "",
    ttl_seconds: float = _FALLBACK_TTL_SECONDS,
) -> bool:
    """Return True when ``(feature_name, msg)`` was already emitted.

    Args:
        feature_name: Source feature key (e.g. ``"cbua_pipeline"``).
        msg: The warning body the caller is about to relay.
        session_id: Claude Code session id. Empty string falls back to
            a TTL-bounded global window (rarely hit — almost every hook
            has a session id).
        ttl_seconds: Fallback-window length for the session-less path.

    Returns:
        ``True`` when the caller should skip the emit; ``False`` when
        the emit should proceed (and the caller should follow up with
        :func:`mark_emitted` to record the new fingerprint).

    Best effort: any internal failure (state file unreadable, etc.)
    returns ``False`` so the warning still gets through. Suppressing a
    real warning because dedup state was corrupt is worse than showing
    a duplicate.
    """
    if is_disabled():
        return False
    if not feature_name or not msg:
        return False

    try:
        msg_hash = _hash(feature_name, msg)
        key = _entry_key(session_id, feature_name, msg_hash)
        data = _load_state()
        entry = data.get(key)
        if not isinstance(entry, dict):
            return False
        if not session_id:
            # Session-less path: honour TTL.
            ts = entry.get("ts")
            if not isinstance(ts, (int, float)):
                return False
            if (time.time() - float(ts)) > ttl_seconds:
                return False
        return True
    except Exception:
        return False


def mark_emitted(
    feature_name: str,
    msg: str,
    *,
    session_id: str = "",
) -> None:
    """Record that ``(feature_name, msg)`` just fired.

    Call immediately after the relay actually emits — never before, so
    a failed emit does not poison future dedup decisions.

    Best effort: silently swallows IO errors.
    """
    if is_disabled() or not feature_name or not msg:
        return
    try:
        msg_hash = _hash(feature_name, msg)
        key = _entry_key(session_id, feature_name, msg_hash)
        data = _load_state()
        data[key] = {
            "ts": time.time(),
            "feature": feature_name,
            "session_id": session_id or "",
            "hash": msg_hash,
        }
        _evict_if_full(data)
        _save_state(data)
    except Exception:
        return


def clear_session(session_id: str) -> None:
    """Drop every dedup record for ``session_id``.

    Hooked into ``on_session_start`` so a brand-new session ID always
    starts with a clean slate. Tests use this to assert the
    session-boundary semantics.
    """
    if not session_id:
        return
    try:
        data = _load_state()
        prefix = f"{session_id[:32]}::"
        keys = [k for k in data if k.startswith(prefix)]
        if not keys:
            return
        for k in keys:
            data.pop(k, None)
        _save_state(data)
    except Exception:
        return
