"""concinno.cache.session_memory — Persistent session memory distiller.

@module cache.session_memory
@responsibility Port of Claude Code's ``services/SessionMemory`` layer.
    Maintains a markdown file on disk summarizing the important facts
    from the current conversation (user goals, decisions, open questions,
    key files touched, current blocker). A background forked subagent
    periodically re-writes this file using a caller-supplied LLM
    distillation pass. The main agent can then re-read the file to
    recover state after :mod:`~concinno.cache.microcompact` deletes old
    tool results or after an idle gap compresses message history.

    The module is **pure policy + trigger state machine + storage**:

    - **trigger**: :meth:`SessionMemory.should_update` — first distill
      after ``init_threshold`` tool calls, subsequent updates every
      ``update_threshold`` additional tool calls. Mirrors
      :func:`hasMetInitializationThreshold` / :func:`hasMetUpdateThreshold`
      in ``sessionMemoryUtils.ts``.
    - **distillation**: :class:`DistillSink` Protocol — the caller owns
      the actual LLM call. CCC is zero-runtime-dep for core library.
    - **storage**: :meth:`SessionMemory.update` writes markdown to disk,
      bounded by ``max_bytes`` / ``max_lines`` via
      :meth:`SessionMemory.truncate_content`. Mirrors the memdir
      convention from ``memdir.ts:57`` (``truncateEntrypointContent``).
    - **state**: :class:`SessionMemoryState` persisted via
      :class:`~concinno.core.state_store.StateStore` so tool counts
      survive process restarts within a session.

    The forked-subagent model (the main agent's conversation shouldn't
    be interrupted by the distillation) is modelled by giving the
    distillation pass an **isolated** :class:`~concinno.agent.fork_context.SubagentContext`
    when one is supplied — this keeps the distiller's file reads /
    metadata out of the parent's caches. When no fork context is
    supplied, the distillation runs inline (common in tests).

@dependencies concinno.core.state_store, concinno.agent.fork_context
@exports DEFAULT_INIT_TOOL_COUNT, DEFAULT_UPDATE_TOOL_COUNT,
    DEFAULT_MAX_MD_BYTES, DEFAULT_MAX_MD_LINES, DEFAULT_MD_FILENAME,
    DistillInput, DistillOutput, DistillSink, SessionMemoryState,
    SessionMemory

Porting contract with ``services/SessionMemory/sessionMemory.ts``:

- The TS source uses **token thresholds** (``minimumMessageTokensToInit``,
  ``minimumTokensBetweenUpdate``) AND a tool-call threshold
  (``toolCallsBetweenUpdates``). Tokens are the primary gate, tool
  calls are secondary.
- This Python port **uses tool-call counts only**. CCC does not have a
  server-authoritative token counter — the TS layer relies on
  ``tokenCountWithEstimation`` which walks the actual Anthropic
  response. Library code shouldn't fake that. Tool calls are a
  serviceable proxy: in practice one tool call ≈ 2-5k tokens, so 20
  calls ≈ the TS default 10k init threshold, and 40 additional calls
  ≈ the 5k+ update threshold over typical sessions.
- ``recent_events`` is the Python analogue of the TS forked agent's
  ability to read the raw ``messages`` array. The caller supplies
  one-line summaries as each tool completes; the distiller sees a
  bounded ring buffer of the last 50.
- **Ring buffer is NOT persisted**. Events are ephemeral; the markdown
  file is the durable form. On process restart, the distiller has
  fewer recent events to work from until the session fills the buffer
  again — this is the intended tradeoff (persisting the ring would
  double-book memory for no benefit, since the MD already captures
  the distilled outcome).
"""

from __future__ import annotations

import logging
import os
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from concinno.agent.fork_context import SubagentContext

logger = logging.getLogger("concinno.cache.session_memory")

__all__ = [
    "DEFAULT_INIT_TOOL_COUNT",
    "DEFAULT_UPDATE_TOOL_COUNT",
    "DEFAULT_MAX_MD_BYTES",
    "DEFAULT_MAX_MD_LINES",
    "DEFAULT_MD_FILENAME",
    "DistillInput",
    "DistillOutput",
    "DistillSink",
    "SessionMemoryState",
    "SessionMemory",
]


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: First distill runs once cumulative tool calls reach this count. Mirrors
#: the "initialization threshold" role from ``sessionMemoryUtils.ts``.
DEFAULT_INIT_TOOL_COUNT = 20

