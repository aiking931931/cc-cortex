"""concinno.meta_skills.cross_channel — Cross-channel shared CBUA context.

@module meta_skills.cross_channel
@responsibility Share ``★`` permanent milestones and recent messages
    across channels (Discord / Gmail / Telegram / IDE chat) for the
    same user. Messages go to per-user / per-channel JSONL under
    ``~/.concinno/memory/<user_id>/<channel>.jsonl``. Fetching picks
    recent messages from OTHER channels plus every ``★`` entry globally.
@dependencies stdlib only. ``concinno.handoff_engine`` functions exist
    (``get_handoff_mode`` / ``check_token_gate``) but not a class —
    this skill is the three-tier (Index / Summary / Archive) analogue
    at a cross-channel granularity, not a handoff writer. The on-disk
    shape matches the CBUA three-layer spec in
    ``rules/L1/handoff.md``.
    # TODO: lift to handoff_engine when a cross-channel handoff writer
    #       ships upstream.
@exports CrossChannelMemoryBridge, MemoryEntry

Design
------
Three tiers, by time-to-access (matches CBUA handoff hygiene):

- **Index** (Recent-N per channel)  — in memory, read on every fetch.
- **Summary** (★ milestones)        — cached across channels.
- **Archive** (full JSONL)           — disk-only, read on demand.

Fetching context from channel C pulls Recent-N from every channel
other than C, plus all starred entries globally. Current-channel
history is intentionally excluded because the LLM already has it in
its turn context — re-injecting would double the cost and confuse
primacy bias. Explicit fetch by current-channel still works via a
dedicated method when needed.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("concinno.meta_skills.cross_channel")

_MEMORY_ROOT = Path.home() / ".concinno" / "memory"

# Matches ``rules/L1/handoff.md`` Index budget: short per-channel cap.
_DEFAULT_LIMIT = 20
_MAX_MESSAGE_LEN = 4000

# ``user_id`` must be filesystem-safe. Allow alnum + dash + underscore
# + CJK + dot.
_SAFE_ID = re.compile(r"[^\w\-.一-鿿]+")


def _safe_id(raw: str) -> str:
    """Coerce a user/channel id to a safe directory/filename component.

    Strips path separators, backslashes, and any other non-whitelisted
    chars. Empty / dot-only inputs raise ``ValueError``.
    """
    cleaned = _SAFE_ID.sub("_", raw).strip("._")
    if not cleaned:
        msg = f"invalid id {raw!r} (must contain alnum / CJK / dash)"
        raise ValueError(msg)
    return cleaned


@dataclass
class MemoryEntry:
    """One record in a channel JSONL."""

    entry_id: str
    channel: str
    ts: float
    message: str
    starred: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_jsonable(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_jsonable(d: dict[str, Any]) -> "MemoryEntry":
        return MemoryEntry(
            entry_id=str(d.get("entry_id") or uuid.uuid4().hex),
            channel=str(d.get("channel", "")),
            ts=float(d.get("ts", 0.0)),
            message=str(d.get("message", "")),
            starred=bool(d.get("starred", False)),
            metadata=dict(d.get("metadata", {}) or {}),
        )


class CrossChannelMemoryBridge:
    """Per-user cross-channel memory bridge."""

    def __init__(
        self,
        channels: list[str],
        *,
        user_id: str,
        limit: int = _DEFAULT_LIMIT,
        root: Path | None = None,
    ) -> None:
        if not channels:
            msg = "channels must be non-empty"
            raise ValueError(msg)
        self._channels = [_safe_id(c) for c in channels]
        self._user_id = _safe_id(user_id)
        self._limit = max(1, int(limit))
        self._root = root or _MEMORY_ROOT
        self._user_dir.mkdir(parents=True, exist_ok=True)

    # ── Paths ────────────────────────────────────────────────────

    @property
    def _user_dir(self) -> Path:
        return self._root / self._user_id

    def _channel_file(self, channel: str) -> Path:
        ch = _safe_id(channel)
        if ch not in self._channels:
            msg = f"channel {channel!r} not registered (known: {self._channels})"
            raise KeyError(msg)
        return self._user_dir / f"{ch}.jsonl"

    # ── Writes ───────────────────────────────────────────────────

    def record(
        self,
        channel: str,
        message: str,
        *,
        starred: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryEntry:
        """Append a message to the per-channel JSONL.

        Long messages are truncated at :data:`_MAX_MESSAGE_LEN` with a
        ``…[truncated]`` marker so one noisy message can't blow out the
        memory budget.
        """
        if not isinstance(message, str):
            msg = "message must be str"
            raise TypeError(msg)
        if len(message) > _MAX_MESSAGE_LEN:
            message = message[:_MAX_MESSAGE_LEN] + "…[truncated]"
        entry = MemoryEntry(
            entry_id=uuid.uuid4().hex,
            channel=_safe_id(channel),
            ts=time.time(),
            message=message,
            starred=bool(starred),
            metadata=dict(metadata or {}),
        )
        path = self._channel_file(channel)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry.to_jsonable(), ensure_ascii=False))
            fh.write("\n")
        return entry

    def mark_milestone(self, channel: str, entry_id: str) -> bool:
        """Promote an existing entry to ``★`` starred.

        Rewrites the channel JSONL with the target flipped. Returns
        True if we found + updated the entry, False otherwise.

        Concurrency note: not atomic across processes. For single-user
        single-process setups (the common case) that's fine; for
        heavier usage migrate to a proper store — see module TODO.
        """
        path = self._channel_file(channel)
        if not path.exists():
            return False
        entries = self._read_channel(channel)
        updated = False
        for e in entries:
            if e.entry_id == entry_id:
                if not e.starred:
                    e.starred = True
                    updated = True
                break
        if not updated:
            return False
        with path.open("w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e.to_jsonable(), ensure_ascii=False))
                fh.write("\n")
        return True

    # ── Reads ────────────────────────────────────────────────────

    def fetch_context(
        self,
        current_channel: str,
        *,
        limit: int | None = None,
        include_current: bool = False,
    ) -> str:
        """Return a markdown string of cross-channel context.

        Structure::

            ## ★ Milestones
            - [chan] (ts) message
            ...

            ## Recent (from other channels)
            - [chan] (ts) message
            ...

        ``limit`` caps per-channel recents. ``include_current`` lets
        callers opt into pulling the current channel's history too.
        """
        ch = _safe_id(current_channel)
        cap = limit if limit is not None else self._limit
        stars: list[MemoryEntry] = []
        recents: list[MemoryEntry] = []
        for other in self._channels:
            entries = self._read_channel(other)
            # Stars from every channel (always).
            stars.extend(e for e in entries if e.starred)
            if other == ch and not include_current:
                continue
            # Recent N per non-current channel (chronological last).
            recents.extend(entries[-cap:])

        # Sort stars + recents by timestamp ascending so the markdown
        # reads oldest → newest.
        stars.sort(key=lambda e: e.ts)
        recents.sort(key=lambda e: e.ts)

        lines: list[str] = []
        if stars:
            lines.append("## ★ Milestones")
            for e in stars:
                lines.append(_fmt_entry(e))
            lines.append("")
        if recents:
            lines.append("## Recent (cross-channel)")
            for e in recents:
                lines.append(_fmt_entry(e))
        return "\n".join(lines).rstrip()

    def list_starred(self) -> list[MemoryEntry]:
        """Every ``★`` entry across all channels, oldest first."""
        out: list[MemoryEntry] = []
        for ch in self._channels:
            out.extend(e for e in self._read_channel(ch) if e.starred)
        out.sort(key=lambda e: e.ts)
        return out

    def list_channel(self, channel: str) -> list[MemoryEntry]:
        """Full history for one channel."""
        return self._read_channel(channel)

    # ── Internals ────────────────────────────────────────────────

    def _read_channel(self, channel: str) -> list[MemoryEntry]:
        path = self._channel_file(channel)
        if not path.exists():
            return []
        out: list[MemoryEntry] = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("cross_channel: dropped malformed line in %s", path)
                    continue
                if isinstance(data, dict):
                    out.append(MemoryEntry.from_jsonable(data))
        return out


# ── Helpers ──────────────────────────────────────────────────────────


def _fmt_entry(e: MemoryEntry) -> str:
    marker = "★ " if e.starred else ""
    ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(e.ts))
    # Single-line message for markdown rendering.
    msg = e.message.replace("\n", " ↵ ")
    return f"- {marker}[{e.channel}] ({ts}) {msg}"
