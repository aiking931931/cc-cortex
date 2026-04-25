"""Append-only JSONL state log for a Persona.

Each chat / consolidation / pin event writes one JSON line. Replay
on load reconstructs the in-memory state. JSONL was chosen over a
single JSON blob because:

* Append is O(1) — concurrent writers are crash-safe at line level.
* No full re-serialise on every turn — lower latency for long
  conversations.
* Human-greppable for debugging.

The state log is intentionally separate from the persona definition
file (``alice.md``). The definition is the static template; the state
is the running tape. ``Persona.load("alice.md", state="alice.jsonl")``
threads them together.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TurnRecord:
    """One entry in the persona state log."""

    ts: str
    kind: str  # "turn" | "pin" | "unpin" | "consolidate" | "emotion"
    user: str = ""
    assistant: str = ""
    state_delta: dict[str, Any] = field(default_factory=dict)

    def to_jsonl(self) -> str:
        payload = {
            "ts": self.ts,
            "kind": self.kind,
        }
        if self.user:
            payload["user"] = self.user
        if self.assistant:
            payload["assistant"] = self.assistant
        if self.state_delta:
            payload["state_delta"] = self.state_delta
        return json.dumps(payload, ensure_ascii=False)

    @classmethod
    def from_jsonl(cls, line: str) -> TurnRecord:
        d = json.loads(line)
        return cls(
            ts=d.get("ts", _now_iso()),
            kind=d.get("kind", "turn"),
            user=d.get("user", ""),
            assistant=d.get("assistant", ""),
            state_delta=d.get("state_delta", {}) or {},
        )


@dataclass
class PersonaState:
    """In-memory runtime state for a Persona.

    Immutable except via :meth:`append` so consumers can rely on
    record ordering. The on-disk file is the canonical log; this
    object is just a parsed view.
    """

    log_path: Path | None = None
    records: list[TurnRecord] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> PersonaState:
        p = Path(path)
        records: list[TurnRecord] = []
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(TurnRecord.from_jsonl(line))
                except json.JSONDecodeError:
                    # Tolerate corrupt lines — skip rather than fail
                    # the whole replay. A future repair tool can
                    # rewrite them.
                    continue
        return cls(log_path=p, records=records)

    @classmethod
    def empty(cls) -> PersonaState:
        return cls()

    def append(self, record: TurnRecord) -> None:
        """Append a record to memory and (if attached) the on-disk log."""
        self.records.append(record)
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(record.to_jsonl() + "\n")

    def attach(self, path: str | Path) -> None:
        """Attach an on-disk log path; future appends are persisted."""
        self.log_path = Path(path)

    def turns(self) -> list[TurnRecord]:
        """Return only chat turn records (kind == 'turn')."""
        return [r for r in self.records if r.kind == "turn"]

    def save_snapshot(self, path: str | Path) -> None:
        """Re-serialise the entire log to ``path``.

        Useful for compaction or migrating an in-memory state to a
        new file. Atomic via tempfile + rename.
        """
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for r in self.records:
                f.write(r.to_jsonl() + "\n")
        tmp.replace(target)


def make_turn(user: str, assistant: str) -> TurnRecord:
    return TurnRecord(ts=_now_iso(), kind="turn", user=user, assistant=assistant)


def make_pin(content: str, reason: str | None = None) -> TurnRecord:
    delta = {"content": content}
    if reason:
        delta["reason"] = reason
    return TurnRecord(ts=_now_iso(), kind="pin", state_delta=delta)


def make_unpin(content: str) -> TurnRecord:
    return TurnRecord(ts=_now_iso(), kind="unpin", state_delta={"content": content})


def make_consolidate(summary: str) -> TurnRecord:
    return TurnRecord(
        ts=_now_iso(),
        kind="consolidate",
        state_delta={"summary": summary},
    )


def make_emotion(intensity: float, label: str) -> TurnRecord:
    return TurnRecord(
        ts=_now_iso(),
        kind="emotion",
        state_delta={"intensity": intensity, "label": label},
    )


__all__ = [
    "PersonaState",
    "TurnRecord",
    "make_consolidate",
    "make_emotion",
    "make_pin",
    "make_turn",
    "make_unpin",
]
