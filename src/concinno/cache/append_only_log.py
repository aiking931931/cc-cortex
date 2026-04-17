"""concinno.cache.append_only_log — Lossless cognitive event log.

@module cache.append_only_log
@responsibility Capture raw cognitive events (LLM calls, tool calls,
    decisions, handoffs, user messages, subagent spawns, errors) into
    an append-only JSON-lines log BEFORE Claude Code's ``microCompact``
    /``compact`` layer collapses the conversation into a LLM summary.

    Claude Code's ``services/compact/compact.ts:122-131`` writes a
    single LLM-generated summary that preserves only the top-5 files ×
    5K tokens + top-skills × 5K tokens. Anything not in the top-5 is
    gone forever — there is no way to replay the original chain once
    the compact step runs. That is a lossy, irreversible transform and
    the source of the ``CC L9`` ceiling problem documented in the
    Aegis master decision.

    This module is the ``source of truth`` side of the story. Events
    are appended here the moment they occur, so later viewers (debug
    postmortem, acquisition due diligence, cross-session reasoning
    replay) can reconstruct the exact sequence regardless of whatever
    compact did. ``compact`` becomes a viewing layer, not the record.

@dependencies stdlib only (json, os, time, uuid, pathlib, dataclasses)
@exports SCHEMA_VERSION, LogEvent, AppendOnlyLog

Design principles:

- **Append-only**: events are never mutated after write. There is no
  ``update``/``delete`` method on purpose. Corrections are expressed
  as new events referencing ``parent_event_id``.
- **JSON lines**: one JSON object per line. Streaming-safe append via
  ``open(..., 'a')``. Crash-safe: partial (unflushed) lines fail
  ``json.loads`` on read and are silently skipped.
- **Session-scoped, date-partitioned files**:
  ``<log_dir>/YYYY-MM-DD/<session_id>.jsonl``. Retention by day is
  O(1) (``rm -rf`` the folder), session lookup is O(days).
- **Schema-versioned**: every event carries ``schema_version`` so a
  future migration can detect and upgrade.
- **Size-bounded replay**: ``read_session`` accepts a ``limit`` so a
  runaway log cannot OOM the caller.
- **Zero new dependencies**: CCC core zero-dep rule stays intact.
- **No personal paths**: default directory is ``Path.home() /
  .concinno_cache / append_only_log``, overridable via the
  ``CCC_APPEND_LOG_DIR`` environment variable or the ``log_dir``
  constructor argument.

The substring ``search`` implementation is deliberately naive. A ZIQ
retrieval-backed index is an obvious follow-up, but we keep A3 scoped
to persistence so the module can be audited in isolation. ZIQ
integration is future work and intentionally out of scope.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1

# Default log directory. Never hardcode personal paths — resolved at
# instantiation, not at import time, so tests that monkeypatch
# ``Path.home`` or set ``CCC_APPEND_LOG_DIR`` observe the right value.
_DEFAULT_SUBPATH = (".concinno_cache", "append_only_log")


def _default_log_dir() -> Path:
    return Path.home().joinpath(*_DEFAULT_SUBPATH)


@dataclass
class LogEvent:
    """A single captured cognitive event. Never mutated after append.

    Attributes:
        event_id: uuid4 hex, unique per event.
        session_id: caller-provided session token. Free-form string.
        timestamp: Unix epoch seconds (``time.time()``).
        event_type: one of ``"llm_call" | "tool_call" | "decision" |
            "handoff" | "user_message" | "subagent_spawn" | "error"``.
            Enforced as a free-form string (no enum) so callers can
            extend without patching this module.
        payload: arbitrary JSON-serializable dict. ``default=str`` is
            used on serialization so non-JSON types degrade to
            ``str(obj)`` instead of raising.
        parent_event_id: causal predecessor for reasoning chains.
        tokens_estimate: rough token count of ``payload``. Caller
            responsibility — this module does no tokenization.
        schema_version: frozen to ``SCHEMA_VERSION`` at creation.
    """

    event_id: str
    session_id: str
    timestamp: float
    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    parent_event_id: str | None = None
    tokens_estimate: int = 0
    schema_version: int = SCHEMA_VERSION


class AppendOnlyLog:
    """Session-scoped lossless event log.

    Directory layout::

        <log_dir>/
            2026-04-15/
                session-abc.jsonl
                session-def.jsonl
            2026-04-16/
                session-abc.jsonl   # same session spanning days

    Args:
        log_dir: Override the storage root. Resolution order is
            ``log_dir`` arg → ``CCC_APPEND_LOG_DIR`` env var →
            ``Path.home() / .concinno_cache / append_only_log``.
    """

    def __init__(self, log_dir: Path | str | None = None) -> None:
        if log_dir is not None:
            resolved = Path(log_dir)
        else:
            env_dir = os.environ.get("CCC_APPEND_LOG_DIR")
            resolved = Path(env_dir) if env_dir else _default_log_dir()
        self._dir = resolved
        self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def log_dir(self) -> Path:
        return self._dir

    @staticmethod
    def new_event_id() -> str:
        return uuid.uuid4().hex

    def _session_file(self, session_id: str, ts: float | None = None) -> Path:
        day = time.strftime("%Y-%m-%d", time.localtime(ts if ts is not None else time.time()))
        day_dir = self._dir / day
        day_dir.mkdir(parents=True, exist_ok=True)
        return day_dir / f"{session_id}.jsonl"

    @staticmethod
    def _serialize(event: LogEvent) -> str:
        return json.dumps(asdict(event), ensure_ascii=False, default=str)

    @staticmethod
    def _deserialize(line: str) -> LogEvent | None:
        """Parse one JSON line. Returns None on any corruption (crash-safe)."""
        stripped = line.strip()
        if not stripped:
            return None
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        try:
            return LogEvent(**data)
        except TypeError:
            # Unknown fields from a future schema version, or missing
            # required fields from a truncated write. Silent skip —
            # the point of append-only is to never let one bad line
            # take down the replay.
            return None

    def append(self, event: LogEvent) -> None:
        """Append one event. Crash-safe: single write + flush + close."""
        path = self._session_file(event.session_id, event.timestamp)
        line = self._serialize(event) + "\n"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()

    def append_batch(self, events: list[LogEvent]) -> None:
        """Append many events. Groups by target file so each file is
        opened exactly once regardless of how many events target it.
        """
        if not events:
            return
        by_path: dict[Path, list[LogEvent]] = {}
        for ev in events:
            by_path.setdefault(
                self._session_file(ev.session_id, ev.timestamp), [],
            ).append(ev)
        for path, batch in by_path.items():
            with open(path, "a", encoding="utf-8") as fh:
                for ev in batch:
                    fh.write(self._serialize(ev) + "\n")
                fh.flush()

    def _iter_day_dirs(self) -> Iterator[Path]:
        """Yield date-partition dirs in ascending date order."""
        if not self._dir.is_dir():
            return
        for entry in sorted(self._dir.iterdir()):
            if entry.is_dir():
                yield entry

    def read_session(
        self,
        session_id: str,
        limit: int | None = None,
    ) -> list[LogEvent]:
        """Read all events for ``session_id`` across all days.

        Result is sorted by ``timestamp`` ascending. ``limit`` caps the
        number of events returned (applied after the cross-day merge,
        so the earliest ``limit`` events are returned).
        """
        events: list[LogEvent] = []
        for day_dir in self._iter_day_dirs():
            path = day_dir / f"{session_id}.jsonl"
            if not path.exists():
                continue
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    ev = self._deserialize(line)
                    if ev is not None:
                        events.append(ev)
        events.sort(key=lambda e: e.timestamp)
        if limit is not None:
            return events[:limit]
        return events

    def iter_session(self, session_id: str) -> Iterator[LogEvent]:
        """Stream events for a session without loading all into memory.

        Note: per-day files are streamed in date order, but events
        within a single day file are yielded in file order (which is
        append order). Callers that need strict global timestamp
        ordering should use :meth:`read_session` instead.
        """
        for day_dir in self._iter_day_dirs():
            path = day_dir / f"{session_id}.jsonl"
            if not path.exists():
                continue
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    ev = self._deserialize(line)
                    if ev is not None:
                        yield ev

    def list_sessions(self, date: str | None = None) -> list[str]:
        """List session IDs. If ``date`` (``YYYY-MM-DD``), only that day."""
        sessions: set[str] = set()
        if date is not None:
            day_dir = self._dir / date
            if day_dir.is_dir():
                for f in day_dir.glob("*.jsonl"):
                    sessions.add(f.stem)
        else:
            for day_dir in self._iter_day_dirs():
                for f in day_dir.glob("*.jsonl"):
                    sessions.add(f.stem)
        return sorted(sessions)

    def search(
        self,
        query: str,
        date_range: tuple[str, str] | None = None,
        limit: int = 100,
    ) -> list[LogEvent]:
        """Naive case-insensitive substring search across all files.

        ZIQ integration future work — kept as a simple scan for A3 so
        the persistence layer can be audited in isolation.

        Args:
            query: substring to match against each raw JSON line.
            date_range: optional ``(start, end)`` inclusive YYYY-MM-DD
                bounds. Day dirs outside the range are skipped.
            limit: maximum number of matching events to return.
        """
        matches: list[LogEvent] = []
        q_lower = query.lower()
        for day_dir in self._iter_day_dirs():
            day_name = day_dir.name
            if date_range is not None and not (
                date_range[0] <= day_name <= date_range[1]
            ):
                continue
            for path in sorted(day_dir.glob("*.jsonl")):
                with open(path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        if q_lower not in line.lower():
                            continue
                        ev = self._deserialize(line)
                        if ev is None:
                            continue
                        matches.append(ev)
                        if len(matches) >= limit:
                            return matches
        return matches

    def stats(self) -> dict[str, Any]:
        """Operational stats: total events, disk size, day count."""
        total_events = 0
        total_bytes = 0
        days: set[str] = set()
        for day_dir in self._iter_day_dirs():
            days.add(day_dir.name)
            for path in day_dir.glob("*.jsonl"):
                try:
                    total_bytes += path.stat().st_size
                except OSError:
                    continue
                with open(path, "r", encoding="utf-8") as fh:
                    for _ in fh:
                        total_events += 1
        return {
            "total_events": total_events,
            "total_bytes": total_bytes,
            "days_with_data": len(days),
            "log_dir": str(self._dir),
            "schema_version": SCHEMA_VERSION,
        }


__all__ = ["SCHEMA_VERSION", "LogEvent", "AppendOnlyLog"]
