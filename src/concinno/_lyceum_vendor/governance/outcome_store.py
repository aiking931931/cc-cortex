# SPDX-FileCopyrightText: 2025 Nous Research
# SPDX-License-Identifier: MIT

"""ZIQ outcome JSONL store.

Ported from concinno-skills-memory.ziq_outcome at commit
20bab6c8cb35006453bcb662afb4844831ea6427.

Lyceum-idiomatic surface
------------------------
The upstream Concinno module exports a NoiseFilterOutcome callback
contract for the ziq_outcome_bus. That bus is Concinno-specific
infrastructure (FieldRead namespace routing). For Lyceum we need a
much simpler thing: an append-only JSONL that any subsystem can
write to and any consumer can replay for FTRL posterior updates.

Schema (one record per line)::

    {
      "decision_id":  str | None,        # uuid for binding corrections
      "arm":          str,               # the choice that was made
      "outcome":      float in [0, 1],   # 1.0 = success, 0.0 = failure
      "timestamp":    str,               # ISO 8601 UTC
      "feature":      str,               # FQDN like
                                          # "lyceum.governance.smart_approval"
      "source":       str                # "smart_approval" | "curator" | ...
    }

File path
---------
``~/.lyceum/governance/ziq_outcomes.jsonl`` — Lyceum's own state dir.
NOT ``~/.concinno/`` (per the Lyceum/Concinno boundary in the spec).
The directory is created on first write.

Atomicity
---------
Concurrent writers use ``open(..., "a")`` which on POSIX is atomic for
writes <= PIPE_BUF (typically 4096 bytes); JSONL records are short
enough to satisfy that. On Windows we fall back to a per-record
``msvcrt.locking()`` advisory lock so two processes never interleave.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

__all__ = [
    "OutcomeRecord",
    "default_store_path",
    "record_outcome",
    "read_outcomes",
    "iter_outcomes",
]


# ─── Defaults ─────────────────────────────────────────────────────


_DEFAULT_PATH_ENV = "LYCEUM_ZIQ_OUTCOMES_PATH"
_LYCEUM_HOME_ENV = "LYCEUM_HOME"

# Process-local lock so multiple threads in the same Python process
# don't interleave even before we hit the OS-level append atomicity.
_WRITE_LOCK = threading.Lock()


# ─── Record dataclass ─────────────────────────────────────────────


@dataclass(frozen=True)
class OutcomeRecord:
    """One immutable outcome row.

    Attributes:
        feature: FQDN identifying the subsystem
            (e.g. ``lyceum.governance.smart_approval``).
        arm: The choice that was made (FTRL bucket key).
        outcome: Float in [0, 1]; 1.0 = success, 0.0 = failure.
        timestamp: ISO 8601 UTC timestamp of when the record was written.
        source: Free-form label describing the signal source.
        decision_id: Optional UUID for binding later corrections.
    """

    feature: str
    arm: str
    outcome: float
    timestamp: str
    source: str
    decision_id: Optional[str] = None

    def to_json(self) -> str:
        """Serialise to a single-line JSON string with newline."""
        payload = {
            "decision_id": self.decision_id,
            "arm": self.arm,
            "outcome": self.outcome,
            "timestamp": self.timestamp,
            "feature": self.feature,
            "source": self.source,
        }
        return json.dumps(payload, ensure_ascii=False) + "\n"

    @classmethod
    def from_json(cls, raw: str) -> "OutcomeRecord":
        """Parse one JSON line back into an :class:`OutcomeRecord`."""
        data = json.loads(raw)
        return cls(
            feature=data["feature"],
            arm=data["arm"],
            outcome=float(data["outcome"]),
            timestamp=data["timestamp"],
            source=data.get("source", ""),
            decision_id=data.get("decision_id"),
        )


# ─── Path resolution ──────────────────────────────────────────────


def _lyceum_home() -> Path:
    """Resolve the Lyceum home dir.

    Resolution order (later overrides earlier):
        1. ``Path.home() / ".lyceum"``
        2. env ``LYCEUM_HOME``
    """
    raw = os.environ.get(_LYCEUM_HOME_ENV, "").strip()
    if raw:
        return Path(raw)
    return Path.home() / ".lyceum"


def default_store_path() -> Path:
    """Return the JSONL path used when callers don't pass an override.

    Resolution order (later overrides earlier):
        1. ``<lyceum_home>/governance/ziq_outcomes.jsonl``
        2. env ``LYCEUM_ZIQ_OUTCOMES_PATH``
    """
    raw = os.environ.get(_DEFAULT_PATH_ENV, "").strip()
    if raw:
        return Path(raw)
    return _lyceum_home() / "governance" / "ziq_outcomes.jsonl"


# ─── Validation ───────────────────────────────────────────────────


def _validate_outcome(outcome: float) -> float:
    """Coerce and validate an outcome value to [0.0, 1.0]."""
    try:
        value = float(outcome)
    except (TypeError, ValueError) as exc:
        raise TypeError(
            f"outcome must be numeric, got {type(outcome).__name__}"
        ) from exc
    if not (0.0 <= value <= 1.0):
        raise ValueError(
            f"outcome must lie in [0.0, 1.0], got {value}"
        )
    return value


# ─── Atomic writer ────────────────────────────────────────────────


def _open_for_append(path: Path):
    """Open the JSONL file for append, creating parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # buffering=1 = line-buffered so each write flushes the line.
    return open(path, "a", encoding="utf-8", buffering=1)


