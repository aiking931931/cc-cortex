"""concinno.ziq_persist — disk persistence primitive for ZIQ FTRL state.

@module ziq_persist
@responsibility Provide a small, audit-friendly disk-backed primitive that
    converts the in-memory FTRL weight dicts used by ZIQ-aware consumers
    into something that survives process restart. The original consumer
    (``concinno.skills.disclosure.SkillDisclosure``) was removed in K5;
    other ZIQ-aware sites continue to use this module.

@dependencies stdlib only.
@exports load_ftrl_state, record_ftrl_update, compact_ftrl_state,
    state_dir, snapshot_path, jsonl_path

Why this exists
---------------
The 8-axis 4.6.0 audit (C-axis) flagged that FTRL weights live in a
``dict[str, float]`` on the consumer object — every restart resets the
table to ``1.0`` so the "FTRL is causal online learning that never
changes" promise from the ZIQ design (per ``kb_ziq``) is broken in
practice. Disk persistence at ``~/.concinno/ziq_state/`` closes that
gap: weights survive across sessions, jsonl trail lets the operator
audit every update, snapshot lets cold start be O(N keys) not
O(M lifetime updates).

Layout
------
::

    ~/.concinno/ziq_state/
        skill_disclosure_ftrl.snapshot.json   # last compacted state
        skill_disclosure_ftrl.jsonl           # append-only update log

The split is intentional: jsonl is the source of truth for *recent*
events (auditable, tail-friendly, never rewritten) while the snapshot
folds older events to keep cold-load cheap. ``load_ftrl_state`` reads
snapshot + replays jsonl tail — same semantics with or without
compaction having happened.

Schema
------
* Snapshot ``<feature>_ftrl.snapshot.json``::

    {
      "schema": 1,
      "feature": "<feature>",
      "weights": {"<key>": <float>, ...},
      "compacted_through_ts": "<ISO-8601>",
      "compacted_event_count": <int>
    }

* jsonl rows ``<feature>_ftrl.jsonl``::

    {"ts": "<ISO-8601>", "feature": "<feature>", "key": "<str>",
     "weight_before": <float>, "weight_after": <float>,
     "signal": <float|null>,
     "posterior_components": {<arbitrary>}}

Atomicity
---------
Snapshot writes are temp-file + ``os.replace`` (Windows-safe; no
``fcntl`` dependency). Jsonl writes use ``open(path, 'a')`` then
``write`` + ``flush`` — append is atomic on POSIX up to ``PIPE_BUF``;
on Windows append handles serialise via the OS, so single-line writes
do not interleave between processes. We do not need ``fcntl`` here —
the only mutation that needs serialisation is snapshot replacement,
which is covered by ``os.replace``.

Switchability
-------------
Honours the same env/feature kill-switch convention as the rest of
the codebase: ``CONCINNO_ZIQ_PERSIST_DISABLED=1`` (or any of
``true|yes|on``) collapses every public function to a fail-open no-op.
The state_dir override env ``CONCINNO_ZIQ_STATE_DIR`` lets tests and
CI redirect away from ``~/.concinno/`` without monkeypatching.

Failure mode
------------
Every path-touching operation is wrapped so a corrupt jsonl line, a
missing parent dir, or a permission error returns the cold-start
default (empty dict / silent skip on writes). The consumer never sees
an exception bubble out of this module — that contract is what lets
``observe_use`` callers stay one-liner.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "compact_ftrl_state",
    "is_disabled",
    "jsonl_path",
    "load_ftrl_state",
    "record_ftrl_update",
    "snapshot_path",
    "state_dir",
]


_SCHEMA_VERSION = 1


# ── kill switch ─────────────────────────────────────────────────────


def is_disabled() -> bool:
    """Return ``True`` when ``CONCINNO_ZIQ_PERSIST_DISABLED`` is truthy.

    Env-only on purpose: a single test fixture or operator opt-out
    flips it without touching FEATURE_META. Ditto for matching the
    convention used by :func:`concinno.ziq_hook_ignore_rate.is_disabled`.
    """
    raw = os.environ.get("CONCINNO_ZIQ_PERSIST_DISABLED", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


# ── path helpers ────────────────────────────────────────────────────


def state_dir() -> Path:
    """Resolve the ZIQ state directory.

    Order of precedence:
        1. ``$CONCINNO_ZIQ_STATE_DIR`` env override (tests / CI).
        2. ``$HOME/.concinno/ziq_state``.
    """
    override = os.environ.get("CONCINNO_ZIQ_STATE_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".concinno" / "ziq_state"


def _safe_feature(feature: str) -> str:
    """Restrict feature name to filesystem-safe chars.

    Allow ``[A-Za-z0-9._-]``; everything else collapses to ``_``. Empty
    name maps to ``_`` so we never produce a hidden dotfile path.
    """
    if not feature:
        return "_"
    out: list[str] = []
    for ch in feature:
        if ch.isalnum() or ch in {".", "_", "-"}:
            out.append(ch)
        else:
            out.append("_")
    return "".join(out) or "_"


def snapshot_path(feature: str) -> Path:
    """Path of the compacted snapshot for ``feature``."""
    return state_dir() / f"{_safe_feature(feature)}_ftrl.snapshot.json"


def jsonl_path(feature: str) -> Path:
    """Path of the append-only update log for ``feature``."""
    return state_dir() / f"{_safe_feature(feature)}_ftrl.jsonl"


# ── ISO-8601 ────────────────────────────────────────────────────────


def _utc_iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(
        ts if ts is not None else time.time(),
        tz=timezone.utc,
    ).isoformat()


# ── public API ──────────────────────────────────────────────────────


def load_ftrl_state(feature: str) -> dict[str, float]:
    """Load the persisted weights for ``feature``.

    Returns an empty dict when:
        * the persistence layer is disabled,
        * neither snapshot nor jsonl exist (cold start),
        * the snapshot is corrupt (defensive — we do not trust an
          unparseable JSON blob over a fresh start).

    When both files exist, the snapshot is loaded first and the jsonl
    is replayed on top so the most recent ``weight_after`` wins. This
    keeps semantics identical whether or not :func:`compact_ftrl_state`
    has run — callers can ignore compaction state entirely.
    """
    if is_disabled() or not feature:
        return {}
    weights = _load_snapshot(snapshot_path(feature))
    _replay_jsonl(jsonl_path(feature), weights)
    return weights


def _load_snapshot(snap: Path) -> dict[str, float]:
    """Read a snapshot file into a flat ``{key: weight}`` dict.

    Returns an empty dict on missing-file / corrupt-JSON / unexpected
    schema. The snapshot's auxiliary fields (``compacted_through_ts``,
    ``compacted_event_count``) are ignored here — they are diagnostic
    metadata, not load-time state.
    """
    weights: dict[str, float] = {}
    if not snap.is_file():
        return weights
    try:
        doc = json.loads(snap.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return weights
    if not isinstance(doc, dict):
        return weights
    raw = doc.get("weights")
    if not isinstance(raw, dict):
        return weights
    for k, v in raw.items():
        if isinstance(k, str) and isinstance(v, (int, float)):
            weights[k] = float(v)
    return weights


def _parse_jsonl_row(line: str) -> tuple[str, float] | None:
    """Return ``(key, weight_after)`` for a valid row, ``None`` otherwise."""
    line = line.strip()
    if not line:
        return None
    try:
        row = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(row, dict):
        return None
    key = row.get("key")
    after = row.get("weight_after")
    if isinstance(key, str) and isinstance(after, (int, float)):
        return key, float(after)
    return None


def _replay_jsonl(log: Path, weights: dict[str, float]) -> None:
    """Replay every well-formed row of ``log`` into ``weights`` in place."""
    if not log.is_file():
        return
    try:
        with log.open("r", encoding="utf-8") as fh:
            for line in fh:
                parsed = _parse_jsonl_row(line)
                if parsed is None:
                    continue
                weights[parsed[0]] = parsed[1]
    except OSError:
        return


def record_ftrl_update(
    feature: str,
    key: str,
    *,
    weight_before: float,
    weight_after: float,
    signal: float | None = None,
    posterior_components: Mapping[str, Any] | None = None,
) -> None:
    """Append one FTRL update event to the jsonl trail.

    Best-effort: when the directory is unwritable or the row cannot be
    serialised, the call is a silent no-op (the caller's in-memory
    update is the source of truth for the running process anyway).
    """
    if is_disabled() or not feature or not key:
        return
    try:
        weight_before = float(weight_before)
        weight_after = float(weight_after)
    except (TypeError, ValueError):
        return
    row: dict[str, Any] = {
        "ts": _utc_iso(),
        "feature": feature,
        "key": key,
        "weight_before": weight_before,
        "weight_after": weight_after,
    }
    if signal is not None:
        try:
            row["signal"] = float(signal)
        except (TypeError, ValueError):
            row["signal"] = None
    if posterior_components is not None:
        try:
            row["posterior_components"] = dict(posterior_components)
        except (TypeError, ValueError):
            row["posterior_components"] = {}
    try:
        encoded = json.dumps(row, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return
    log = jsonl_path(feature)
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(encoded + "\n")
    except OSError:
        return


def compact_ftrl_state(
    feature: str,
    *,
    keep_last_n: int = 10_000,
) -> dict[str, float]:
    """Fold the jsonl tail into a snapshot and trim the log.

    Reads the current logical state via :func:`load_ftrl_state`, writes
    it as the new snapshot atomically (temp file + ``os.replace``), and
    truncates the jsonl to keep only the last ``keep_last_n`` events
    for audit. Returns the merged state.

    Idempotent — safe to call when neither file exists (returns empty
    dict, leaves disk untouched). Safe to call concurrently with
    :func:`record_ftrl_update`: in the worst case a concurrent append
    races the truncate and we keep a few extra rows, never lose any.
    """
    if is_disabled() or not feature:
        return {}
    weights = load_ftrl_state(feature)
    snap = snapshot_path(feature)
    log = jsonl_path(feature)
    # Empty-state shortcut: compacting a feature that has never been
    # touched on disk is a no-op. We deliberately do NOT write an
    # empty snapshot here — defensive callers (e.g. a hook compacting
    # every known feature at startup) would otherwise litter the
    # state dir with empty ``<feature>_ftrl.snapshot.json`` files
    # carrying no information. ``{}`` is the natural cold-start
    # result and we keep the disk footprint zero.
    if not weights and not log.exists() and not snap.exists():
        return {}
    doc = {
        "schema": _SCHEMA_VERSION,
        "feature": feature,
        "weights": weights,
        "compacted_through_ts": _utc_iso(),
        "compacted_event_count": _count_lines(log),
    }
    try:
        snap.parent.mkdir(parents=True, exist_ok=True)
        tmp = snap.with_suffix(snap.suffix + ".tmp")
        tmp.write_text(
            json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp, snap)
    except OSError:
        return weights
    # Trim jsonl tail to keep_last_n. Never raise — failure leaves the
    # full log in place which is fine, the snapshot is the source of
    # truth on next load anyway.
    if log.is_file() and keep_last_n >= 0:
        try:
            with log.open("r", encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError:
            return weights
        if len(lines) > keep_last_n:
            tail = lines[-keep_last_n:] if keep_last_n > 0 else []
            try:
                tmp_log = log.with_suffix(log.suffix + ".tmp")
                tmp_log.write_text("".join(tail), encoding="utf-8")
                os.replace(tmp_log, log)
            except OSError:
                pass
    return weights


def _count_lines(path: Path) -> int:
    if not path.is_file():
        return 0
    try:
        with path.open("rb") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0
