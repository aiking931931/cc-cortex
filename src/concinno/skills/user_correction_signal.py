"""Per-turn user-correction signal for ``SkillEmergenceGuard``.

The HP2 SkillEmergenceGuard needs to know whether the *most recent
user prompt* (the one that opened the current turn) carried a
correction marker — that's trigger #3 ``user_correction``. The hook
that knows is ``on_prompt_submit`` (it has the user's text); the hook
that reads the flag is ``on_post_tool`` (it builds the
:class:`EmergenceSignal`). The two hooks run in different processes,
so we persist the flag to a single small JSON file under
``~/.concinno/state/`` and let the post-tool hook read it.

File schema (one record, latest-wins):

.. code-block:: json

    {
        "ts": 1714400000.123,
        "active": true,
        "confidence": 1.0
    }

* ``ts`` — Unix seconds when the prompt was observed.
* ``active`` — ``is_correction(user_prompt)`` result for that prompt.
* ``confidence`` — 1.0 for L1 (explicit), 0.6 for L2 (implicit),
  0.0 when no match (kept for ZIQ telemetry).

The file is overwritten on every prompt — there is no history. The
post-tool reader applies a TTL (default 30 min) so a stale signal
from yesterday's session does not poison today's first tool call.

Path override: env ``CONCINNO_USER_CORRECTION_SIGNAL_PATH`` for tests.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from concinno.knowledge import is_correction

__all__ = [
    "DEFAULT_TTL_SECONDS",
    "is_active",
    "record_prompt",
    "signal_path",
]


DEFAULT_TTL_SECONDS: int = 1800  # 30 min — covers a normal multi-tool turn


def signal_path() -> Path:
    """Return the JSON file path. Override via env for tests."""
    override = os.environ.get("CONCINNO_USER_CORRECTION_SIGNAL_PATH", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".concinno" / "state" / "user_correction_signal.json"


def record_prompt(user_prompt: str) -> None:
    """Persist whether the prompt carries a correction marker.

    Called from :func:`concinno.hooks.on_prompt_submit.handle_prompt_submit`
    once per user message. Always overwrites the file (atomic via tmp +
    replace) so the signal reflects the latest prompt only. Never
    raises — the hook is on the hot path.
    """
    active = False
    confidence = 0.0
    try:
        result = is_correction(user_prompt or "", return_confidence=True)
        # ``is_correction(..., return_confidence=True)`` returns
        # ``tuple[bool, float]`` — narrow defensively against the
        # bool-only legacy shape.
        if isinstance(result, tuple) and len(result) == 2:
            active = bool(result[0])
            confidence = float(result[1])
        elif isinstance(result, bool):
            active = result
            confidence = 1.0 if result else 0.0
    except Exception:
        # If pattern compilation breaks, fail safe (no correction).
        pass
    record = {
        "ts": time.time(),
        "active": bool(active),
        "confidence": float(confidence),
    }
    p = signal_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(
            json.dumps(record, indent=2, sort_keys=True), encoding="utf-8",
        )
        os.replace(tmp, p)
    except OSError:
        # Read-only home / permission error — silently skip. Better to
        # lose the signal than to crash the prompt-submit hook.
        return


def is_active(*, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> bool:
    """Return True if the latest prompt within TTL was a correction.

    Public helper exported via ``__all__``; consumers should call this
    after :func:`record_prompt` has stored a recent correction signal.
    Stale records (older than ``ttl_seconds``) and missing / corrupted
    files all return False — the trigger is opt-in, not load-bearing.
    """
    p = signal_path()
    if not p.exists():
        return False
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(raw, dict):
        return False
    if not raw.get("active"):
        return False
    ts = raw.get("ts")
    if not isinstance(ts, (int, float)):
        return False
    age = time.time() - float(ts)
    if age < 0 or age > float(ttl_seconds):
        return False
    return True