def _write_with_windows_lock(handle, line: str) -> None:
    """Best-effort Windows advisory lock around a single append."""
    try:
        import msvcrt
    except ImportError:
        handle.write(line)
        handle.flush()
        return
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, len(line))
    except OSError:
        # Could not acquire lock; fall back to plain append.
        handle.write(line)
        handle.flush()
        return
    try:
        handle.write(line)
        handle.flush()
    finally:
        try:
            handle.seek(handle.tell() - len(line))
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, len(line))
        except OSError:
            # Release best-effort; OS unlocks on close.
            pass


def _atomic_append(path: Path, line: str) -> None:
    """Append one line atomically, with cross-platform locking."""
    with _WRITE_LOCK, _open_for_append(path) as handle:
        if sys.platform == "win32":
            _write_with_windows_lock(handle, line)
        else:
            # POSIX: O_APPEND is atomic for writes <= PIPE_BUF.
            # JSONL records fit comfortably under 4 KiB.
            handle.write(line)
            handle.flush()


# ─── Public API ───────────────────────────────────────────────────


def record_outcome(
    feature: str,
    arm: str,
    outcome: float,
    source: str,
    *,
    decision_id: Optional[str] = None,
    path: Optional[Path] = None,
) -> OutcomeRecord:
    """Append one outcome record to the JSONL store.

    Args:
        feature: FQDN of the subsystem reporting the outcome.
        arm: The choice that was made (FTRL bucket key).
        outcome: Float in [0.0, 1.0]; 1.0 = success, 0.0 = failure.
        source: Free-form label describing the signal source.
        decision_id: Optional UUID for binding later corrections.
        path: Override the JSONL path (default :func:`default_store_path`).

    Returns:
        The :class:`OutcomeRecord` that was written. Useful for tests
        and callers that want to log the value back to telemetry.

    Raises:
        TypeError: if ``outcome`` is not numeric.
        ValueError: if ``outcome`` is outside ``[0.0, 1.0]``.
    """
    validated_outcome = _validate_outcome(outcome)
    record = OutcomeRecord(
        feature=str(feature),
        arm=str(arm),
        outcome=validated_outcome,
        timestamp=datetime.now(timezone.utc).isoformat(),
        source=str(source),
        decision_id=decision_id,
    )
    target = path if path is not None else default_store_path()
    _atomic_append(target, record.to_json())
    return record


def iter_outcomes(
    feature: Optional[str] = None,
    *,
    since: Optional[str] = None,
    path: Optional[Path] = None,
) -> Iterator[OutcomeRecord]:
    """Yield outcome records, optionally filtered by feature and time.

    Args:
        feature: If supplied, only records whose ``feature`` matches.
        since: ISO 8601 timestamp string. Records with
            ``timestamp >= since`` are yielded. None = no time filter.
        path: Override the JSONL path.
    """
    target = path if path is not None else default_store_path()
    if not target.exists():
        return
    with open(target, "r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = OutcomeRecord.from_json(stripped)
            except (json.JSONDecodeError, KeyError):
                # Skip malformed rows rather than fail the whole read;
                # the JSONL append-only design tolerates partial lines
                # at the end after a crash.
                continue
            if feature is not None and record.feature != feature:
                continue
            if since is not None and record.timestamp < since:
                continue
            yield record


def read_outcomes(
    feature: Optional[str] = None,
    *,
    since: Optional[str] = None,
    path: Optional[Path] = None,
) -> list[OutcomeRecord]:
    """Eager-load helper around :func:`iter_outcomes`."""
    return list(iter_outcomes(feature, since=since, path=path))
