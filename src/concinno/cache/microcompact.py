"""concinno.cache.microcompact — Prompt-cache microcompaction.

@module cache.microcompact
@responsibility Port of Claude Code's ``microCompact`` layer. Queues
    ``cache_edits`` deletions for tool results whose content is no longer
    worth keeping in the Anthropic server-side prompt cache, so the
    cached prefix hash stays stable (later turns still hit cache) while
    dead tool-result bytes are reclaimed in place. Two triggers:

    - **time-based**: gap since last assistant > cache TTL means the
      cache has (or is about to) expire. Old tool results are queued
      for deletion so the inevitable prefix rewrite carries less weight.
    - **token-budget**: soft/hard thresholds queue oldest tool results
      until projected token use drops under target.

    The module is **library code**. It has no HTTP client of its own —
    the caller supplies a :class:`CacheEditSink` protocol implementation
    that actually forwards the queued edits to the Anthropic API (or
    whichever endpoint the host uses). This keeps ``concinno`` free of
    runtime network dependencies.

@dependencies concinno.core.state_store
@exports COMPACTABLE_TOOLS, TIME_BASED_MC_CLEARED_MESSAGE, ToolCall,
    CacheEdit, CachedMCState, CacheEditSink, Microcompactor,
    compact_if_needed

Design notes (porting contract with ``microCompact.ts``):

- ``COMPACTABLE_TOOLS`` mirrors the TS source-of-truth set. Any tool
  not in this set is silently ignored on ``register_tool_call`` — the
  TS path walks message content and simply never records non-compactable
  tools. We preserve that behavior exactly.
- ``is_main_thread=False`` mirrors ``isMainThreadSource() == false`` in
  ``microCompact.ts:250``. Forked agents (session_memory,
  prompt_suggestion, …) must NOT register into cached MC state because
  the main thread's flush would try to delete phantom IDs from its own
  conversation. Registration is therefore a no-op under a fork.
- ``TIME_BASED_MC_CLEARED_MESSAGE`` is the sentinel the TS layer writes
  in place of cleared ``tool_result.content``. We expose it for parity so
  Python hosts that also manipulate message content locally stay in sync
  with the server-side cache.
- The sink-based ``flush`` / queue split mirrors the TS split between
  ``consumePendingCacheEdits`` (queue side) and the API layer that
  actually attaches ``cache_edits`` blocks to the next request.

v1 scope intentionally excludes pinned-edit replay (``pinCacheEdits`` /
``getPinnedCacheEdits``): those concern the API-request builder, not the
compaction decision engine. They can live in a separate
``cache.pinned_edits`` module when we need them.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Protocol, Sequence

# ---------------------------------------------------------------------------
# Sparse-restoration constants (CC parity: compact.ts:122-131)
# ---------------------------------------------------------------------------

#: Upper bound on files re-injected after a compact pass. Mirrors
#: ``POST_COMPACT_MAX_FILES_TO_RESTORE`` in ``compact.ts``. Keeping the
#: set small avoids a full-file flood drowning the fresh summary.
POST_COMPACT_MAX_FILES_TO_RESTORE = 5

#: Per-file token cap when re-injecting. Mirrors
#: ``POST_COMPACT_MAX_TOKENS_PER_FILE``. Files exceeding the cap are
#: truncated (head/tail preserved) rather than dropped so the model
#: still sees the load-bearing prefix.
POST_COMPACT_MAX_TOKENS_PER_FILE = 5_000

#: Aggregate budget for the file block. ``POST_COMPACT_TOKEN_BUDGET``
#: in the TS source. Used to cap the total cost of re-injection when a
#: caller passes ``None`` for ``total_files_budget``.
POST_COMPACT_TOKEN_BUDGET = 50_000

#: Per-skill token cap. Mirrors ``POST_COMPACT_MAX_TOKENS_PER_SKILL``.
#: Skills above the cap get truncated, not dropped — the top of a
#: SKILL.md is usually the critical part.
POST_COMPACT_MAX_TOKENS_PER_SKILL = 5_000

#: Aggregate budget for the skill block. Mirrors
#: ``POST_COMPACT_SKILLS_TOKEN_BUDGET``. Sized to hold ~5 skills at the
#: per-skill cap.
POST_COMPACT_SKILLS_TOKEN_BUDGET = 25_000

#: Marker inserted at the truncation seam so the model (and humans
#: reading the injected block) can tell the file body is incomplete.
SPARSE_TRUNCATION_MARKER = "\n\n[... truncated for sparse restore ...]\n\n"

logger = logging.getLogger("concinno.cache.microcompact")

#: Union of action strings a :class:`CacheEdit` may carry. The original
#: ``delete_tool_result`` remains the only value produced by the tool-call
#: compaction path; the two ``*_section`` values are emitted by the
#: section-edit path (see :class:`SectionEdit`) and exist on the literal
#: alias so static callers can reference a single source of truth.
CacheEditAction = Literal[
    "delete_tool_result",
    "delete_section",
    "replace_section",
]

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Tool names safe to compact. Token-heavy, result-heavy, idempotent-ish.
#: Mirror of ``COMPACTABLE_TOOLS`` in ``services/compact/microCompact.ts:41``.
COMPACTABLE_TOOLS: frozenset[str] = frozenset(
    {
        "Read",
        "Bash",
        "Grep",
        "Glob",
        "WebSearch",
        "WebFetch",
        "Edit",
        "Write",
    }
)

#: Sentinel text inserted where a tool result used to live. Mirrors
#: ``TIME_BASED_MC_CLEARED_MESSAGE`` in the TS source. Host code that edits
#: local message content (not just the server cache) must use this exact
#: string so the API-side and client-side views stay aligned.
TIME_BASED_MC_CLEARED_MESSAGE = (
    "[tool result cleared by microcompact — older than cache TTL]"
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ToolCall:
    """Minimal tool-call record the compactor operates on.

    Attributes:
        call_id: Unique ID of the tool_use block (matches the
            ``tool_use_id`` on the paired ``tool_result``).
        tool_name: Name of the tool (must be in :data:`COMPACTABLE_TOOLS`
            to be tracked).
        input_hash: Stable hash of the tool input — used for
            dedup / equality checks by higher layers.
        result_tokens: Estimated token count of the tool_result content.
            Used by the token-budget trigger to decide how many results
            to queue for deletion.
        timestamp: Monotonic seconds when the result was recorded.
            Used by the time-based trigger.
        result_deleted: Set to True after a successful flush. Deleted
            entries are kept in the list so :meth:`Microcompactor.stats`
            can report the reclaim total, but they are skipped by both
            triggers.
    """

    call_id: str
    tool_name: str
    input_hash: str
    result_tokens: int
    timestamp: float
    result_deleted: bool = False


@dataclass
class CacheEdit:
    """A single edit queued for the Anthropic cache-editing API.

    Attributes:
        call_id: ``tool_use_id`` whose tool_result is to be deleted.
        action: Historically only ``"delete_tool_result"``. The type is
            widened to :data:`CacheEditAction` so the literal alias is a
            single source of truth across the module, but the tool-call
            compaction path still only ever emits ``"delete_tool_result"``.
            Section-edit actions travel on :class:`SectionEdit` instead
            — they are peers, not a subclass.
        reason: ``"time_based"`` | ``"token_budget"`` | ``"manual"`` —
            used for analytics / debugging, not routing.
    """

    call_id: str
    action: CacheEditAction = "delete_tool_result"
    reason: str = ""


@dataclass
class SectionEdit:
    """A prefix-preserving edit to an L3 cognitive_pool section.

    Section edits are **peers** of :class:`CacheEdit`, not a subclass.
    They target the body of a named section in the L3 cognitive pool so
    the Anthropic cache prefix (everything above the section) remains
    stable and the post-edit request still hits cache.

    Attributes:
        section_id: Stable 8-hex hash produced by cognitive_pool. Callers
            MUST supply this — microcompact does not compute section ids
            and does not reach into the pool module.
        action: ``"delete_section"`` removes the section range entirely;
            ``"replace_section"`` substitutes :attr:`new_body` in place.
        start_marker: Literal HTML-comment header tag that brackets the
            section on disk (see ``cognitive_pool.SECTION_HEADER_PREFIX``
            / ``SECTION_HEADER_SUFFIX``). Passed through to the sink
            untouched — the sink is responsible for matching it inside
            the cache body.
        end_marker: Literal HTML-comment footer tag (see
            ``cognitive_pool.SECTION_FOOTER``). Same contract as
            :attr:`start_marker` — microcompact treats it as opaque.
        new_body: Replacement body for ``replace_section``. Ignored for
            ``delete_section``. An empty string is valid for replace and
            will clear the section body while keeping the markers.
        reason: Free-form analytic tag (e.g. ``"drift"`` / ``"refresh"``
            / ``"manual"``). Not used for routing.
    """

    section_id: str
    action: Literal["delete_section", "replace_section"]
    start_marker: str
    end_marker: str
    new_body: str = ""
    reason: str = ""


@dataclass
class SparseRestoreConfig:
    """Budgets for post-compact sparse restoration.

    Ports ``POST_COMPACT_*`` constants from ``compact.ts:122-131``.
    Defaults match the TS source. Callers only override when they
    have measured a host-specific budget; leaving the defaults gives
    CC-equivalent behaviour out of the box.

    Attributes:
        max_files: Top N files to keep. CC uses 5.
        tokens_per_file: Per-file truncation cap (5K in CC).
        total_files_budget: Aggregate cap for the file block. When the
            sum of ``tokens_per_file * max_files`` exceeds this value,
            the collector stops early rather than emitting a bloated
            block. Default mirrors ``POST_COMPACT_TOKEN_BUDGET``.
        max_skills: Top N skills to keep. CC's constant isn't directly
            exported; we derive from ``total_skills_budget /
            tokens_per_skill`` and land on 5, matching the TS comment.
        tokens_per_skill: Per-skill truncation cap (5K in CC).
        total_skills_budget: Aggregate cap for the skill block
            (25K in CC).
    """

    max_files: int = POST_COMPACT_MAX_FILES_TO_RESTORE
    tokens_per_file: int = POST_COMPACT_MAX_TOKENS_PER_FILE
    total_files_budget: int = POST_COMPACT_TOKEN_BUDGET
    max_skills: int = 5
    tokens_per_skill: int = POST_COMPACT_MAX_TOKENS_PER_SKILL
    total_skills_budget: int = POST_COMPACT_SKILLS_TOKEN_BUDGET


@dataclass
class FileAccessRecord:
    """Host-supplied access stats for a tracked file.

    The compactor does not observe file access itself — the host
    (a hook, a proxy, a test fake) feeds these in. Fields mirror
    what CC's ``fileTracker`` collects: recency, raw reads, edits.
    ``content`` is optional so callers can pre-read or defer the
    read until scoring is done and only the winners are materialised.
    """

    path: str
    #: Monotonic seconds of the most recent access. Higher = newer.
    last_access_ts: float
    #: Total times the file was read this session. Signal of
    #: repeated reference value.
    access_count: int = 0
    #: Times the file was edited this session. Edit > read as a
    #: relevance signal; the scoring formula weights it higher.
    edit_count: int = 0
    #: Optional pre-read content. When ``None`` the host provides a
    #: ``content_loader`` callable to :meth:`Microcompactor.collect_top_files`.
    content: str | None = None
    #: Estimated token count of ``content``. When ``None`` the
    #: collector falls back to ``len(content) // 4`` (CC's default
    #: rough estimator).
    tokens: int | None = None


@dataclass
class SkillEntry:
    """Host-supplied registry row for a tracked skill.

    Mirrors the shape of CC's ``skillsLoader`` entries that compact
    re-injects. ``body`` holds the SKILL.md (or equivalent) text;
    the collector truncates head/tail if it exceeds the per-skill
    token cap.
    """

    name: str
    body: str
    #: How many times the skill was invoked this session. Primary
    #: ranking signal.
    invocation_count: int = 0
    #: Monotonic seconds of the most recent invocation. Tiebreaker
    #: for equal invocation counts.
    last_invocation_ts: float = 0.0
    #: Optional pre-computed token count. ``None`` → fall back to
    #: ``len(body) // 4`` (same convention as FileAccessRecord).
    tokens: int | None = None


@dataclass
class FileSparseEntry:
    """Materialised file entry ready for injection."""

    name: str
    content: str
    tokens: int
    score: float


@dataclass
class SkillSparseEntry:
    """Materialised skill entry ready for injection."""

    name: str
    content: str
    tokens: int
    score: float


@dataclass
class CachedMCState:
    """Per-session microcompact state.

    Persisted via :class:`~concinno.core.state_store.StateStore` so that
    restarts within a session don't lose accrued tool-call history.
    """

    tool_calls: list[ToolCall] = field(default_factory=list)
    pending_edits: list[CacheEdit] = field(default_factory=list)
    last_assistant_ts: float = 0.0
    #: Forked-agent guard. Mirrors ``isMainThreadSource`` in the TS port.
    main_thread_only: bool = True
    #: Pending L3 section edits. Parallel queue to :attr:`pending_edits`
    #: — flushed separately via :meth:`Microcompactor.flush_sections`
    #: because section edits hit a sibling sink method, not ``submit``.
    #: Added in v1.16. Legacy state files without this key load with an
    #: empty list.
    pending_section_edits: list[SectionEdit] = field(default_factory=list)
    #: Running total of section edits that have been successfully
    #: flushed, for diagnostics via :meth:`Microcompactor.stats`.
    section_edits_applied_total: int = 0


class CacheEditSink(Protocol):
    """Caller-supplied bridge to the Anthropic cache-editing API.

    CCC deliberately does not depend on any HTTP client. Concrete sinks
    live in the consumer (a hook, a proxy, a test fake) and are injected
    into :class:`Microcompactor`. This keeps the library portable and
    zero-dep.

    The ``submit`` method handles :class:`CacheEdit` (tool-result
    deletions). Sinks that also want to handle L3 section edits should
    implement the optional ``submit_sections`` sibling method — the
    compactor detects it via ``hasattr`` and falls back to a logged
    no-op when absent, so legacy sinks keep working.

    Returns:
        Number of edits successfully applied. If the sink raises, the
        compactor treats the flush as a no-op and keeps the pending list
        intact for the next attempt.
    """

    def submit(self, edits: Sequence[CacheEdit]) -> int:
        ...

    def submit_sections(self, edits: Sequence[SectionEdit]) -> int:
        """Optional: apply L3 section edits in prefix-stable fashion.

        Sinks may omit this method. :meth:`Microcompactor.flush_sections`
        probes for it with ``hasattr`` before calling; absence triggers
        a logged no-op, not a crash.
        """
        ...


# ---------------------------------------------------------------------------
# Microcompactor
# ---------------------------------------------------------------------------


class Microcompactor:
    """Queue and flush cache-delete edits for compactable tool results.

    Args:
        cache_dir: Root directory for persistent state. When ``None``,
            save / load become no-ops (useful for transient sessions and
            tests).
        session_id: Logical session identifier. Scoped state files are
            written under ``{cache_dir}/microcompact/{session_id[:8]}.json``.
        time_based_ttl_s: Time-based trigger threshold. Slightly under
            the Anthropic 5-minute cache TTL by default so we queue
            before the server cache actually expires.
        token_budget_soft: Soft token budget. At or above this level
            :meth:`evaluate_token_budget_trigger` queues oldest results
            until projected use drops back under.
        token_budget_hard: Hard token budget. Triggers an aggressive
            pass (target = ``soft - (hard - soft)``) so the next turn
            has breathing room.
        is_main_thread: Forked-agent guard. ``False`` turns
            :meth:`register_tool_call` into a no-op.
        sink: Concrete :class:`CacheEditSink` used by :meth:`flush`. When
            ``None``, ``flush`` returns ``0`` without touching state.

    Raises:
        ValueError: If ``token_budget_hard < token_budget_soft``.
    """

    #: Namespace used under the state_store root.
    _NAMESPACE = "microcompact"

    def __init__(
        self,
        *,
        cache_dir: str | None = None,
        session_id: str | None = None,
        time_based_ttl_s: float = 270.0,
        token_budget_soft: int = 40_000,
        token_budget_hard: int = 60_000,
        is_main_thread: bool = True,
        sink: CacheEditSink | None = None,
    ) -> None:
        if token_budget_hard < token_budget_soft:
            msg = (
                f"token_budget_hard ({token_budget_hard}) must be >= "
                f"token_budget_soft ({token_budget_soft})"
            )
            raise ValueError(msg)

        self._cache_dir = cache_dir
        self._session_id = session_id or "unknown"
        self._ttl_s = float(time_based_ttl_s)
        self._soft = int(token_budget_soft)
        self._hard = int(token_budget_hard)
        self._sink = sink

        self._state = CachedMCState(main_thread_only=is_main_thread)
        self._store: Any = None  # lazy — stdlib-only outside state_store

    # ---- lifecycle ------------------------------------------------------

    def _ensure_store(self) -> Any:
        if self._cache_dir is None:
            return None
        if self._store is None:
            # Lazy import keeps the module free of cross-package init
            # cost for hosts that never persist.
            from concinno.core.state_store import StateStore

            self._store = StateStore(self._cache_dir)
        return self._store

    def save(self) -> None:
        """Persist current state. No-op when ``cache_dir`` is ``None``."""
        store = self._ensure_store()
        if store is None:
            return
        payload = {
            "tool_calls": [asdict(tc) for tc in self._state.tool_calls],
            "pending_edits": [asdict(ce) for ce in self._state.pending_edits],
            "last_assistant_ts": self._state.last_assistant_ts,
            "main_thread_only": self._state.main_thread_only,
            "pending_section_edits": [
                asdict(se) for se in self._state.pending_section_edits
            ],
            "section_edits_applied_total": self._state.section_edits_applied_total,
        }
        try:
            store.write(self._NAMESPACE, self._session_id, payload)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("microcompact save failed: %s", exc)

    def load(self) -> None:
        """Restore state written by a previous :meth:`save`.

        Silently ignores missing / corrupt state — the compactor starts
        empty in that case, which is the safe default.
        """
        store = self._ensure_store()
        if store is None:
            return
        data = store.read(self._NAMESPACE, self._session_id, default={})
        if not isinstance(data, dict):
            return

        raw_calls = data.get("tool_calls") or []
        raw_edits = data.get("pending_edits") or []
        raw_section_edits = data.get("pending_section_edits") or []
        try:
            self._state.tool_calls = [
                ToolCall(**tc) for tc in raw_calls if isinstance(tc, dict)
            ]
            self._state.pending_edits = [
                CacheEdit(**ce) for ce in raw_edits if isinstance(ce, dict)
            ]
            # Backwards compat: pre-1.16 state files have no
            # ``pending_section_edits`` key. ``data.get(...) or []`` on
            # the line above yields [] in that case, so this stays empty.
            self._state.pending_section_edits = [
                SectionEdit(**se)
                for se in raw_section_edits
                if isinstance(se, dict)
            ]
        except TypeError as exc:  # pragma: no cover - defensive
            logger.debug("microcompact load skipped malformed row: %s", exc)
            self._state.tool_calls = []
            self._state.pending_edits = []
            self._state.pending_section_edits = []

        last_ts = data.get("last_assistant_ts", 0.0)
        if isinstance(last_ts, (int, float)):
            self._state.last_assistant_ts = float(last_ts)

        # Backwards compat: default to 0 when the counter is absent.
        applied_total = data.get("section_edits_applied_total", 0)
        if isinstance(applied_total, int):
            self._state.section_edits_applied_total = applied_total

        main_only = data.get("main_thread_only", True)
        if isinstance(main_only, bool):
            self._state.main_thread_only = main_only

    # ---- registration ---------------------------------------------------

    def register_tool_call(
        self,
        *,
        call_id: str,
        tool_name: str,
        input_hash: str,
        result_tokens: int,
    ) -> None:
        """Record a completed tool call.

        Silently returns when:

        - ``is_main_thread=False`` (fork guard), or
        - ``tool_name`` is not in :data:`COMPACTABLE_TOOLS`, or
        - a record with the same ``call_id`` already exists (idempotent).
        """
        if not self._state.main_thread_only:
            return
        if tool_name not in COMPACTABLE_TOOLS:
            return
        if any(tc.call_id == call_id for tc in self._state.tool_calls):
            return

        self._state.tool_calls.append(
            ToolCall(
                call_id=call_id,
                tool_name=tool_name,
                input_hash=input_hash,
                result_tokens=int(result_tokens),
                timestamp=time.monotonic(),
            )
        )

    def note_assistant_message(self, *, timestamp: float) -> None:
        """Stamp the last-seen assistant message time.

        Used by :meth:`evaluate_time_based_trigger` to compute the gap
        since the last model turn. The value must be on the same clock
        as the ``now`` argument to the evaluator (monotonic in tests,
        typically ``time.monotonic()`` in production).
        """
        self._state.last_assistant_ts = float(timestamp)

    # ---- triggers -------------------------------------------------------

    def _already_queued(self, call_id: str) -> bool:
        for edit in self._state.pending_edits:
            if edit.call_id == call_id:
                return True
        return False

    def _queue_delete(self, call_id: str, *, reason: str) -> CacheEdit | None:
        if self._already_queued(call_id):
            return None
        edit = CacheEdit(
            call_id=call_id, action="delete_tool_result", reason=reason
        )
        self._state.pending_edits.append(edit)
        return edit

    def evaluate_time_based_trigger(
        self, *, now: float | None = None
    ) -> list[CacheEdit]:
        """Queue deletes for tool results older than ``time_based_ttl_s``.

        Idempotent: already-deleted or already-queued calls are skipped,
        so calling this each turn is safe.

        Args:
            now: Monotonic clock snapshot. ``None`` → ``time.monotonic()``.

        Returns:
            The list of **new** edits queued on this call (possibly empty).
        """
        clock = time.monotonic() if now is None else float(now)
        new_edits: list[CacheEdit] = []
        for tc in self._state.tool_calls:
            if tc.result_deleted:
                continue
            if tc.timestamp + self._ttl_s >= clock:
                continue
            edit = self._queue_delete(tc.call_id, reason="time_based")
            if edit is not None:
                new_edits.append(edit)
        return new_edits

    def evaluate_token_budget_trigger(
        self, *, current_tokens: int
    ) -> list[CacheEdit]:
        """Queue oldest tool results until projected tokens drop below target.

        Two paths:

        - **soft**: ``current_tokens > soft`` → target = ``soft``.
        - **hard**: ``current_tokens > hard`` → target =
          ``soft - (hard - soft)`` (more aggressive).

        Under the soft threshold this is a no-op.
        """
        current = int(current_tokens)
        if current <= self._soft:
            return []

        if current > self._hard:
            # Hard path is strictly more aggressive than soft — the
            # target drops by the soft/hard gap so the next turn has
            # real headroom rather than landing exactly on soft.
            target = self._soft - (self._hard - self._soft)
        else:
            target = self._soft

        # Sort live calls oldest-first. Already-queued or already-deleted
        # entries are skipped but count toward savings if their bytes are
        # still in the cache (deleted=False with pending edit).
        live = [
            tc
            for tc in self._state.tool_calls
            if not tc.result_deleted and not self._already_queued(tc.call_id)
        ]
        live.sort(key=lambda tc: tc.timestamp)

        new_edits: list[CacheEdit] = []
        projected = current
        for tc in live:
            if projected <= target:
                break
            edit = self._queue_delete(tc.call_id, reason="token_budget")
            if edit is not None:
                new_edits.append(edit)
                projected -= tc.result_tokens
        return new_edits

    # ---- flush ----------------------------------------------------------

    def flush(self) -> int:
        """Hand pending edits to the sink.

        Returns the number of edits successfully applied. When no sink
        is configured, or ``pending_edits`` is empty, returns ``0``. If
        the sink raises, the pending list is preserved so the next
        ``flush`` can retry — no tool calls are marked deleted.
        """
        if self._sink is None:
            return 0
        if not self._state.pending_edits:
            return 0

        edits = list(self._state.pending_edits)
        try:
            applied = int(self._sink.submit(edits))
        except Exception as exc:
            logger.debug("microcompact sink.submit raised: %s", exc)
            return 0

        if applied <= 0:
            return 0

        # Build a fast lookup so marking is O(n+m), not O(n*m).
        flushed_ids = {e.call_id for e in edits}
        for tc in self._state.tool_calls:
            if tc.call_id in flushed_ids:
                tc.result_deleted = True
        self._state.pending_edits.clear()
        return applied

    # ---- section edits (L3 cognitive_pool) -----------------------------

    def queue_section_replace(
        self,
        *,
        section_id: str,
        start_marker: str,
        end_marker: str,
        new_body: str,
        reason: str = "",
    ) -> SectionEdit:
        """Queue a ``replace_section`` edit for later flush.

        Multiple pending edits targeting the same ``section_id`` are
        allowed and applied in insertion order on flush — callers that
        need last-writer-wins semantics should dedupe upstream.

        Returns:
            The queued :class:`SectionEdit` (same object stored on the
            internal pending list — hosts may mutate ``reason`` but not
            the markers after queueing).
        """
        edit = SectionEdit(
            section_id=section_id,
            action="replace_section",
            start_marker=start_marker,
            end_marker=end_marker,
            new_body=new_body,
            reason=reason,
        )
        self._state.pending_section_edits.append(edit)
        return edit

    def queue_section_delete(
        self,
        *,
        section_id: str,
        start_marker: str,
        end_marker: str,
        reason: str = "",
    ) -> SectionEdit:
        """Queue a ``delete_section`` edit for later flush.

        ``new_body`` is forced to an empty string — delete semantics.
        Multiple pending edits for the same section id are allowed and
        applied in order on flush (same policy as
        :meth:`queue_section_replace`).
        """
        edit = SectionEdit(
            section_id=section_id,
            action="delete_section",
            start_marker=start_marker,
            end_marker=end_marker,
            new_body="",
            reason=reason,
        )
        self._state.pending_section_edits.append(edit)
        return edit

    def pending_section_edits(self) -> list[SectionEdit]:
        """Return a snapshot copy of the pending section-edit queue.

        The returned list is a new list instance — mutating it does not
        affect the compactor's internal state. The contained
        :class:`SectionEdit` instances are shared, so mutating their
        fields still reaches the queue (matches the host-introspection
        contract of :attr:`state`).
        """
        return list(self._state.pending_section_edits)

    def flush_sections(self) -> int:
        """Hand pending section edits to the sink.

        Uses :meth:`CacheEditSink.submit_sections` when the sink
        implements it (detected via ``hasattr``). Otherwise logs a
        warning and returns ``0`` — section edits stay queued and the
        host can swap in a capable sink and retry.

        Returns:
            Number of section edits successfully applied. The queue is
            cleared on positive return; preserved on zero / exception.
        """
        if self._sink is None:
            return 0
        if not self._state.pending_section_edits:
            return 0

        submit_sections = getattr(self._sink, "submit_sections", None)
        if submit_sections is None or not callable(submit_sections):
            logger.warning(
                "microcompact flush_sections: sink %r has no "
                "submit_sections method; %d section edits remain queued",
                type(self._sink).__name__,
                len(self._state.pending_section_edits),
            )
            return 0

        edits = list(self._state.pending_section_edits)
        try:
            applied = int(submit_sections(edits))
        except Exception as exc:
            logger.debug("microcompact sink.submit_sections raised: %s", exc)
            return 0

        if applied <= 0:
            return 0

        self._state.pending_section_edits.clear()
        self._state.section_edits_applied_total += applied
        return applied

    # ---- sparse restoration (CC parity: compact.ts:122-131) ------------

    @staticmethod
    def _estimate_tokens(text: str, override: int | None = None) -> int:
        """Return ``override`` when provided, else ``len(text) // 4``.

        Matches CC's rough token estimator for callers that don't
        already have a measured value. Zero-length input → zero.
        """
        if override is not None:
            return max(0, int(override))
        if not text:
            return 0
        # Floor at 1 for non-empty content so a 1-char file still
        # counts as "present" for budget math.
        return max(1, len(text) // 4)

    @staticmethod
    def _truncate_to_tokens(text: str, max_tokens: int) -> tuple[str, int]:
        """Head/tail truncate ``text`` so the estimator returns ≤ cap.

        Returns ``(new_text, new_tokens)``. Inserts
        :data:`SPARSE_TRUNCATION_MARKER` at the seam. Head/tail split
        is 70/30 — the top of a file / skill is usually the critical
        part (imports, frontmatter, declarations), but the tail often
        carries the most-recent edits.
        """
        if max_tokens <= 0:
            return ("", 0)
        current = max(1, len(text) // 4)
        if current <= max_tokens:
            return (text, current)

        # Work in char space. ~4 chars/token is the rough estimator.
        keep_chars = max_tokens * 4
        marker = SPARSE_TRUNCATION_MARKER
        marker_chars = len(marker)
        if keep_chars <= marker_chars + 8:
            # Degenerate: not enough room for any body. Emit just the
            # marker so the caller still sees the truncation signal.
            trimmed = marker
        else:
            body_chars = keep_chars - marker_chars
            head_chars = int(body_chars * 0.7)
            tail_chars = body_chars - head_chars
            head = text[:head_chars]
            tail = text[-tail_chars:] if tail_chars > 0 else ""
            trimmed = f"{head}{marker}{tail}"
        return (trimmed, max(1, len(trimmed) // 4))

    @staticmethod
    def _score_file(
        record: FileAccessRecord,
        *,
        now: float,
        recency_weight: float = 1.0,
        access_weight: float = 0.5,
        edit_weight: float = 2.0,
    ) -> float:
        """Rank formula: recency + access + edit.

        Recency is normalized to ``1 / (1 + age_seconds / 300)`` so a
        file accessed 5 minutes ago scores 0.5, 10 minutes ago 0.33,
        etc. Edits dominate raw reads because "the user touched this
        recently" is a stronger relevance signal than "we skimmed it".
        """
        age = max(0.0, now - record.last_access_ts)
        recency = 1.0 / (1.0 + age / 300.0)
        return (
            recency_weight * recency
            + access_weight * float(record.access_count)
            + edit_weight * float(record.edit_count)
        )

    @staticmethod
    def _score_skill(entry: SkillEntry, *, now: float) -> float:
        """Rank by invocation count, tiebreak by recency.

        Formula: ``invocation_count + recency_bonus`` where the
        recency bonus is ``1 / (1 + age/300)`` so a skill invoked
        just now gets +1.0 and one invoked 10 minutes ago gets +0.33.
        The bonus never exceeds 1, so one extra invocation always
        beats a recent-but-less-used skill.
        """
        age = max(0.0, now - entry.last_invocation_ts)
        recency = 1.0 / (1.0 + age / 300.0)
        return float(entry.invocation_count) + recency

    def collect_top_files(
        self,
        file_history: dict[str, FileAccessRecord],
        config: SparseRestoreConfig | None = None,
        *,
        now: float | None = None,
        content_loader: "Any | None" = None,
    ) -> list[FileSparseEntry]:
        """Rank and materialise the top-N file sparse entries.

        Args:
            file_history: Host-supplied mapping of ``path →
                FileAccessRecord``. Keys are opaque to the compactor.
            config: Budget overrides. ``None`` → CC defaults.
            now: Monotonic clock snapshot for recency math. ``None``
                → ``time.monotonic()``.
            content_loader: Optional callable ``(path) -> str`` used
                when a record has no pre-loaded ``content``. Records
                without content and no loader are simply skipped
                (they cannot be truthfully injected).

        Returns:
            At most ``config.max_files`` entries, sorted by descending
            score, each truncated to ``config.tokens_per_file``, with
            the total block never exceeding ``config.total_files_budget``.
            Empty input → empty list (no crash).
        """
        cfg = config or SparseRestoreConfig()
        if not file_history or cfg.max_files <= 0:
            return []
        clock = time.monotonic() if now is None else float(now)

        scored: list[tuple[float, FileAccessRecord]] = [
            (self._score_file(rec, now=clock), rec)
            for rec in file_history.values()
        ]
        # Sort descending by score; ties broken by most-recent access
        # (higher last_access_ts wins).
        scored.sort(
            key=lambda pair: (pair[0], pair[1].last_access_ts),
            reverse=True,
        )

        out: list[FileSparseEntry] = []
        budget_remaining = max(0, int(cfg.total_files_budget))
        for score, rec in scored:
            if len(out) >= cfg.max_files:
                break
            content = rec.content
            if content is None and content_loader is not None:
                try:
                    content = content_loader(rec.path)
                except Exception as exc:
                    logger.debug(
                        "collect_top_files: loader failed for %s: %s",
                        rec.path,
                        exc,
                    )
                    continue
            if content is None:
                continue
            est = self._estimate_tokens(content, rec.tokens)
            cap = min(cfg.tokens_per_file, budget_remaining)
            if cap <= 0:
                break
            if est > cap:
                content, est = self._truncate_to_tokens(content, cap)
            out.append(
                FileSparseEntry(
                    name=rec.path,
                    content=content,
                    tokens=est,
                    score=score,
                )
            )
            budget_remaining -= est
        return out

    def collect_top_skills(
        self,
        skill_registry: dict[str, SkillEntry],
        config: SparseRestoreConfig | None = None,
        *,
        now: float | None = None,
    ) -> list[SkillSparseEntry]:
        """Rank and materialise the top-N skill sparse entries.

        Overflow policy: a skill exceeding ``tokens_per_skill`` is
        truncated, **not** dropped — the top of a SKILL.md usually
        holds the frontmatter + triggers, which is the load-bearing
        part. Only after the ``total_skills_budget`` is exhausted do
        further skills get dropped.

        Empty input → empty list (no crash).
        """
        cfg = config or SparseRestoreConfig()
        if not skill_registry or cfg.max_skills <= 0:
            return []
        clock = time.monotonic() if now is None else float(now)

        scored: list[tuple[float, SkillEntry]] = [
            (self._score_skill(entry, now=clock), entry)
            for entry in skill_registry.values()
        ]
        scored.sort(
            key=lambda pair: (pair[0], pair[1].last_invocation_ts),
            reverse=True,
        )

        out: list[SkillSparseEntry] = []
        budget_remaining = max(0, int(cfg.total_skills_budget))
        for score, entry in scored:
            if len(out) >= cfg.max_skills:
                break
            if budget_remaining <= 0:
                break
            est = self._estimate_tokens(entry.body, entry.tokens)
            cap = min(cfg.tokens_per_skill, budget_remaining)
            content = entry.body
            if est > cap:
                content, est = self._truncate_to_tokens(content, cap)
            out.append(
                SkillSparseEntry(
                    name=entry.name,
                    content=content,
                    tokens=est,
                    score=score,
                )
            )
            budget_remaining -= est
        return out

    def build_sparse_restore_block(
        self,
        files: list[FileSparseEntry],
        skills: list[SkillSparseEntry],
    ) -> str:
        """Render top files + skills as a system-reminder markdown block.

        Output format is deterministic for snapshot tests:

        - Header marks the block as post-compact restoration.
        - File section lists each entry under ``## File: <path>``
          with a fenced body.
        - Skill section lists each entry under ``## Skill: <name>``
          with a fenced body.

        Empty inputs on both sides → empty string (callers can
        unconditionally concat the result without a guard).
        """
        if not files and not skills:
            return ""
        lines: list[str] = [
            "<system-reminder>",
            "Post-compact sparse restoration — top-N working set kept",
            "across the compact boundary so the fresh summary is not the",
            "only anchor. Content is truncated where flagged.",
            "</system-reminder>",
            "",
        ]
        if files:
            lines.append("# Restored files")
            lines.append("")
            for entry in files:
                lines.append(f"## File: {entry.name}")
                lines.append(f"_tokens={entry.tokens} score={entry.score:.3f}_")
                lines.append("")
                lines.append("```")
                lines.append(entry.content)
                lines.append("```")
                lines.append("")
        if skills:
            lines.append("# Restored skills")
            lines.append("")
            for entry in skills:
                lines.append(f"## Skill: {entry.name}")
                lines.append(f"_tokens={entry.tokens} score={entry.score:.3f}_")
                lines.append("")
                lines.append("```")
                lines.append(entry.content)
                lines.append("```")
                lines.append("")
        return "\n".join(lines)

    # ---- diagnostics ----------------------------------------------------

    def stats(self) -> dict[str, int]:
        """Return a point-in-time stats snapshot.

        Keys:

        - ``tool_calls_total`` — every registration ever observed (incl.
          deleted entries).
        - ``tool_calls_deleted`` — entries whose flush succeeded.
        - ``pending_edits`` — queued but not yet flushed.
        - ``tokens_reclaimed_estimate`` — sum of ``result_tokens`` over
          deleted entries.
        - ``section_edits_pending`` — queued L3 section edits not yet
          flushed (added v1.16).
        - ``section_edits_applied_total`` — running total of section
          edits that have been successfully flushed over this
          compactor's lifetime (added v1.16).
        """
        deleted = [tc for tc in self._state.tool_calls if tc.result_deleted]
        return {
            "tool_calls_total": len(self._state.tool_calls),
            "tool_calls_deleted": len(deleted),
            "pending_edits": len(self._state.pending_edits),
            "tokens_reclaimed_estimate": sum(tc.result_tokens for tc in deleted),
            "section_edits_pending": len(self._state.pending_section_edits),
            "section_edits_applied_total": self._state.section_edits_applied_total,
        }

    # ---- test / host introspection -------------------------------------

    @property
    def state(self) -> CachedMCState:
        """Expose the underlying state for hosts that need to inspect it.

        Intended primarily for tests and diagnostic hooks. Mutating the
        returned object bypasses all guards.
        """
        return self._state


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------


def compact_if_needed(
    compactor: Microcompactor,
    *,
    now: float | None = None,
    current_tokens: int | None = None,
) -> int:
    """Run both triggers and flush.

    Args:
        compactor: Configured :class:`Microcompactor`.
        now: Passthrough to :meth:`Microcompactor.evaluate_time_based_trigger`.
        current_tokens: When provided, also runs the token-budget
            trigger. ``None`` skips the token path.

    Returns:
        Number of edits applied by the flush. Note: time/token triggers
        may queue new edits that remain pending if no sink is attached.
    """
    compactor.evaluate_time_based_trigger(now=now)
    if current_tokens is not None:
        compactor.evaluate_token_budget_trigger(current_tokens=current_tokens)
    return compactor.flush()


def compact_all(compactor: Microcompactor) -> tuple[int, int]:
    """Flush both tool-result and L3 section-edit queues.

    Thin convenience wrapper — does not run the time/token triggers;
    call :func:`compact_if_needed` separately if those are needed. The
    two flush methods are independent: a failure (or no-op) in one does
    not prevent the other from running.

    Args:
        compactor: Configured :class:`Microcompactor`.

    Returns:
        ``(tool_edits_applied, section_edits_applied)`` — both are zero
        when their respective queues were empty or the sink refused.
    """
    tool_applied = compactor.flush()
    section_applied = compactor.flush_sections()
    return tool_applied, section_applied