#: Subsequent distills run every N additional tool calls after the last
#: successful update. Mirrors ``toolCallsBetweenUpdates``.
DEFAULT_UPDATE_TOOL_COUNT = 40

#: Hard cap on markdown file size in UTF-8 bytes. Matches the memdir
#: convention (``truncateEntrypointContent`` in ``memdir.ts:57``).
DEFAULT_MAX_MD_BYTES = 25_000

#: Hard cap on markdown file line count. Also from memdir.
DEFAULT_MAX_MD_LINES = 200

#: Default filename for the distilled markdown file.
DEFAULT_MD_FILENAME = "session_memory.md"

#: Ring buffer capacity for recent-event summaries.
_RING_BUFFER_CAPACITY = 50

#: Warning suffix appended when ``max_lines`` is exceeded.
_LINE_TRUNC_WARNING = "\n<!-- truncated: exceeded max_lines -->\n"

#: Warning suffix appended when ``max_bytes`` is exceeded.
_BYTE_TRUNC_WARNING = "\n<!-- truncated: exceeded max_bytes -->\n"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class DistillInput:
    """What the caller hands the distillation sink.

    Attributes:
        prior_summary: Previous markdown content (empty string on first
            run). The sink should merge new facts into this rather than
            discarding it.
        recent_events: Human-readable lines describing recent tool
            calls, in chronological order. Bounded to the last 50 by
            the ring buffer.
        tool_count: Cumulative tool-call count at the moment the
            distillation was scheduled.
        max_bytes: Hard UTF-8 byte cap the returned markdown must fit
            under. The sink is expected to aim well below this; the
            :class:`SessionMemory` applies a defense-in-depth pass
            anyway.
        max_lines: Hard line cap, same defense-in-depth story.
    """

    prior_summary: str
    recent_events: list[str]
    tool_count: int
    max_bytes: int
    max_lines: int


@dataclass
class DistillOutput:
    """What the sink returns.

    Attributes:
        markdown: The new markdown content. May already be capped —
            :meth:`SessionMemory.truncate_content` is still applied
            after receipt.
        success: ``False`` when the sink failed to produce usable
            output (e.g. LLM error). On failure, the markdown field
            is typically empty and the failure is recorded in
            :class:`SessionMemoryState`.
        error: Optional human-readable error when ``success=False``.
    """

    markdown: str
    success: bool = True
    error: str | None = None


class DistillSink(Protocol):
    """Caller-supplied bridge to the actual LLM distillation pass.

    CCC does not depend on any HTTP client. Concrete sinks live in the
    consumer (a hook, a proxy, a test fake) and are injected into
    :class:`SessionMemory`. Implementations may optionally accept a
    :class:`~concinno.agent.fork_context.SubagentContext` for the
    forked-agent model, but the core :meth:`distill` method signature
    is kept minimal so stateless test fakes are trivial to write.
    """

    def distill(self, inp: DistillInput) -> DistillOutput:
        ...


@dataclass
class SessionMemoryState:
    """Persisted per-session state.

    Attributes:
        tool_count_total: Cumulative tool calls observed by
            :meth:`SessionMemory.record_tool_call` for the lifetime of
            the session (not reset by distillation).
        tool_count_at_last_update: Value of ``tool_count_total`` at
            the moment the most recent successful distill completed.
            Used by :meth:`SessionMemory.should_update` to compute the
            "calls since last update" gap.
        last_update_ts: Wall-clock timestamp of the last successful
            distill. ``0.0`` means no distill has run yet.
        md_bytes: Size of the currently-on-disk markdown file in UTF-8
            bytes, after any truncation pass.
        md_lines: Line count of the currently-on-disk markdown file.
        distill_successes: Cumulative count of successful sink calls.
        distill_failures: Cumulative count of failed sink calls
            (exceptions OR ``DistillOutput(success=False)``).
    """

    tool_count_total: int = 0
    tool_count_at_last_update: int = 0
    last_update_ts: float = 0.0
    md_bytes: int = 0
    md_lines: int = 0
    distill_successes: int = 0
    distill_failures: int = 0


# ---------------------------------------------------------------------------
# SessionMemory
# ---------------------------------------------------------------------------


def _default_cache_dir() -> Path:
    """Return the default cache directory under the user's home dir.

    Uses the ``CONCINNO_SESSION_MEMORY_DIR`` env var when set, so
    hosts / tests can redirect storage without monkeypatching.
    """
    env = os.environ.get("CONCINNO_SESSION_MEMORY_DIR")
    if env:
        return Path(env)
    return Path.home() / ".claude" / ".concinno_session_memory"


