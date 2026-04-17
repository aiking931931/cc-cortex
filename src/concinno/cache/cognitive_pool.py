"""concinno.cache.cognitive_pool — L3 shared markdown pool with stable section hashes.

@module cache.cognitive_pool
@responsibility L3 of the ZIQ v7 three-layer cognitive sharing
    architecture. Sits above :mod:`concinno.cache.session_memory`
    (per-session) and :mod:`concinno.cache.memdir` (cross-session
    append-only log) as the **cross-session + cross-agent** persistent
    markdown pool.

    Any session or subagent for the same user reads and writes the
    same pool file. Each section carries a stable 8-hex-digit hash
    computed from the title (not the body), so future rewrites via
    the Anthropic cache-edit API (``microcompact.py``) can replace
    individual section bodies without invalidating the prompt cache
    prefix. The hash is load-bearing: once shipped it MUST NOT change
    or every persisted pool in the world breaks.

    Key invariants:

    1. **Title is the identity**. ``section_id = sha256(title)[:8]``.
       Body changes do not change the id; only retitling does.
    2. **Write preserves order**. Sections appear in insertion order;
       an upsert to an existing title replaces in place without
       shuffling siblings. This keeps cache prefixes stable across
       rewrites of later sections.
    3. **Atomic save**. We always write to ``<pool>.tmp`` then
       :meth:`~pathlib.Path.replace` onto the real path. A crash
       mid-write leaves the previous pool intact.
    4. **Stale detection is read-side**. Stale sections stay on disk
       until :meth:`CognitivePool.prune_stale` or an eviction needs
       room; :meth:`~CognitivePool.read_all` filters them at query
       time so a quick ``now``-injection test is deterministic.
    5. **No personal paths**. Default root is
       ``~/.claude/.concinno_pool``; ``CONCINNO_POOL_ROOT`` env var
       overrides. This is a library — strangers must be able to use
       it without touching source.

@dependencies stdlib only (hashlib, os, pathlib, re, dataclasses, time, typing)
@exports DEFAULT_POOL_FILENAME, DEFAULT_MAX_SECTIONS,
    DEFAULT_MAX_SECTION_BYTES, DEFAULT_SECTION_TTL_S,
    SECTION_HEADER_PREFIX, SECTION_HEADER_SUFFIX, SECTION_FOOTER,
    PoolSection, PoolStats, CognitivePool, PoolFull, PoolCorrupt
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

logger = logging.getLogger("concinno.cache.cognitive_pool")

__all__ = [
    "DEFAULT_MAX_SECTION_BYTES",
    "DEFAULT_MAX_SECTIONS",
    "DEFAULT_POOL_FILENAME",
    "DEFAULT_SECTION_TTL_S",
    "SECTION_FOOTER",
    "SECTION_HEADER_PREFIX",
    "SECTION_HEADER_SUFFIX",
    "CognitivePool",
    "PoolCorrupt",
    "PoolFull",
    "PoolSection",
    "PoolStats",
]


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Opening of a section header line. The parser matches this exact
#: prefix; the full header line is
#: ``<!-- cp-section:{id} title={title} updated={ts}[ tags={t1,t2}] -->``.
SECTION_HEADER_PREFIX = "<!-- cp-section:"

#: Closing of a section header line — not to be confused with
#: :data:`SECTION_FOOTER`, which terminates the section *body*.
SECTION_HEADER_SUFFIX = " -->"

#: Line that terminates a section body. Must appear alone on a line.
SECTION_FOOTER = "<!-- /cp-section -->"

#: Default markdown filename for the pool. Only the filename, not a
#: path — callers choose the root via the ``root`` argument or the
#: ``CONCINNO_POOL_ROOT`` environment variable.
DEFAULT_POOL_FILENAME = "cognitive_pool.md"

#: Maximum number of sections the pool will hold before
#: :meth:`CognitivePool.upsert_section` tries to evict stale entries
#: or raises :class:`PoolFull`.
DEFAULT_MAX_SECTIONS = 200

#: Maximum UTF-8 byte length of a single section body. Oversize bodies
#: are truncated with a warning comment rather than rejected.
DEFAULT_MAX_SECTION_BYTES = 4_000

#: Default time-to-live for sections that do not specify one.
#: ``None`` = never expire. Callers can set this to e.g. ``86_400.0``
#: (24 h) per-pool.
DEFAULT_SECTION_TTL_S: float | None = None

#: Warning marker appended to section bodies that exceeded
#: :data:`DEFAULT_MAX_SECTION_BYTES`.
_BODY_TRUNC_WARNING = "\n<!-- truncated: exceeded max_section_bytes -->\n"

#: Fraction of sections that may be corrupt before :meth:`CognitivePool.load`
#: raises :class:`PoolCorrupt` instead of silently skipping.
_CORRUPT_MAJORITY_THRESHOLD = 0.5

#: Regex that parses a well-formed header line. Named groups:
#: ``id`` (8-hex), ``title`` (non-whitespace), ``ts`` (float-ish
#: string), ``tags`` (optional comma-separated non-whitespace run).
_HEADER_RE = re.compile(
    r"^<!-- cp-section:(?P<id>[0-9a-f]{8})"
    r" title=(?P<title>\S+)"
    r" updated=(?P<ts>[0-9]+(?:\.[0-9]+)?)"
    r"(?: tags=(?P<tags>\S+))?"
    r" -->$"
)


# ---------------------------------------------------------------------------
# Default-root resolution
# ---------------------------------------------------------------------------


def _default_root() -> Path:
    """Return the default cognitive pool root directory.

    Honours ``CONCINNO_POOL_ROOT`` when set, otherwise falls back to
    ``~/.claude/.concinno_pool``. NEVER hardcodes a workspace path:
    CCC is a library, not an application.
    """
    env = os.environ.get("CONCINNO_POOL_ROOT")
    if env:
        return Path(env)
    return Path.home() / ".claude" / ".concinno_pool"


def _normalize_title(title: str) -> str:
    """Collapse whitespace runs in ``title`` to single underscores.

    Section headers are parsed with a single-token ``title=`` field,
    so we cannot allow spaces. Callers that pass ``"user goals"`` end
    up with ``"user_goals"``; re-normalising an already-normalised
    title is a no-op.
    """
    stripped = title.strip()
    if not stripped:
        msg = "title must be non-empty"
        raise ValueError(msg)
    return re.sub(r"\s+", "_", stripped)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class PoolSection:
    """A single named section inside the cognitive pool.

    Attributes:
        section_id: 8-hex-digit stable hash derived from :attr:`title`
            via :meth:`CognitivePool.compute_section_id`. Stable across
            body rewrites so cache-edit APIs can target it.
        title: Single-token human-readable identifier such as
            ``"user.goals"`` or ``"session.blockers"``. Whitespace is
            collapsed to underscores by :meth:`CognitivePool.upsert_section`.
        body: Markdown content of the section. May contain newlines.
            Must not contain the exact line :data:`SECTION_FOOTER`
            (the writer strips such occurrences defensively).
        tags: Optional tuple of short tags for
            :meth:`CognitivePool.read_tagged` filtering.
        created_ts: Wall-clock creation time (seconds since epoch).
            Preserved across upserts to the same title.
        updated_ts: Wall-clock last-write time. Combined with
            :attr:`ttl_s` to drive :meth:`is_stale`.
        ttl_s: Per-section time-to-live in seconds. ``None`` means
            "inherit the pool default"; :meth:`is_stale` resolves the
            effective value at call time.
    """

    section_id: str
    title: str
    body: str
    tags: tuple[str, ...] = ()
    created_ts: float = 0.0
    updated_ts: float = 0.0
    ttl_s: float | None = None

    def is_stale(
        self,
        *,
        now: float,
        default_ttl_s: float | None = None,
    ) -> bool:
        """Return ``True`` when this section has outlived its TTL.

        ``ttl_s`` on the section wins over ``default_ttl_s``. When
        both are ``None`` the section is immortal and the method
        returns ``False``. A non-positive effective TTL is treated as
        ``None`` (immortal) so callers can safely pass ``0``.
        """
        effective = self.ttl_s if self.ttl_s is not None else default_ttl_s
        if effective is None or effective <= 0:
            return False
        return now > self.updated_ts + effective

    def to_markdown(self) -> str:
        """Render this section as the on-disk markdown block.

        Output shape (exactly three logical parts)::

            <!-- cp-section:{id} title={title} updated={ts}[ tags={t1,t2}] -->
            {body}
            <!-- /cp-section -->

        The body is written verbatim but with a trailing newline
        guaranteed so the footer starts on its own line. Tags are only
        emitted when the tuple is non-empty to keep the happy-path
        header short.
        """
        header = (
            f"{SECTION_HEADER_PREFIX}{self.section_id}"
            f" title={self.title}"
            f" updated={self.updated_ts}"
        )
        if self.tags:
            header = header + f" tags={','.join(self.tags)}"
        header = header + SECTION_HEADER_SUFFIX
        body = self.body if self.body.endswith("\n") else self.body + "\n"
        return f"{header}\n{body}{SECTION_FOOTER}\n"


@dataclass
class PoolStats:
    """Snapshot of the on-disk pool state.

    Attributes:
        total_sections: Sections parsed from the file, including stale.
        stale_sections: How many of :attr:`total_sections` are stale
            relative to the ``now`` passed to :meth:`CognitivePool.stats`.
        total_bytes: Size of the pool file on disk in bytes, or ``0``
            when the file does not yet exist.
        last_write_ts: Most recent :attr:`PoolSection.updated_ts` seen
            in the file, or ``0.0`` when empty.
    """

    total_sections: int = 0
    stale_sections: int = 0
    total_bytes: int = 0
    last_write_ts: float = 0.0


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PoolFull(RuntimeError):
    """Raised by :meth:`CognitivePool.upsert_section` when inserting a
    new section would exceed :attr:`CognitivePool.max_sections` and no
    stale sections are available for eviction.
    """


class PoolCorrupt(RuntimeError):
    """Raised by :meth:`CognitivePool.load` when more than
    :data:`_CORRUPT_MAJORITY_THRESHOLD` of the sections on disk fail
    to parse. The caller can choose to back up the file and call
    :meth:`CognitivePool.clear`, or propagate the error.
    """


# ---------------------------------------------------------------------------
# CognitivePool
# ---------------------------------------------------------------------------


class CognitivePool:
    """Cross-session, cross-agent shared markdown pool.

    Args:
        root: Directory that will hold the pool file. ``None`` →
            resolved via :func:`_default_root`. Created on demand on
            the first :meth:`save`.
        filename: Basename of the pool file inside ``root``. Defaults
            to :data:`DEFAULT_POOL_FILENAME`.
        max_sections: Hard cap on the number of sections. Upserting
            past this cap triggers stale eviction then
            :class:`PoolFull`.
        max_section_bytes: Hard UTF-8 byte cap on a single section's
            body. Oversized bodies are truncated with a warning
            suffix rather than rejected.
        default_ttl_s: Pool-wide default TTL in seconds applied to
            sections whose own ``ttl_s`` is ``None``. ``None`` means
            "never expire by default"; individual sections can still
            override with their own :attr:`PoolSection.ttl_s`.

    Raises:
        ValueError: If any numeric argument is non-positive.
    """

    def __init__(
        self,
        *,
        root: str | Path | None = None,
        filename: str = DEFAULT_POOL_FILENAME,
        max_sections: int = DEFAULT_MAX_SECTIONS,
        max_section_bytes: int = DEFAULT_MAX_SECTION_BYTES,
        default_ttl_s: float | None = DEFAULT_SECTION_TTL_S,
    ) -> None:
        if max_sections <= 0:
            msg = f"max_sections must be > 0, got {max_sections}"
            raise ValueError(msg)
        if max_section_bytes <= 0:
            msg = f"max_section_bytes must be > 0, got {max_section_bytes}"
            raise ValueError(msg)
        if default_ttl_s is not None and default_ttl_s <= 0:
            # Treat "<= 0" as "no TTL" so callers don't have to juggle
            # sentinels; we coerce up-front and the rest of the class
            # can assume None | positive.
            default_ttl_s = None

        self._root = Path(root) if root is not None else _default_root()
        self._filename = filename
        self._max_sections = int(max_sections)
        self._max_section_bytes = int(max_section_bytes)
        self._default_ttl_s = default_ttl_s
        self._sections: list[PoolSection] = []
        self._loaded = False

    # ---- configuration accessors --------------------------------------

    @property
    def root(self) -> Path:
        """Directory holding the pool file."""
        return self._root

    @property
    def max_sections(self) -> int:
        return self._max_sections

    @property
    def max_section_bytes(self) -> int:
        return self._max_section_bytes

    @property
    def default_ttl_s(self) -> float | None:
        return self._default_ttl_s

    def pool_path(self) -> Path:
        """Absolute path to the pool markdown file."""
        return self._root / self._filename

    # ---- hashing (stable contract, DO NOT CHANGE) ---------------------

    @staticmethod
    def compute_section_id(title: str) -> str:
        """Return the stable 8-hex-digit id for ``title``.

        Implementation: first 8 chars of
        ``sha256(title.encode("utf-8")).hexdigest()``. This contract
        is load-bearing for future microcompact section-edit
        integration — once shipped it MUST NOT change or every
        persisted pool in the world breaks. Fix tests, not the hash.

        Note that the id is derived from the title only. Two sections
        with the same title have the same id regardless of body; two
        sections with different titles never collide in practice
        (2^32 space, user-supplied titles in the low hundreds).
        """
        digest = hashlib.sha256(title.encode("utf-8")).hexdigest()
        return digest[:8]

    # ---- internal I/O -------------------------------------------------

    def _ensure_loaded(self) -> None:
        """Load from disk on first access if not already cached."""
        if not self._loaded:
            self.load()

    def load(self) -> None:
        """Read and parse the pool file.

        On a missing file this is a no-op (empty pool). On a parse
        error in an individual section we skip that section and
        increment a counter; if more than
        :data:`_CORRUPT_MAJORITY_THRESHOLD` of observed sections are
        corrupt we give up and raise :class:`PoolCorrupt` so the
        caller can decide whether to back up and reset.
        """
        self._loaded = True
        self._sections = []
        path = self.pool_path()
        if not path.exists():
            return
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:  # pragma: no cover - defensive
            logger.debug("cognitive_pool load read failed: %s", exc)
            return
        observed, corrupt, parsed = self._parse_raw(raw)
        if observed > 0 and corrupt / observed > _CORRUPT_MAJORITY_THRESHOLD:
            msg = (
                f"cognitive pool {path} has {corrupt}/{observed} corrupt "
                f"sections (>{int(_CORRUPT_MAJORITY_THRESHOLD * 100)}% threshold)"
            )
            raise PoolCorrupt(msg)
        self._sections = parsed

    def _parse_raw(self, raw: str) -> tuple[int, int, list[PoolSection]]:
        """Walk ``raw`` line-by-line parsing sections via a small DFA.

        Returns ``(observed, corrupt, parsed)`` so :meth:`load` can
        make the majority-corrupt decision. Anything between sections
        is treated as annotation and discarded — users may freely add
        comments to the file outside of section blocks.

        The DFA has two states:

        * **outside**: looking for a header line. Lines starting with
          :data:`SECTION_HEADER_PREFIX` attempt a regex match. A
          header that ends with :data:`SECTION_HEADER_SUFFIX` but does
          not match the regex counts as corrupt (observed +1,
          corrupt +1) but we keep reading — one malformed header
          doesn't poison the whole file.
        * **inside**: accumulating body lines until we see a line
          equal to :data:`SECTION_FOOTER`. If EOF arrives before a
          footer the current section is also counted as corrupt.
        """
        parsed: list[PoolSection] = []
        observed = 0
        corrupt = 0

        current_header: re.Match[str] | None = None
        current_body: list[str] = []
        inside = False

        for line in raw.splitlines():
            if not inside:
                if line.startswith(SECTION_HEADER_PREFIX) and line.endswith(
                    SECTION_HEADER_SUFFIX
                ):
                    observed += 1
                    m = _HEADER_RE.match(line)
                    if m is None:
                        corrupt += 1
                        continue
                    current_header = m
                    current_body = []
                    inside = True
                # else: annotation line, skip
                continue

            # inside == True
            if line == SECTION_FOOTER:
                assert current_header is not None
                section = self._section_from_header(
                    current_header, current_body
                )
                if section is None:
                    corrupt += 1
                else:
                    parsed.append(section)
                current_header = None
                current_body = []
                inside = False
                continue
            current_body.append(line)

        # EOF reached while still inside a section → corrupt.
        if inside:
            corrupt += 1

        return observed, corrupt, parsed

    def _section_from_header(
        self,
        header: re.Match[str],
        body_lines: list[str],
    ) -> PoolSection | None:
        """Materialise a :class:`PoolSection` from a parsed header match.

        Returns ``None`` when the timestamp field fails to parse —
        callers (see :meth:`_parse_raw`) bump the corrupt counter in
        that case. The body lines are joined back with ``\\n``; a
        trailing newline is NOT added here because :meth:`to_markdown`
        will re-normalise on the next write.
        """
        try:
            updated_ts = float(header.group("ts"))
        except ValueError:
            return None
        tags_s = header.group("tags")
        tags: tuple[str, ...] = ()
        if tags_s:
            tags = tuple(t for t in tags_s.split(",") if t)
        body = "\n".join(body_lines)
        return PoolSection(
            section_id=header.group("id"),
            title=header.group("title"),
            body=body,
            tags=tags,
            created_ts=updated_ts,
            updated_ts=updated_ts,
            ttl_s=None,
        )

    def save(self) -> None:
        """Atomically persist the current in-memory sections to disk.

        Writes to ``<pool>.tmp`` first, then
        :meth:`~pathlib.Path.replace` onto the real path. A crash
        between the two steps leaves the previous file intact — this
        is the whole reason we don't stream writes directly to the
        real path. The parent directory is created on demand.
        """
        self._root.mkdir(parents=True, exist_ok=True)
        path = self.pool_path()
        tmp = path.with_suffix(path.suffix + ".tmp")
        chunks = [section.to_markdown() for section in self._sections]
        payload = "".join(chunks)
        # open() inside a with-block so a mid-write exception leaves
        # the tmp file closed and the real pool untouched. We still
        # fsync-equivalent via replace(), which on POSIX is atomic
        # and on Windows uses MoveFileEx internally.
        with tmp.open("w", encoding="utf-8", newline="") as fh:
            fh.write(payload)
        tmp.replace(path)

    # ---- read ---------------------------------------------------------

    def read_all(self, *, now: float | None = None) -> list[PoolSection]:
        """Return every non-stale section in file order.

        ``now`` defaults to :func:`time.time` — tests inject a fixed
        value for determinism. Stale filtering uses the section's own
        ``ttl_s`` when set, falling back to the pool default.
        """
        self._ensure_loaded()
        when = now if now is not None else time.time()
        return [
            s
            for s in self._sections
            if not s.is_stale(now=when, default_ttl_s=self._default_ttl_s)
        ]

    def read_section(
        self,
        *,
        title: str,
        now: float | None = None,
    ) -> PoolSection | None:
        """Look up a section by (normalised) title.

        Returns ``None`` when the title is absent OR when a matching
        section is stale. Callers that want to see stale rows should
        call :meth:`get_by_id` directly.
        """
        self._ensure_loaded()
        normalized = _normalize_title(title)
        when = now if now is not None else time.time()
        for s in self._sections:
            if s.title != normalized:
                continue
            if s.is_stale(now=when, default_ttl_s=self._default_ttl_s):
                return None
            return s
        return None

    def get_by_id(
        self,
        section_id: str,
        *,
        now: float | None = None,
    ) -> PoolSection | None:
        """Look up a section by its stable hash id.

        Stale filtering behaves the same as :meth:`read_section`.
        Useful for microcompact integration where we hold the id from
        a previous cache invocation.
        """
        self._ensure_loaded()
        when = now if now is not None else time.time()
        for s in self._sections:
            if s.section_id != section_id:
                continue
            if s.is_stale(now=when, default_ttl_s=self._default_ttl_s):
                return None
            return s
        return None

    def read_tagged(
        self,
        tags: Iterable[str],
        *,
        match_all: bool = False,
        now: float | None = None,
    ) -> list[PoolSection]:
        """Filter by tag set.

        ``match_all=False`` (default) returns any section whose tag
        tuple intersects ``tags``. ``match_all=True`` returns only
        sections that contain every tag in ``tags``. Stale sections
        are excluded in both modes. An empty ``tags`` iterable
        returns an empty list — asking for "no tags" is almost always
        a bug, and :meth:`read_all` is the right call.
        """
        wanted = set(tags)
        if not wanted:
            return []
        alive = self.read_all(now=now)
        out: list[PoolSection] = []
        for s in alive:
            have = set(s.tags)
            if match_all:
                if wanted.issubset(have):
                    out.append(s)
            else:
                if wanted & have:
                    out.append(s)
        return out

    # ---- write --------------------------------------------------------

    def upsert_section(
        self,
        *,
        title: str,
        body: str,
        tags: Iterable[str] = (),
        ttl_s: float | None = None,
        now: float | None = None,
    ) -> PoolSection:
        """Insert or replace a section by title.

        Behaviour:

        1. ``title`` is normalised (whitespace → underscore) and its
           stable id is computed.
        2. The in-memory pool is loaded if not already.
        3. If a section with the same id exists its body, tags,
           ``updated_ts``, and ``ttl_s`` are replaced. ``created_ts``
           is preserved so we can still answer "when did we first see
           this?".
        4. If the section is new and the pool already holds
           :attr:`max_sections`, we try to evict one stale section
           first (oldest by ``updated_ts``); if no stale row is
           available we raise :class:`PoolFull`.
        5. If ``body`` exceeds :attr:`max_section_bytes` UTF-8 bytes
           we truncate at the last newline before the budget and
           append :data:`_BODY_TRUNC_WARNING`. When no newline exists
           within the budget we do a raw byte slice with
           ``errors="ignore"`` so we cannot split a multi-byte
           character in half.
        6. The whole pool is persisted via :meth:`save`.

        Returns the (possibly truncated) :class:`PoolSection` actually
        stored.
        """
        self._ensure_loaded()
        normalized = _normalize_title(title)
        section_id = self.compute_section_id(normalized)
        when = now if now is not None else time.time()
        body_clean = self._strip_footer_collisions(body)
        body_final = self._truncate_body(body_clean)
        tags_t = tuple(t for t in tags if t)

        # Existing?
        for idx, existing in enumerate(self._sections):
            if existing.section_id != section_id:
                continue
            updated = PoolSection(
                section_id=section_id,
                title=normalized,
                body=body_final,
                tags=tags_t,
                created_ts=existing.created_ts,
                updated_ts=when,
                ttl_s=ttl_s,
            )
            self._sections[idx] = updated
            self.save()
            return updated

        # New section — enforce capacity.
        if len(self._sections) >= self._max_sections:
            self._evict_one_stale(now=when)
            if len(self._sections) >= self._max_sections:
                msg = (
                    f"cognitive pool is full "
                    f"({len(self._sections)}/{self._max_sections}) "
                    f"and no stale sections are available for eviction"
                )
                raise PoolFull(msg)

        fresh = PoolSection(
            section_id=section_id,
            title=normalized,
            body=body_final,
            tags=tags_t,
            created_ts=when,
            updated_ts=when,
            ttl_s=ttl_s,
        )
        self._sections.append(fresh)
        self.save()
        return fresh

    def _strip_footer_collisions(self, body: str) -> str:
        """Remove any literal :data:`SECTION_FOOTER` lines from ``body``.

        Allowing a footer line inside a body would end the section
        early on the next parse. We strip defensively rather than
        rejecting so upstream LLM content (which sometimes echoes
        markers) round-trips cleanly.
        """
        if SECTION_FOOTER not in body:
            return body
        kept = [
            line for line in body.splitlines() if line.strip() != SECTION_FOOTER
        ]
        return "\n".join(kept)

    def _truncate_body(self, body: str) -> str:
        """Enforce :attr:`max_section_bytes` with a newline-aware cut.

        Returns ``body`` unchanged when it fits. Otherwise cuts at the
        last newline inside the budget (leaving room for
        :data:`_BODY_TRUNC_WARNING`) so markdown structure survives.
        Falls back to a safe UTF-8 prefix when no newline exists.
        """
        encoded = body.encode("utf-8")
        if len(encoded) <= self._max_section_bytes:
            return body
        warn = _BODY_TRUNC_WARNING.encode("utf-8")
        budget = self._max_section_bytes - len(warn)
        if budget <= 0:
            return _BODY_TRUNC_WARNING
        slice_ = encoded[:budget]
        nl = slice_.rfind(b"\n")
        head_bytes = slice_[: nl + 1] if nl > 0 else slice_
        head = head_bytes.decode("utf-8", errors="ignore")
        return head + _BODY_TRUNC_WARNING

    def _evict_one_stale(self, *, now: float) -> None:
        """Drop the oldest stale section, if any, to free room.

        "Oldest" is by ``updated_ts`` ascending. Called from
        :meth:`upsert_section` immediately before raising
        :class:`PoolFull`; if this method succeeds in finding a
        victim, the caller retries the capacity check and proceeds.
        """
        stale_idx: list[int] = [
            i
            for i, s in enumerate(self._sections)
            if s.is_stale(now=now, default_ttl_s=self._default_ttl_s)
        ]
        if not stale_idx:
            return
        stale_idx.sort(key=lambda i: self._sections[i].updated_ts)
        victim = stale_idx[0]
        del self._sections[victim]

    def remove_section(self, *, title: str) -> bool:
        """Delete a section by title.

        Returns ``True`` when a row was removed, ``False`` when no
        matching section existed. Persists on success. Removing a
        stale section is allowed — the stale filter only affects
        reads.
        """
        self._ensure_loaded()
        normalized = _normalize_title(title)
        section_id = self.compute_section_id(normalized)
        for idx, existing in enumerate(self._sections):
            if existing.section_id == section_id:
                del self._sections[idx]
                self.save()
                return True
        return False

    def clear(self) -> None:
        """Drop all sections and persist an empty pool."""
        self._ensure_loaded()
        self._sections = []
        self.save()

    # ---- pruning ------------------------------------------------------

    def prune_stale(self, *, now: float | None = None) -> int:
        """Remove every stale section and return the count removed.

        Persists only when at least one section was actually removed.
        Passing ``now`` lets tests force a specific wall-clock.
        """
        self._ensure_loaded()
        when = now if now is not None else time.time()
        before = len(self._sections)
        self._sections = [
            s
            for s in self._sections
            if not s.is_stale(now=when, default_ttl_s=self._default_ttl_s)
        ]
        removed = before - len(self._sections)
        if removed > 0:
            self.save()
        return removed

    # ---- stats --------------------------------------------------------

    def stats(self, *, now: float | None = None) -> PoolStats:
        """Compute a snapshot of the on-disk state.

        Section counts reflect the in-memory cache (after
        :meth:`_ensure_loaded`). The byte total is a fresh
        ``stat()`` call so it matches what an external observer would
        see. ``last_write_ts`` is the max ``updated_ts`` across all
        sections — a cheap proxy for "how fresh is this pool?".
        """
        self._ensure_loaded()
        when = now if now is not None else time.time()
        total = len(self._sections)
        stale = sum(
            1
            for s in self._sections
            if s.is_stale(now=when, default_ttl_s=self._default_ttl_s)
        )
        total_bytes = 0
        path = self.pool_path()
        if path.exists():
            try:
                total_bytes = path.stat().st_size
            except OSError:  # pragma: no cover - defensive
                total_bytes = 0
        last_write = max(
            (s.updated_ts for s in self._sections),
            default=0.0,
        )
        return PoolStats(
            total_sections=total,
            stale_sections=stale,
            total_bytes=total_bytes,
            last_write_ts=last_write,
        )
