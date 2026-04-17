"""concinno.cache.memdir — Append-only dated event log under MEMORY.md.

@module cache.memdir
@responsibility Python port of Claude Code's ``memdir`` subsystem
    (``src/memdir/memdir.ts`` and siblings). The module provides the
    **durable, structurally append-only** layer that sits underneath
    :mod:`concinno.cache.session_memory`.

    Think of the pair as an index + log:

    - ``MEMORY.md`` (the "entrypoint" / index) is what
      :class:`~concinno.cache.session_memory.SessionMemory` distills
      into. It is **overwritable** — every distill pass rewrites it.
    - ``memdir`` (this module) is the **append-only** log of raw events
      the distiller reads from. New events append to today's dated
      file (``YYYY-MM-DD.md``); when that file hits the hard line/byte
      caps (identical to session_memory's caps — 200 lines / 25 000
      bytes), a monotonic suffix file is opened (``YYYY-MM-DD_02.md``,
      ``_03``, ...). Old files are **never overwritten**. This makes
      "what did we do on Tuesday?" a legal query.

    The module is pure storage + parse. No LLM calls, no network, no
    optional dependencies. :meth:`Memdir.find_relevant` is a simple
    keyword scanner — callers who want semantic retrieval should plug
    memdir into :class:`concinno.ziq_retrieval.ZIQRetrieval` as a
    namespace.

    Key invariants:

    1. **Append-only**. Past days are not rewritten. Rollover inside
       today is allowed (new suffix file), but we never touch suffix
       ``_N`` once ``_N+1`` exists.
    2. **Hard caps are defense-in-depth**. We check them BEFORE writing
       each entry so a single oversized entry can't blow the cap.
    3. **Parse is best-effort**. Corrupt or human-edited lines are
       silently skipped, not raised. The log should always stay
       readable.
    4. **No personal paths**. Default root is
       ``~/.claude/.concinno_memdir``; ``CONCINNO_MEMDIR`` env var
       overrides.

@dependencies stdlib only
@exports DEFAULT_MAX_LINES_PER_FILE, DEFAULT_MAX_BYTES_PER_FILE,
    DEFAULT_MEMDIR_NAME, DEFAULT_MAX_ENTRYPOINT_LINES,
    DEFAULT_MAX_ENTRYPOINT_BYTES, ENTRYPOINT_FILENAME,
    MemoryEntry, MemdirStats, Memdir

Porting notes vs ``memdir.ts``:

- The TS source exposes ``truncateEntrypointContent`` as a module-level
  function. Here it lives as :meth:`Memdir.truncate_entrypoint` (an
  instance method because the caps are per-instance) AND the behaviour
  is **identical** to :meth:`SessionMemory.truncate_content` — both
  files share the same memdir convention, so the logic is duplicated
  deliberately to keep the two subsystems independent. If one changes,
  the other MUST be updated in lockstep.
- The TS ``getAutoMemPath`` / ``ensureMemoryDirExists`` helpers are
  collapsed into the :class:`Memdir` constructor: we compute the root
  path once and ``mkdir(parents=True, exist_ok=True)`` on first write.
- The TS ``findRelevantMemories`` uses tiered retrieval (keyword →
  topic → LLM). We only implement the keyword tier; semantic tiers
  belong to ZIQ.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger("concinno.cache.memdir")

__all__ = [
    "DEFAULT_MAX_BYTES_PER_FILE",
    "DEFAULT_MAX_ENTRYPOINT_BYTES",
    "DEFAULT_MAX_ENTRYPOINT_LINES",
    "DEFAULT_MAX_LINES_PER_FILE",
    "DEFAULT_MEMDIR_NAME",
    "ENTRYPOINT_FILENAME",
    "Memdir",
    "MemdirStats",
    "MemoryEntry",
]


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Hard line cap for a single dated log file. When reached, a new
#: suffix file is opened for the same day. Matches the entrypoint
#: cap so the durable log and its distilled index share units.
DEFAULT_MAX_LINES_PER_FILE = 200

#: Hard UTF-8 byte cap for a single dated log file. Same rollover
#: behaviour as the line cap. Matches ``MAX_ENTRYPOINT_BYTES`` from
#: ``memdir.ts``.
DEFAULT_MAX_BYTES_PER_FILE = 25_000

#: Directory name used under the default root. Kept as a public
#: constant so tooling can reference it symbolically.
DEFAULT_MEMDIR_NAME = "memdir"

#: Hard line cap for ``MEMORY.md`` — the distilled index that sits
#: above memdir. Used by :meth:`Memdir.truncate_entrypoint`.
DEFAULT_MAX_ENTRYPOINT_LINES = 200

#: Hard UTF-8 byte cap for ``MEMORY.md``.
DEFAULT_MAX_ENTRYPOINT_BYTES = 25_000

#: Canonical filename for the distilled index that lives next to
#: memdir files but is written by session_memory, not memdir itself.
ENTRYPOINT_FILENAME = "MEMORY.md"

#: Warning suffix appended when the entrypoint line cap fires.
_LINE_TRUNC_WARNING = "\n<!-- truncated: exceeded max_lines -->\n"

#: Warning suffix appended when the entrypoint byte cap fires.
_BYTE_TRUNC_WARNING = "\n<!-- truncated: exceeded max_bytes -->\n"

#: Regex that parses a well-formed memdir line back into its parts.
#: Matches ``- [HH:MM:SS] kind: summary`` with an optional
#: `` (tags: a,b)`` suffix. ``kind`` is restricted to ``\w+`` so a
#: stray ``-`` in the summary doesn't accidentally parse as a kind.
_LINE_RE = re.compile(
    r"^- \[(\d{2}:\d{2}:\d{2})\] (\w+): (.*?)(?:\s+\(tags: ([^)]*)\))?$"
)

#: Token regex for :meth:`Memdir.find_relevant`. Lowercase alphanumeric
#: words of length ≥ 3 — matches what ZIQ / fewshot tokenisers use so
#: queries are consistent across the memory subsystem.
_TOKEN_RE = re.compile(r"[a-z0-9_]{3,}")

#: Regex for the date prefix of a memdir file. ``_NN`` suffix is
#: optional; absent → suffix 1.
_FILENAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:_(\d+))?\.md$")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class MemoryEntry:
    """A single event in the append-only log.

    Attributes:
        timestamp: Wall-clock time the event happened. The date
            portion determines which file the entry goes in; the time
            portion is what ends up in the rendered markdown line.
        kind: Short category tag (``\\w+``). Examples: ``tool_call``,
            ``user_msg``, ``decision``, ``blocker``. Regex-restricted
            so the round-trip parser can find the boundary reliably.
        summary: Single-line human-readable description. Newlines in
            the summary will break the parser — callers should strip
            them before constructing the entry.
        tags: Optional free-form tag tuple (e.g.
            ``("ziq", "benchmark")``). Tags are rendered in
            parentheses at end-of-line and stripped back out on parse.
        details: Optional key-value extras. Rendered on a second line
            as ``<!-- details: k=v; ... -->`` so humans reading the
            log see them but the parser skips them. Values are
            stringified; nested structures are the caller's problem.
    """

    timestamp: datetime
    kind: str
    summary: str
    tags: tuple[str, ...] = ()
    details: dict[str, str] = field(default_factory=dict)

    def to_md_line(self) -> str:
        """Render the entry as markdown lines terminated by ``\\n``.

        Format:

        ``- [HH:MM:SS] kind: summary`` with optional
        `` (tags: t1,t2)`` suffix, plus an optional second line
        ``<!-- details: k1=v1; k2=v2 -->`` when ``details`` is
        non-empty. Both lines are newline-terminated so the caller can
        concatenate multiple entries with no extra separators.
        """
        head = (
            f"- [{self.timestamp.strftime('%H:%M:%S')}] "
            f"{self.kind}: {self.summary}"
        )
        if self.tags:
            head = head + f" (tags: {','.join(self.tags)})"
        out = head + "\n"
        if self.details:
            parts = [f"{k}={v}" for k, v in self.details.items()]
            out = out + f"<!-- details: {'; '.join(parts)} -->\n"
        return out

    @classmethod
    def parse_md_line(cls, line: str, *, day: date) -> MemoryEntry | None:
        """Parse a single rendered line back into an entry.

        Returns ``None`` when the line doesn't match the expected
        shape — callers should treat that as "skip, keep reading".
        The ``day`` argument is needed because memdir lines only carry
        the time-of-day; the file name carries the date.
        """
        stripped = line.rstrip("\n")
        if not stripped:
            return None
        if stripped.startswith("<!--"):
            # Second-line details marker — skip silently.
            return None
        m = _LINE_RE.match(stripped)
        if m is None:
            return None
        time_s, kind, summary, tags_s = m.groups()
        try:
            hh, mm, ss = (int(x) for x in time_s.split(":"))
            ts = datetime(day.year, day.month, day.day, hh, mm, ss)
        except ValueError:
            return None
        tags: tuple[str, ...] = ()
        if tags_s:
            tags = tuple(t for t in tags_s.split(",") if t)
        return cls(timestamp=ts, kind=kind, summary=summary, tags=tags)


@dataclass
class MemdirStats:
    """Snapshot of the on-disk memdir state.

    Computed on demand by :meth:`Memdir.stats` — we do not cache
    because the log is visible to other processes (the whole point of
    the append-only design).

    Attributes:
        total_entries: Sum of parseable entries across all files.
        total_files: Number of ``*.md`` files under the root,
            excluding ``MEMORY.md``.
        oldest_day: Earliest date present on disk, or ``None`` when
            the log is empty.
        newest_day: Latest date present on disk, or ``None`` when the
            log is empty.
        total_bytes: Sum of file sizes in bytes.
    """

    total_entries: int = 0
    total_files: int = 0
    oldest_day: date | None = None
    newest_day: date | None = None
    total_bytes: int = 0


# ---------------------------------------------------------------------------
# Default-root resolution
# ---------------------------------------------------------------------------


def _default_root() -> Path:
    """Return the default memdir root.

    Honours ``CONCINNO_MEMDIR`` when set, otherwise falls back to
    ``~/.claude/.concinno_memdir``. NEVER hardcodes a workspace path:
    CCC is a library, not an application.
    """
    env = os.environ.get("CONCINNO_MEMDIR")
    if env:
        return Path(env)
    return Path.home() / ".claude" / ".concinno_memdir"


# ---------------------------------------------------------------------------
# Memdir
# ---------------------------------------------------------------------------


class Memdir:
    """Append-only dated log with automatic rollover on cap hit.

    Args:
        root: Directory that contains the dated files and (optionally)
            ``MEMORY.md``. When ``None``, resolved via
            :func:`_default_root`. The directory is created lazily on
            first :meth:`append`.
        max_lines_per_file: Hard line cap per dated file. When adding
            an entry would exceed this, a new suffix is opened.
        max_bytes_per_file: Hard UTF-8 byte cap per dated file. Same
            rollover behaviour as the line cap.

    Raises:
        ValueError: If either cap is non-positive.
    """

    def __init__(
        self,
        *,
        root: str | Path | None = None,
        max_lines_per_file: int = DEFAULT_MAX_LINES_PER_FILE,
        max_bytes_per_file: int = DEFAULT_MAX_BYTES_PER_FILE,
    ) -> None:
        if max_lines_per_file <= 0:
            msg = f"max_lines_per_file must be > 0, got {max_lines_per_file}"
            raise ValueError(msg)
        if max_bytes_per_file <= 0:
            msg = f"max_bytes_per_file must be > 0, got {max_bytes_per_file}"
            raise ValueError(msg)

        self._root = Path(root) if root is not None else _default_root()
        self._max_lines = int(max_lines_per_file)
        self._max_bytes = int(max_bytes_per_file)

    # ---- configuration accessors --------------------------------------

    @property
    def root(self) -> Path:
        """Return the directory that holds all memdir files."""
        return self._root

    @property
    def max_lines_per_file(self) -> int:
        return self._max_lines

    @property
    def max_bytes_per_file(self) -> int:
        return self._max_bytes

    # ---- path helpers -------------------------------------------------

    def file_path_for(self, day: date, *, suffix: int = 1) -> Path:
        """Compute the path for a given day and rollover suffix.

        ``suffix=1`` → ``root/YYYY-MM-DD.md`` (no suffix on disk).
        ``suffix>=2`` → ``root/YYYY-MM-DD_NN.md`` zero-padded to two
        digits. This helper is pure — it does not touch the filesystem,
        so it's safe for callers that need deterministic paths.
        """
        if suffix <= 1:
            return self._root / f"{day.isoformat()}.md"
        return self._root / f"{day.isoformat()}_{suffix:02d}.md"

    def entrypoint_path(self) -> Path:
        """Return the canonical path for the distilled ``MEMORY.md``.

        Memdir itself never writes this file — it's managed by
        :class:`~concinno.cache.session_memory.SessionMemory` or an
        equivalent distiller. We expose the path here because the
        convention (MEMORY.md sits inside the memdir root) came from
        ``memdir.ts`` and should stay authoritative in one place.
        """
        return self._root / ENTRYPOINT_FILENAME

    def list_files(self) -> list[Path]:
        """Return every dated file under the root, oldest-first.

        ``MEMORY.md`` is excluded — it's the index, not a log file. We
        sort by filename; since ISO dates sort lexicographically and
        rollover suffixes are zero-padded, ``2026-04-13.md`` precedes
        ``2026-04-13_02.md`` precedes ``2026-04-14.md`` naturally.
        """
        if not self._root.exists():
            return []
        files = [
            p
            for p in self._root.glob("*.md")
            if p.name != ENTRYPOINT_FILENAME
            and _FILENAME_RE.match(p.name) is not None
        ]
        files.sort(key=lambda p: p.name)
        return files

    # ---- append -------------------------------------------------------

    def append(self, entry: MemoryEntry) -> Path:
        """Append ``entry`` to today's dated file, rolling over if needed.

        Rollover logic: start at the highest existing suffix for
        today's date (or suffix 1 if none exists). Compute the size
        and line count the file would have AFTER appending the entry;
        if either cap would be exceeded, bump the suffix and create a
        fresh file instead. This is a pre-check, so a single oversized
        entry still writes to a fresh file rather than blowing the
        cap on an existing one.

        Returns the path actually written to.
        """
        self._root.mkdir(parents=True, exist_ok=True)
        day = entry.timestamp.date()
        rendered = entry.to_md_line()
        encoded_delta = len(rendered.encode("utf-8"))
        line_delta = rendered.count("\n")

        suffix = self._current_suffix(day)
        target = self.file_path_for(day, suffix=suffix)

        if target.exists():
            existing_bytes = target.stat().st_size
            # Count lines by decoded content so '\r\n' on Windows does
            # not double-count.
            try:
                existing_lines = target.read_text(encoding="utf-8").count("\n")
            except OSError as exc:  # pragma: no cover - defensive
                logger.debug("memdir append stat-read failed: %s", exc)
                existing_lines = 0
            if (
                existing_bytes + encoded_delta > self._max_bytes
                or existing_lines + line_delta > self._max_lines
            ):
                suffix += 1
                target = self.file_path_for(day, suffix=suffix)

        with target.open("a", encoding="utf-8", newline="") as fh:
            fh.write(rendered)
        return target

    def _current_suffix(self, day: date) -> int:
        """Return the highest rollover suffix currently on disk for ``day``.

        When no file exists yet, returns 1 (the "no-suffix" form).
        """
        if not self._root.exists():
            return 1
        prefix = day.isoformat()
        highest = 1
        for p in self._root.glob(f"{prefix}*.md"):
            m = _FILENAME_RE.match(p.name)
            if m is None:
                continue
            if m.group(1) != prefix:
                continue
            suffix_s = m.group(2)
            suffix_n = int(suffix_s) if suffix_s else 1
            if suffix_n > highest:
                highest = suffix_n
        return highest

    # ---- read ---------------------------------------------------------

    def read_day(self, day: date) -> list[MemoryEntry]:
        """Read every entry written for ``day``, in file + line order.

        All rollover suffixes for the day are concatenated in suffix
        order. Corrupt or non-entry lines (e.g. the ``<!-- details
        -->`` comment lines, or a line the user edited by hand) are
        silently skipped — this method never raises on parse error.
        """
        if not self._root.exists():
            return []
        prefix = day.isoformat()
        candidates: list[tuple[int, Path]] = []
        for p in self._root.glob(f"{prefix}*.md"):
            m = _FILENAME_RE.match(p.name)
            if m is None or m.group(1) != prefix:
                continue
            suffix_s = m.group(2)
            suffix_n = int(suffix_s) if suffix_s else 1
            candidates.append((suffix_n, p))
        candidates.sort(key=lambda t: t[0])

        out: list[MemoryEntry] = []
        for _suffix, path in candidates:
            try:
                content = path.read_text(encoding="utf-8")
            except OSError as exc:  # pragma: no cover - defensive
                logger.debug("memdir read_day failed for %s: %s", path, exc)
                continue
            for raw in content.splitlines():
                entry = MemoryEntry.parse_md_line(raw, day=day)
                if entry is not None:
                    out.append(entry)
        return out

    def read_window(self, *, start: date, end: date) -> list[MemoryEntry]:
        """Read every entry in ``[start, end]`` inclusive.

        When ``start > end`` we return an empty list rather than
        raising; this matches the "reversed range = empty" convention
        from :class:`range` and keeps call sites concise.
        """
        if start > end:
            return []
        out: list[MemoryEntry] = []
        current = start
        while current <= end:
            out.extend(self.read_day(current))
            current = current + timedelta(days=1)
        return out

    # ---- retrieval ----------------------------------------------------

    def find_relevant(
        self,
        query: str,
        *,
        max_entries: int = 20,
        max_age_days: int = 14,
    ) -> list[MemoryEntry]:
        """Keyword-scan the last ``max_age_days`` for entries matching ``query``.

        Tokenisation is deliberately aligned with ZIQ / fewshot (see
        :data:`_TOKEN_RE`): lowercase alphanumeric runs of length ≥ 3.
        We intersect the query tokens with the tokens extracted from
        ``summary + " " + kind + " " + tags`` for each entry; ANY
        overlap counts as a match.

        Results are returned newest-first (by timestamp, descending)
        and capped at ``max_entries``. This is NOT semantic retrieval
        — use :class:`concinno.ziq_retrieval.ZIQRetrieval` when you
        need embedding-based recall.
        """
        if max_entries <= 0 or max_age_days <= 0:
            return []
        q_tokens = set(_TOKEN_RE.findall(query.lower()))
        if not q_tokens:
            return []

        today = date.today()
        earliest = today - timedelta(days=max_age_days - 1)
        window = self.read_window(start=earliest, end=today)
        # Newest first. Python's sort is stable, so entries written in
        # order within the same second stay ordered.
        window.sort(key=lambda e: e.timestamp, reverse=True)

        out: list[MemoryEntry] = []
        for entry in window:
            hay = " ".join(
                [entry.summary, entry.kind, " ".join(entry.tags)]
            ).lower()
            hay_tokens = set(_TOKEN_RE.findall(hay))
            if q_tokens & hay_tokens:
                out.append(entry)
                if len(out) >= max_entries:
                    break
        return out

    # ---- truncation (entrypoint helper) -------------------------------

    def truncate_entrypoint(
        self,
        content: str,
        *,
        max_lines: int = DEFAULT_MAX_ENTRYPOINT_LINES,
        max_bytes: int = DEFAULT_MAX_ENTRYPOINT_BYTES,
    ) -> str:
        """Apply the ``MEMORY.md`` line then byte caps.

        Mirrors :meth:`concinno.cache.session_memory.SessionMemory.truncate_content`
        one-for-one; the two subsystems share the same memdir
        convention. Kept as a standalone helper here because the TS
        source defines ``truncateEntrypointContent`` in the memdir
        module. If either implementation changes, update both.

        Passes:

        1. **Lines**: if ``content`` has more than ``max_lines`` lines
           (``splitlines(keepends=True)``), keep the first ``max_lines``
           and append :data:`_LINE_TRUNC_WARNING`.
        2. **Bytes**: if the UTF-8 encoded result is longer than
           ``max_bytes``, cut at the last ``\\n`` before the budget
           (leaving room for :data:`_BYTE_TRUNC_WARNING`) so markdown
           structure survives. When no newline is found, fall back to
           a safe UTF-8 prefix.
        """
        if max_lines <= 0 or max_bytes <= 0:
            msg = "max_lines and max_bytes must be > 0"
            raise ValueError(msg)

        lines = content.splitlines(keepends=True)
        truncated_by_lines = False
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            truncated_by_lines = True
        result = "".join(lines)
        if truncated_by_lines:
            result = result + _LINE_TRUNC_WARNING

        encoded = result.encode("utf-8")
        if len(encoded) <= max_bytes:
            return result

        warning_bytes = _BYTE_TRUNC_WARNING.encode("utf-8")
        budget = max_bytes - len(warning_bytes)
        if budget <= 0:
            return _BYTE_TRUNC_WARNING

        slice_ = encoded[:budget]
        nl_index = slice_.rfind(b"\n")
        if nl_index > 0:
            head_bytes = slice_[: nl_index + 1]
        else:
            head_bytes = slice_
        head = head_bytes.decode("utf-8", errors="ignore")
        return head + _BYTE_TRUNC_WARNING

    # ---- diagnostics --------------------------------------------------

    def stats(self) -> MemdirStats:
        """Compute a point-in-time summary of the log.

        Walks every dated file under the root (excluding
        ``MEMORY.md``), parses entries to get the count, and stats
        each file for the byte total. Not cached — callers that need
        this in a hot path should memoise externally.
        """
        files = self.list_files()
        if not files:
            return MemdirStats()

        total_entries = 0
        total_bytes = 0
        days: list[date] = []
        for p in files:
            m = _FILENAME_RE.match(p.name)
            if m is None:
                continue
            try:
                day = date.fromisoformat(m.group(1))
            except ValueError:
                continue
            try:
                total_bytes += p.stat().st_size
            except OSError:  # pragma: no cover - defensive
                continue
            try:
                content = p.read_text(encoding="utf-8")
            except OSError:  # pragma: no cover - defensive
                continue
            for raw in content.splitlines():
                if MemoryEntry.parse_md_line(raw, day=day) is not None:
                    total_entries += 1
            days.append(day)

        if not days:
            return MemdirStats(total_files=len(files), total_bytes=total_bytes)

        return MemdirStats(
            total_entries=total_entries,
            total_files=len(files),
            oldest_day=min(days),
            newest_day=max(days),
            total_bytes=total_bytes,
        )