class SessionMemory:
    """Forked-subagent session distiller with bounded markdown output.

    Args:
        cache_dir: Root directory for the markdown file and persisted
            state. When ``None``, falls back to
            ``$CONCINNO_SESSION_MEMORY_DIR`` or
            ``~/.claude/.concinno_session_memory``.
        session_id: Logical session identifier. State and markdown
            file live under ``{cache_dir}/{session_id}/``. ``None``
            uses ``"default"``.
        md_filename: Filename of the markdown file inside the session
            directory.
        init_threshold: Tool-call count that triggers the first
            distill.
        update_threshold: Tool-call delta (since ``tool_count_at_last_update``)
            that triggers each subsequent distill.
        max_bytes: Hard cap on markdown UTF-8 size. Enforced by
            :meth:`truncate_content` after the sink returns.
        max_lines: Hard cap on markdown line count. Also enforced in
            :meth:`truncate_content`, before the byte pass.
        sink: Concrete :class:`DistillSink` that performs the LLM
            distillation. When ``None``, :meth:`update` raises
            :class:`RuntimeError` — the state machine still runs (so
            callers can test trigger logic without a sink).
        fork_context: Optional isolated
            :class:`~concinno.agent.fork_context.SubagentContext`
            representing the forked distillation agent. Accessible
            via :attr:`fork_context` for sinks that want to respect
            the parent / child file-state isolation, but the core
            :meth:`update` flow does not touch it directly.

    Raises:
        ValueError: If thresholds are non-positive or caps are
            non-positive.
    """

    _NAMESPACE = "session_memory"

    def __init__(
        self,
        *,
        cache_dir: str | None = None,
        session_id: str | None = None,
        md_filename: str = DEFAULT_MD_FILENAME,
        init_threshold: int = DEFAULT_INIT_TOOL_COUNT,
        update_threshold: int = DEFAULT_UPDATE_TOOL_COUNT,
        max_bytes: int = DEFAULT_MAX_MD_BYTES,
        max_lines: int = DEFAULT_MAX_MD_LINES,
        sink: DistillSink | None = None,
        fork_context: SubagentContext | None = None,
    ) -> None:
        if init_threshold <= 0:
            msg = f"init_threshold must be > 0, got {init_threshold}"
            raise ValueError(msg)
        if update_threshold <= 0:
            msg = f"update_threshold must be > 0, got {update_threshold}"
            raise ValueError(msg)
        if max_bytes <= 0:
            msg = f"max_bytes must be > 0, got {max_bytes}"
            raise ValueError(msg)
        if max_lines <= 0:
            msg = f"max_lines must be > 0, got {max_lines}"
            raise ValueError(msg)

        self._cache_dir = Path(cache_dir) if cache_dir is not None else _default_cache_dir()
        self._session_id = session_id or "default"
        self._md_filename = md_filename
        self._init_threshold = int(init_threshold)
        self._update_threshold = int(update_threshold)
        self._max_bytes = int(max_bytes)
        self._max_lines = int(max_lines)
        self._sink = sink
        self._fork_context = fork_context

        self._state = SessionMemoryState()
        self._events: deque[str] = deque(maxlen=_RING_BUFFER_CAPACITY)
        self._store: Any = None  # lazy

    # ---- configuration accessors --------------------------------------

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    @property
    def max_lines(self) -> int:
        return self._max_lines

    @property
    def init_threshold(self) -> int:
        return self._init_threshold

    @property
    def update_threshold(self) -> int:
        return self._update_threshold

    @property
    def tool_count_total(self) -> int:
        return self._state.tool_count_total

    @property
    def fork_context(self) -> SubagentContext | None:
        """Return the forked-subagent context, if any was provided.

        Sinks that want to respect parent/child isolation can pull
        the :class:`~concinno.agent.fork_context.FileStateCache`
        off this and route their file reads through it.
        """
        return self._fork_context

    # ---- path helpers --------------------------------------------------

    def md_path(self) -> Path:
        """Return the full path where the markdown file lives."""
        return self._cache_dir / self._session_id / self._md_filename

    # ---- persistence ---------------------------------------------------

    def _ensure_store(self) -> Any:
        if self._store is None:
            from concinno.core.state_store import StateStore

            self._store = StateStore(str(self._cache_dir))
        return self._store

    def save(self) -> None:
        """Persist :class:`SessionMemoryState` to disk.

        Uses ``StateStore.write_flat`` under the ``session_memory``
        namespace, keyed by session_id. Safe to call every
        :meth:`record_tool_call`: write is atomic and cheap.
        """
        store = self._ensure_store()
        try:
            store.write_flat(
                self._NAMESPACE,
                f"{self._session_id}.json",
                asdict(self._state),
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("session_memory save failed: %s", exc)

    def load(self) -> None:
        """Restore state written by a previous :meth:`save`.

        Silently ignores missing / corrupt state — the instance keeps
        its fresh defaults in that case. The ring buffer is NOT
        restored (events are ephemeral, see module docstring).
        """
        store = self._ensure_store()
        data = store.read_flat(
            self._NAMESPACE,
            f"{self._session_id}.json",
            default={},
        )
        if not isinstance(data, dict):
            return
        try:
            self._state = SessionMemoryState(
                tool_count_total=int(data.get("tool_count_total", 0)),
                tool_count_at_last_update=int(data.get("tool_count_at_last_update", 0)),
                last_update_ts=float(data.get("last_update_ts", 0.0)),
                md_bytes=int(data.get("md_bytes", 0)),
                md_lines=int(data.get("md_lines", 0)),
                distill_successes=int(data.get("distill_successes", 0)),
                distill_failures=int(data.get("distill_failures", 0)),
            )
        except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
            logger.debug("session_memory load skipped malformed state: %s", exc)

    # ---- tool-call recording -------------------------------------------

    def record_tool_call(self, *, tool_name: str, summary: str) -> None:
        """Register a completed tool call.

        Appends ``f"{tool_name}: {summary}"`` to the bounded ring
        buffer (capacity 50), bumps :attr:`tool_count_total`, and
        persists state. The ring buffer is what the distiller sees in
        :attr:`DistillInput.recent_events`.

        This method is **not** filtered by tool name — unlike
        microcompact's ``COMPACTABLE_TOOLS`` gate, the distiller wants
        to see ALL activity (including Edit / Write / Task) so it can
        track decisions and files touched.
        """
        self._events.append(f"{tool_name}: {summary}")
        self._state.tool_count_total += 1
        self.save()

    # ---- trigger logic -------------------------------------------------

    def should_update(self) -> bool:
        """Return True when a distillation run should be scheduled.

        Two cases:

        1. **First run**: markdown file does not exist and
           ``tool_count_total >= init_threshold``.
        2. **Subsequent runs**: markdown file exists and
           ``tool_count_total - tool_count_at_last_update >= update_threshold``.

        No time / token component — callers that want a time gate
        should wrap this in their own debounce.

        2.7.2 Gap 4: user-facing ``memory_file_enabled`` kill switch.
        :func:`concinno.config.get("memory_file_enabled")` is False when
        the operator ran ``concinno config set memory_file_enabled false``
        (or set ``CONCINNO_MEMORY_FILE_ENABLED=0``). We return False
        unconditionally so :meth:`update` short-circuits before any
        distillation sink is invoked. Fail-soft: any config error keeps
        the existing behaviour (enabled).
        """
        try:
            from concinno.config import get as _cfg_get
            if _cfg_get("memory_file_enabled") is False:
                return False
        except Exception:
            pass
        if not self.md_path().exists():
            return self._state.tool_count_total >= self._init_threshold
        delta = self._state.tool_count_total - self._state.tool_count_at_last_update
        return delta >= self._update_threshold

    # ---- markdown truncation -------------------------------------------

    def truncate_content(self, content: str) -> str:
        """Apply ``max_lines`` then ``max_bytes`` caps.

        Two-pass mirror of ``memdir.ts:57`` ``truncateEntrypointContent``:

        1. **Line cap**: if the input has more than ``max_lines``
           lines, keep the first ``max_lines`` and append
           :data:`_LINE_TRUNC_WARNING`.
        2. **Byte cap**: if the UTF-8 encoding exceeds ``max_bytes``,
           find the last newline before the byte limit, slice there,
           and append :data:`_BYTE_TRUNC_WARNING`. Cutting at a
           newline avoids producing partial UTF-8 sequences and keeps
           the output parseable as markdown.

        When the byte-cap fallback can't find a newline (single giant
        line), we fall back to a safe UTF-8 prefix by decoding with
        ``errors="ignore"``.
        """
        # --- line pass ---
        lines = content.splitlines(keepends=True)
        truncated_by_lines = False
        if len(lines) > self._max_lines:
            lines = lines[: self._max_lines]
            truncated_by_lines = True
        result = "".join(lines)
        if truncated_by_lines:
            result = result + _LINE_TRUNC_WARNING

        # --- byte pass ---
        encoded = result.encode("utf-8")
        if len(encoded) <= self._max_bytes:
            return result

        # reserve room for the warning suffix so the final file is
        # still under the cap after we append it
        warning_bytes = _BYTE_TRUNC_WARNING.encode("utf-8")
        budget = self._max_bytes - len(warning_bytes)
        if budget <= 0:
            # pathological: the warning itself doesn't fit. Return an
            # empty-plus-warning file rather than crashing.
            return _BYTE_TRUNC_WARNING

        slice_ = encoded[:budget]
        # find the last newline inside the slice so we cut on a line
        # boundary (keeps markdown structure + avoids mid-char cuts
        # since newline is always a single 0x0A byte in UTF-8)
        nl_index = slice_.rfind(b"\n")
        if nl_index > 0:
            head_bytes = slice_[: nl_index + 1]
        else:
            # no newline in the slice: fall back to a safe UTF-8 prefix
            head_bytes = slice_
        head = head_bytes.decode("utf-8", errors="ignore")
        return head + _BYTE_TRUNC_WARNING

    # ---- reads ---------------------------------------------------------

    def read_summary(self) -> str:
        """Return the current markdown content, or ``""``.

        Empty string is returned when the file does not yet exist OR
        when a read error occurs (permission / corrupt). Callers that
        need to distinguish these cases should stat :meth:`md_path`.
        """
        path = self.md_path()
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:  # pragma: no cover - defensive
            logger.debug("session_memory read_summary failed: %s", exc)
            return ""

    # ---- update --------------------------------------------------------

    def update(self) -> DistillOutput | None:
        """Run a distillation pass if :meth:`should_update` is true.

        Returns ``None`` when the threshold has not been met. Returns
        a :class:`DistillOutput` otherwise — successful OR failed.
        Failed outputs still bump :attr:`SessionMemoryState.distill_failures`
        and are returned so the caller can log / surface the error.

        Raises:
            RuntimeError: If no sink has been configured.
        """
        if not self.should_update():
            return None
        if self._sink is None:
            msg = "no distill sink configured"
            raise RuntimeError(msg)

        inp = DistillInput(
            prior_summary=self.read_summary(),
            recent_events=list(self._events),
            tool_count=self._state.tool_count_total,
            max_bytes=self._max_bytes,
            max_lines=self._max_lines,
        )

        try:
            out = self._sink.distill(inp)
        except Exception as exc:
            logger.debug("session_memory sink.distill raised: %s", exc)
            self._state.distill_failures += 1
            self.save()
            return DistillOutput(markdown="", success=False, error=str(exc))

        if not out.success:
            self._state.distill_failures += 1
            self.save()
            return out

        # Defense-in-depth: enforce caps even if the sink already did.
        capped = self.truncate_content(out.markdown)
        path = self.md_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(capped, encoding="utf-8")

        encoded_len = len(capped.encode("utf-8"))
        line_count = len(capped.splitlines())

        self._state.md_bytes = encoded_len
        self._state.md_lines = line_count
        self._state.tool_count_at_last_update = self._state.tool_count_total
        self._state.last_update_ts = time.time()
        self._state.distill_successes += 1
        self.save()

        return DistillOutput(markdown=capped, success=True, error=None)

    # ---- diagnostics ---------------------------------------------------

    def stats(self) -> dict[str, int]:
        """Return a point-in-time stats snapshot.

        Keys mirror :class:`SessionMemoryState` plus the ring-buffer
        fill level (``recent_events_buffered``). ``last_update_ts`` is
        coerced to an ``int`` because :class:`dict` values are all
        ``int`` by the method signature — use :attr:`SessionMemoryState`
        directly if sub-second precision matters.
        """
        return {
            "tool_count_total": self._state.tool_count_total,
            "tool_count_at_last_update": self._state.tool_count_at_last_update,
            "last_update_ts": int(self._state.last_update_ts),
            "md_bytes": self._state.md_bytes,
            "md_lines": self._state.md_lines,
            "distill_successes": self._state.distill_successes,
            "distill_failures": self._state.distill_failures,
            "recent_events_buffered": len(self._events),
        }

    # ---- test / host introspection -------------------------------------

    @property
    def state(self) -> SessionMemoryState:
        """Expose the underlying state for hosts that need to inspect it."""
        return self._state
