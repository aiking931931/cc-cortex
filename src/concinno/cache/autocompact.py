"""concinno.cache.autocompact — Last-resort context auto-compaction.

@module cache.autocompact
@responsibility Port of Claude Code's ``autoCompact`` layer. When a
    conversation's token count approaches the model's context window
    limit (after microcompact and session_memory have already done what
    they can), this module decides whether to run a full LLM
    summarization pass that replaces old messages with a summary block
    and frees context. The decision engine lives here; the actual
    summarization call is delegated to a caller-supplied
    :class:`CompactSink` (same pattern as
    :class:`~concinno.cache.microcompact.CacheEditSink`).

    Four responsibilities, in strict order:

    1. **Budget reservation** — reserve ``AUTOCOMPACT_BUFFER_TOKENS``
       for the summarizer's own output. Compaction that runs right at
       the context ceiling has nowhere to put the summary.
    2. **Recursion guard** — ``source in RECURSION_GUARDED_SOURCES``
       returns ``noop``. The forked agents doing the compacting (and
       session_memory's own fork, and the context_collapse ctx-agent)
       would deadlock if autoCompact fired inside them.
    3. **Context-collapse mutual exclusion** — when the host opts into
       the newer context_collapse path, autoCompact must stand down so
       the two systems don't race on the same headroom budget.
    4. **Circuit breaker** — after ``MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES``
       consecutive failures (prompt_too_long etc.) the circuit opens and
       :meth:`AutoCompactor.run` raises :class:`AutoCompactExhausted`
       instead of hammering the API with doomed retries. Resetting the
       circuit requires manual intervention (see :meth:`AutoCompactor.load`
       notes).

@dependencies concinno.core.state_store
@exports AUTOCOMPACT_BUFFER_TOKENS, WARNING_THRESHOLD_BUFFER_TOKENS,
    ERROR_THRESHOLD_BUFFER_TOKENS, MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES,
    DEFAULT_MODEL_BUDGETS, QuerySource, RECURSION_GUARDED_SOURCES,
    CompactRequest, CompactResult, CompactSink, AutoCompactState,
    AutoCompactCircuitBreaker, ContextCollapseActive, AutoCompactExhausted,
    AutoCompactor

Design notes (porting contract with ``autoCompact.ts``):

- The TS source threads ``AutoCompactTrackingState`` through the query
  loop as a plain struct. The Python port collapses that into an
  :class:`AutoCompactState` dataclass owned by :class:`AutoCompactor`
  and persisted via :class:`~concinno.core.state_store.StateStore`.
- ``isAutoCompactEnabled`` / env-var overrides do not cross the library
  boundary. Hosts that want to disable autoCompact simply don't call
  :meth:`AutoCompactor.run`.
- The TS circuit breaker trips silently (returns ``wasCompacted=False``);
  the Python port raises :class:`AutoCompactExhausted` so the caller
  can make an explicit "over the limit, give up" decision rather than
  looping forever. A no-op return would be indistinguishable from
  "below threshold, nothing to do".
- ``ContextCollapseActive`` is raised (rather than a silent ``noop``)
  only when the caller explicitly invokes :meth:`run`. :meth:`should_trigger`
  still returns ``"noop"`` so dashboard / metric code can poll the state
  without triggering an exception flow.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Protocol

logger = logging.getLogger("concinno.cache.autocompact")

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Tokens reserved for the summarizer's own output. Based on p99.99 of
#: compact summary output being ~17k tokens in production Claude Code.
#: The library uses 13k as the floor (headroom under 17k comes from the
#: warning/error buffers below).
AUTOCOMPACT_BUFFER_TOKENS = 13_000

#: Tokens reserved below the effective context window for the
#: "warning" zone. Above ``model_budget - buffer - warning`` the
#: compactor will run a warning-level summarization.
WARNING_THRESHOLD_BUFFER_TOKENS = 20_000

#: Tokens reserved for the "error" zone. Above
#: ``model_budget - buffer`` the context is past the point where the
#: warning pass was supposed to save us; the error-level pass runs with
#: a tighter target.
ERROR_THRESHOLD_BUFFER_TOKENS = 20_000

#: Circuit-breaker trip count. After this many consecutive
#: summarization failures in one session we stop retrying and let the
#: caller give up cleanly. BQ 2026-03-10 data: without this, 1,279
#: sessions hit 50+ failures (up to 3,272) each, wasting ~250K API
#: calls/day globally.
MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3

#: Model name → effective context window (total tokens). Callers can
#: skip this lookup by passing ``model_budget`` directly to the
#: :class:`AutoCompactor` constructor.
#: Default is the 200K standard context window available to all tiers.
#: The 1M window on Opus/Sonnet is a beta-gated feature (header
#: ``context-1m-2025-08-07``, tier ≥4 rate limit). Library consumers
#: without 1M access would silently miss their 200K ceiling if we
#: defaulted to 1M — auto-compaction would not trigger, user's call
#: would hit Anthropic's 400 "prompt too long" and the circuit
#: breaker would kill the autocompact path entirely. Opt-in via
#: env ``CONCINNO_OPUS_1M_BETA=1`` raises the Opus/Sonnet budget.
DEFAULT_MODEL_BUDGETS: dict[str, int] = {
    "claude-opus-4-7": 200_000,
    "claude-opus-4-6": 200_000,
    "claude-sonnet-4-6": 200_000,
    "claude-haiku-4-5": 200_000,
}

#: Opt-in 1M context window for consumers with beta access.
_OPUS_1M_BETA_BUDGET = 1_000_000
_OPUS_1M_BETA_ENV = "CONCINNO_OPUS_1M_BETA"


def _budget_for_model(model: str) -> int:
    """Return the effective context-window budget for ``model``.

    Honours the ``CONCINNO_OPUS_1M_BETA=1`` env opt-in by bumping
    Opus 4.6+ and Sonnet 4.6+ to 1M tokens. Unknown models fall
    back to :data:`_UNKNOWN_MODEL_BUDGET_FALLBACK`.
    """
    import os  # lazy — keeps module import cheap

    base = DEFAULT_MODEL_BUDGETS.get(model, _UNKNOWN_MODEL_BUDGET_FALLBACK)
    if os.environ.get(_OPUS_1M_BETA_ENV) == "1" and model in (
        "claude-opus-4-7",
        "claude-opus-4-6",
        "claude-sonnet-4-6",
    ):
        return _OPUS_1M_BETA_BUDGET
    return base

#: Fallback context window used when ``model`` is unknown.
_UNKNOWN_MODEL_BUDGET_FALLBACK = 200_000

QuerySource = Literal[
    "main",
    "session_memory",
    "compact",
    "prompt_suggestion",
    "ctx_agent",
    "other",
]

#: Sources that MUST NOT trigger autoCompact. Both ``session_memory``
#: and ``compact`` are forked agents owned by autoCompact's own
#: pipeline — firing again inside them would deadlock the pipeline on
#: its own output. ``ctx_agent`` is the context_collapse ctx-agent,
#: whose compaction firing would destroy the main thread's committed
#: log. Mirrors ``autoCompact.ts:171-182``.
RECURSION_GUARDED_SOURCES: frozenset[QuerySource] = frozenset(
    {"session_memory", "compact", "ctx_agent"}
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class CompactRequest:
    """What the caller hands the summarizer sink.

    Attributes:
        model: Model name whose context we're compacting. The sink may
            or may not use this to pick a summarization model.
        current_tokens: Current token count at trigger time. Diagnostic —
            the sink should not use this to decide whether to act.
        target_tokens: Goal token count AFTER the summary replaces old
            messages. Computed by the caller as
            ``model_budget - buffer_tokens - error_threshold``.
        reason: ``"warning"`` on the first pass, ``"error"`` if the
            warning pass didn't free enough and we're now in the
            tighter zone, ``"error_retry"`` reserved for future retry
            paths that bypass the circuit breaker.
        metadata: Free-form annotations for analytics / tracing.
    """

    model: str
    current_tokens: int
    target_tokens: int
    reason: Literal["warning", "error", "error_retry"]
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass
class CompactResult:
    """What the sink returns after a summarization attempt.

    Attributes:
        success: ``True`` when the new summary was successfully applied
            to the conversation. On ``False`` the compactor increments
            the consecutive-failure counter.
        summary_tokens: Tokens in the new summary block that replaced
            the old messages. Diagnostic only.
        reclaimed_tokens: Tokens freed by the compaction (old content
            tokens minus summary_tokens). Accumulated into
            :attr:`AutoCompactState.total_reclaimed_tokens`.
        error: Human-readable failure reason (``None`` on success).
    """

    success: bool
    summary_tokens: int
    reclaimed_tokens: int
    error: str | None = None


class CompactSink(Protocol):
    """Caller-supplied bridge to the summarization LLM call.

    CCC deliberately does not depend on any network client. Concrete
    sinks live in the consumer (a hook, a proxy, a test fake) and are
    injected into :class:`AutoCompactor`. This keeps the library
    zero-dep and portable.

    Implementations should be synchronous. Exceptions raised from
    ``summarize`` are caught by :meth:`AutoCompactor.run` and turned
    into a failed :class:`CompactResult` for the circuit-breaker
    bookkeeping.
    """

    def summarize(self, req: CompactRequest) -> CompactResult:
        ...


@dataclass
class AutoCompactCircuitBreaker:
    """Standalone, persistable circuit breaker for autocompact.

    Separate from :class:`AutoCompactState` on purpose: the breaker
    is a pure guard (no reclaim / trigger bookkeeping) and is persisted
    under its own StateStore namespace (``autocompact_breaker``) so an
    ops operator can flip ``disabled`` back to ``False`` without
    touching the rest of the autocompact state file.

    The trip semantics match ``autoCompact.ts:67-70``: after
    ``max_failures`` consecutive failures the breaker opens and
    :meth:`should_attempt` returns ``False`` until :meth:`reset` is
    called manually.

    Attributes:
        max_failures: Trip count. Defaults to
            :data:`MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES` (3, matching
            BQ 2026-03-10's root-cause fix).
        failures: Consecutive-failure counter. Reset to 0 on
            :meth:`record_success`, not on :meth:`reset` (reset also
            clears ``disabled``, which is the stronger signal).
        disabled: Latched circuit state. Once ``True`` only
            :meth:`reset` clears it — a single success after a trip
            does NOT re-enable the compactor. This mirrors the TS
            behaviour: once the conversation is over the ceiling,
            more summarization attempts are rarely the right answer.
        last_failure_reason: Human-readable last error for diagnostics.
        disabled_at_session: Session ID that tripped the breaker.
            ``None`` when the breaker is not currently disabled.
    """

    max_failures: int = MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES
    failures: int = 0
    disabled: bool = False
    last_failure_reason: str = ""
    disabled_at_session: str | None = None

    def record_failure(self, reason: str, session_id: str) -> bool:
        """Increment the failure counter and return True on trip.

        Args:
            reason: Free-form failure description. Stored in
                :attr:`last_failure_reason` for diagnostics.
            session_id: Session that observed the failure. Recorded
                in :attr:`disabled_at_session` on trip.

        Returns:
            ``True`` iff THIS call was the one that tripped the
            breaker (``failures`` reached ``max_failures``). ``False``
            on the first N-1 failures, and also ``False`` on
            subsequent failures after an already-open breaker
            (those are observability noise, not new trips).
        """
        self.last_failure_reason = str(reason)
        if self.disabled:
            return False
        self.failures += 1
        if self.failures >= self.max_failures:
            self.disabled = True
            self.disabled_at_session = session_id
            return True
        return False

    def record_success(self) -> None:
        """Reset the failure counter (but NOT the disabled flag).

        A success after the breaker has tripped is treated as
        ambiguous — the summarizer might have succeeded on a tiny
        turn but the underlying overflow is still present. Ops must
        call :meth:`reset` to explicitly re-enable autocompact.
        """
        self.failures = 0

    def should_attempt(self) -> bool:
        """Return ``False`` when the circuit is open."""
        return not self.disabled

    def reset(self) -> None:
        """Force-clear the breaker. For ops / recovery use only.

        Clears ``failures``, ``disabled``, ``last_failure_reason``
        and ``disabled_at_session`` — the caller is asserting that
        whatever caused the overflow has been addressed.
        """
        self.failures = 0
        self.disabled = False
        self.last_failure_reason = ""
        self.disabled_at_session = None


@dataclass
class AutoCompactState:
    """Per-session circuit state.

    Persisted via :class:`~concinno.core.state_store.StateStore` so a
    host restart doesn't forget that the circuit was open. Resetting
    the circuit manually means calling :meth:`AutoCompactor.load`
    against a freshly-edited state file, or constructing a new
    :class:`AutoCompactor` with a different ``session_id``.
    """

    consecutive_failures: int = 0
    total_compactions: int = 0
    total_reclaimed_tokens: int = 0
    last_trigger_ts: float = 0.0
    last_error: str = ""
    circuit_open: bool = False


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ContextCollapseActive(RuntimeError):
    """Raised by :meth:`AutoCompactor.run` when ``context_collapse_active``
    is set AND the caller's source is one that context_collapse would
    otherwise own. The caller should fall through to the collapse flow
    rather than retrying autoCompact."""


class AutoCompactExhausted(RuntimeError):
    """Raised by :meth:`AutoCompactor.run` when
    ``consecutive_failures >= MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES``.
    Context is irrecoverably over limit from autoCompact's perspective
    and the caller must escalate (blocking error, session end, etc.)."""


# ---------------------------------------------------------------------------
# AutoCompactor
# ---------------------------------------------------------------------------


class AutoCompactor:
    """Decide, guard, and invoke the last-resort compaction pass.

    Args:
        cache_dir: Root directory for persistent state. When ``None``,
            :meth:`save` / :meth:`load` become no-ops. Useful for
            transient sessions and tests.
        session_id: Logical session identifier. Scoped state files are
            written under
            ``{cache_dir}/autocompact/{session_id[:8]}.json``.
        model: Model name used for the budget lookup in
            :data:`DEFAULT_MODEL_BUDGETS`. Ignored when ``model_budget``
            is passed explicitly.
        model_budget: Explicit context window size. When provided,
            overrides ``DEFAULT_MODEL_BUDGETS[model]``.
        buffer_tokens: Tokens reserved for the summarizer's own output.
        warning_threshold: Tokens reserved below the effective window
            for the warning zone.
        error_threshold: Tokens reserved below the effective window for
            the error zone.
        max_consecutive_failures: Circuit-breaker trip count.
        context_collapse_active: Host opt-in flag. When ``True`` the
            compactor stands down (returns ``"noop"`` from
            :meth:`should_trigger` and raises
            :class:`ContextCollapseActive` from :meth:`run` for
            collapse-eligible sources).
        sink: Concrete :class:`CompactSink`. When ``None``,
            :meth:`run` raises ``RuntimeError`` at the point of call
            (not at construction, so callers can build a decision-only
            compactor for tests / dashboards).
    """

    _NAMESPACE = "autocompact"
    #: Separate StateStore namespace for the standalone
    #: :class:`AutoCompactCircuitBreaker`. Persisted independently so
    #: ops can flip the breaker without touching the main state file.
    _NAMESPACE_BREAKER = "autocompact_breaker"

    def __init__(
        self,
        *,
        cache_dir: str | None = None,
        session_id: str | None = None,
        model: str = "claude-opus-4-6",
        model_budget: int | None = None,
        buffer_tokens: int = AUTOCOMPACT_BUFFER_TOKENS,
        warning_threshold: int = WARNING_THRESHOLD_BUFFER_TOKENS,
        error_threshold: int = ERROR_THRESHOLD_BUFFER_TOKENS,
        max_consecutive_failures: int = MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES,
        context_collapse_active: bool = False,
        sink: CompactSink | None = None,
        breaker: AutoCompactCircuitBreaker | None = None,
    ) -> None:
        self._cache_dir = cache_dir
        self._session_id = session_id or "unknown"
        self._model = model
        self._buffer_tokens = int(buffer_tokens)
        self._warning_threshold = int(warning_threshold)
        self._error_threshold = int(error_threshold)
        self._max_consecutive = int(max_consecutive_failures)
        self._context_collapse_active = bool(context_collapse_active)
        self._sink = sink

        if model_budget is not None:
            self._model_budget = int(model_budget)
        else:
            looked_up = DEFAULT_MODEL_BUDGETS.get(model)
            if looked_up is None:
                logger.warning(
                    "autocompact: unknown model %r — falling back to %d tokens",
                    model,
                    _UNKNOWN_MODEL_BUDGET_FALLBACK,
                )
                self._model_budget = _UNKNOWN_MODEL_BUDGET_FALLBACK
            else:
                self._model_budget = int(looked_up)

        self._state = AutoCompactState()
        self._store: Any = None  # lazy — avoids init cost when no persistence

        # Breaker composition: caller may inject a pre-existing
        # instance (e.g. already loaded from disk in an ops script)
        # or let us build a default one sized by ``max_consecutive_failures``.
        if breaker is not None:
            self._breaker = breaker
        else:
            self._breaker = AutoCompactCircuitBreaker(
                max_failures=int(max_consecutive_failures),
            )

    # ---- threshold math -------------------------------------------------

    def get_autocompact_threshold(self) -> int:
        """Return the "below this, don't bother" token floor.

        Formula: ``model_budget - buffer_tokens - warning_threshold``.
        Below this value :meth:`should_trigger` returns ``"noop"`` and
        :meth:`run` returns ``None``.
        """
        return self._model_budget - self._buffer_tokens - self._warning_threshold

    @property
    def model_budget(self) -> int:
        """Resolved context window for the configured model."""
        return self._model_budget

    # ---- lifecycle ------------------------------------------------------

    def _ensure_store(self) -> Any:
        if self._cache_dir is None:
            return None
        if self._store is None:
            from concinno.core.state_store import StateStore

            self._store = StateStore(self._cache_dir)
        return self._store

    def save(self) -> None:
        """Persist current state AND breaker. No-op when ``cache_dir`` is ``None``.

        Breaker is persisted under a separate namespace
        (:attr:`_NAMESPACE_BREAKER`) so an ops operator editing the
        breaker file doesn't risk stomping the reclaim counters, and
        vice versa.
        """
        store = self._ensure_store()
        if store is None:
            return
        filename = f"{self._session_id[:8] or 'unknown'}.json"
        payload = asdict(self._state)
        try:
            store.write_flat(self._NAMESPACE, filename, payload)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("autocompact save failed: %s", exc)

        breaker_payload = asdict(self._breaker)
        try:
            store.write_flat(
                self._NAMESPACE_BREAKER,
                filename,
                breaker_payload,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("autocompact breaker save failed: %s", exc)

    def load(self) -> None:
        """Restore state written by a previous :meth:`save`.

        Silently ignores missing / corrupt state. Intended side-effect:
        a host operator can edit the persisted JSON (e.g. set
        ``circuit_open`` to ``false``) and then construct a new
        :class:`AutoCompactor` that calls :meth:`load` to pick up the
        manual override.
        """
        store = self._ensure_store()
        if store is None:
            return
        data = store.read_flat(
            self._NAMESPACE,
            f"{self._session_id[:8] or 'unknown'}.json",
            default={},
        )
        if not isinstance(data, dict):
            return
        try:
            self._state = AutoCompactState(
                consecutive_failures=int(data.get("consecutive_failures", 0)),
                total_compactions=int(data.get("total_compactions", 0)),
                total_reclaimed_tokens=int(data.get("total_reclaimed_tokens", 0)),
                last_trigger_ts=float(data.get("last_trigger_ts", 0.0)),
                last_error=str(data.get("last_error", "")),
                circuit_open=bool(data.get("circuit_open", False)),
            )
        except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
            logger.debug("autocompact load skipped malformed state: %s", exc)

        # Separate breaker file. Absence → leave the in-memory
        # default untouched (fresh session).
        breaker_data = store.read_flat(
            self._NAMESPACE_BREAKER,
            f"{self._session_id[:8] or 'unknown'}.json",
            default=None,
        )
        if isinstance(breaker_data, dict):
            try:
                self._breaker = AutoCompactCircuitBreaker(
                    max_failures=int(
                        breaker_data.get(
                            "max_failures",
                            self._breaker.max_failures,
                        )
                    ),
                    failures=int(breaker_data.get("failures", 0)),
                    disabled=bool(breaker_data.get("disabled", False)),
                    last_failure_reason=str(
                        breaker_data.get("last_failure_reason", "")
                    ),
                    disabled_at_session=breaker_data.get(
                        "disabled_at_session"
                    ),
                )
            except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
                logger.debug(
                    "autocompact breaker load skipped malformed state: %s",
                    exc,
                )

    # ---- decision -------------------------------------------------------

    def should_trigger(
        self,
        *,
        current_tokens: int,
        source: QuerySource = "main",
    ) -> Literal["noop", "warning", "error"]:
        """Classify the current token level without side effects.

        Decision order (must match :meth:`run` guard order except for
        the circuit check, which is :meth:`run`-only):

        1. ``source`` in :data:`RECURSION_GUARDED_SOURCES` → ``"noop"``
           (hard fork-guard — these sources would deadlock).
        2. ``context_collapse_active`` is set → ``"noop"``
           (mutual exclusion with the newer collapse path).
        3. ``current_tokens < threshold`` → ``"noop"``
           (where threshold = :meth:`get_autocompact_threshold`).
        4. ``current_tokens < model_budget - buffer_tokens``
           → ``"warning"`` (first pass, soft target).
        5. otherwise → ``"error"`` (second pass, hard target).
        """
        if source in RECURSION_GUARDED_SOURCES:
            return "noop"
        if self._context_collapse_active:
            return "noop"
        # 2.7.2 Gap 4: user-facing ``auto_compact`` kill switch.
        # :func:`concinno.config.get("auto_compact")` is False when the
        # operator ran ``concinno config set auto_compact false`` (or
        # set ``CONCINNO_AUTO_COMPACT=0``). Treat as permanent ``"noop"``
        # so :meth:`run` short-circuits before touching the sink.
        # Fail-soft: any config error falls back to "enabled" (current
        # behaviour) — the gate never blocks compaction on its own.
        try:
            from concinno.config import get as _cfg_get
            if _cfg_get("auto_compact") is False:
                return "noop"
        except Exception:
            pass

        threshold = self.get_autocompact_threshold()
        tokens = int(current_tokens)
        if tokens < threshold:
            return "noop"

        error_floor = self._model_budget - self._buffer_tokens
        if tokens < error_floor:
            return "warning"
        return "error"

    # ---- run ------------------------------------------------------------

    def _target_tokens(self) -> int:
        """Compute the post-compaction token target.

        Formula: ``model_budget - buffer_tokens - error_threshold``.
        The summarizer aims to leave the conversation below this
        value so the next turn has real breathing room rather than
        landing exactly on the error floor.
        """
        return self._model_budget - self._buffer_tokens - self._error_threshold

    def run(
        self,
        *,
        current_tokens: int,
        source: QuerySource = "main",
    ) -> CompactResult | None:
        """Evaluate, guard, and (if needed) invoke the summarizer.

        Returns:
            ``None`` when :meth:`should_trigger` returned ``"noop"``
            (no work to do). A :class:`CompactResult` otherwise — the
            exact object the sink returned, augmented with any
            state-bookkeeping side effects.

        Raises:
            ContextCollapseActive: When ``context_collapse_active`` is
                set and ``source`` is not already in
                :data:`RECURSION_GUARDED_SOURCES`. The caller should
                fall through to the collapse flow.
            AutoCompactExhausted: When the circuit breaker is open
                (``state.consecutive_failures`` already hit
                ``max_consecutive_failures``) or when this call is the
                one that trips it.
            RuntimeError: When ``sink`` is ``None`` but work needs
                doing.
        """
        # ---- ordering rationale ----
        # Recursion guard comes BEFORE the circuit check on purpose.
        # A forked compact agent whose own context is blowing up MUST
        # bail silently (noop) rather than raise — raising would bubble
        # up into the summarization pipeline we're supposed to be
        # supporting and cascade into a real failure. The circuit
        # breaker is for the main thread's bookkeeping, not the forks.
        if source in RECURSION_GUARDED_SOURCES:
            return None

        # Context-collapse mutual exclusion. ``should_trigger`` merely
        # returns noop for polling callers, but the explicit ``run``
        # path signals with an exception so the caller can wire the
        # fallback path deliberately.
        if self._context_collapse_active:
            raise ContextCollapseActive(
                "context_collapse is active; autocompact is standing down"
            )

        # Circuit breaker. Check AFTER the fork/collapse guards so the
        # two mutual-exclusion paths aren't blocked by a prior failure
        # spiral. Two checks run in lockstep for backward compat:
        # the legacy ``_state.circuit_open`` flag AND the standalone
        # :class:`AutoCompactCircuitBreaker`. Either tripping is
        # enough to raise — the breaker is strictly additive.
        if self._state.circuit_open or not self._breaker.should_attempt():
            logger.warning(
                "autocompact: breaker open, skipping run "
                "(failures=%d, disabled=%s, session=%s)",
                self._breaker.failures,
                self._breaker.disabled,
                self._breaker.disabled_at_session,
            )
            raise AutoCompactExhausted(
                f"autocompact circuit open after "
                f"{self._state.consecutive_failures} consecutive failures; "
                f"last_error={self._state.last_error!r}"
            )

        level = self.should_trigger(current_tokens=current_tokens, source=source)
        if level == "noop":
            return None

        if self._sink is None:
            msg = "no sink configured; AutoCompactor.run requires a CompactSink"
            raise RuntimeError(msg)

        req = CompactRequest(
            model=self._model,
            current_tokens=int(current_tokens),
            target_tokens=self._target_tokens(),
            reason=level,
        )

        self._state.last_trigger_ts = time.monotonic()

        try:
            result = self._sink.summarize(req)
        except Exception as exc:
            # Sink raised — treat as failure for circuit bookkeeping
            # rather than letting the exception escape. The caller
            # gets a structured failure via AutoCompactExhausted when
            # the counter trips.
            result = CompactResult(
                success=False,
                summary_tokens=0,
                reclaimed_tokens=0,
                error=f"sink raised: {exc!r}",
            )

        if result.success:
            self._state.consecutive_failures = 0
            self._state.total_compactions += 1
            self._state.total_reclaimed_tokens += int(result.reclaimed_tokens)
            self._state.last_error = ""
            self._breaker.record_success()
            self.save()
            return result

        # Failure path. Update both the legacy inline counter and the
        # standalone breaker — they are kept in lockstep so existing
        # callers reading ``state.circuit_open`` / ``consecutive_failures``
        # continue to see correct values.
        self._state.consecutive_failures += 1
        self._state.last_error = result.error or "unknown"
        tripped = self._breaker.record_failure(
            reason=self._state.last_error,
            session_id=self._session_id,
        )
        if self._state.consecutive_failures >= self._max_consecutive or tripped:
            self._state.circuit_open = True
            logger.warning(
                "autocompact: circuit breaker tripped after %d consecutive "
                "failures; last_error=%r",
                self._state.consecutive_failures,
                self._state.last_error,
            )
            self.save()
            raise AutoCompactExhausted(
                f"autocompact failed {self._state.consecutive_failures} "
                f"times in a row; last_error={self._state.last_error!r}"
            )
        self.save()
        return result

    # ---- diagnostics ----------------------------------------------------

    def stats(self) -> dict[str, int | bool]:
        """Return a point-in-time counter snapshot.

        Keys:

        - ``consecutive_failures`` — current failure run.
        - ``total_compactions`` — successful summarizations this
          session.
        - ``total_reclaimed_tokens`` — cumulative
          :attr:`CompactResult.reclaimed_tokens` over successful runs.
        - ``circuit_open`` — circuit-breaker state.
        - ``model_budget`` — resolved context window.
        - ``threshold`` — :meth:`get_autocompact_threshold` value.
        """
        return {
            "consecutive_failures": self._state.consecutive_failures,
            "total_compactions": self._state.total_compactions,
            "total_reclaimed_tokens": self._state.total_reclaimed_tokens,
            "circuit_open": self._state.circuit_open,
            "model_budget": self._model_budget,
            "threshold": self.get_autocompact_threshold(),
        }

    # ---- test / host introspection -------------------------------------

    @property
    def state(self) -> AutoCompactState:
        """Expose the underlying state for hosts that need to inspect
        or manually edit it. Intended primarily for tests and recovery
        scripts. Mutating the returned object bypasses all guards.
        """
        return self._state

    @property
    def breaker(self) -> AutoCompactCircuitBreaker:
        """Expose the standalone circuit breaker.

        Hosts can call :meth:`AutoCompactCircuitBreaker.reset` on the
        returned instance to manually re-enable autocompact after an
        operator has addressed whatever caused the overflow. Note
        that resetting the standalone breaker does NOT clear the
        legacy ``state.circuit_open`` flag — callers wanting a full
        clean slate should use :meth:`reset_breaker`.
        """
        return self._breaker

    def reset_breaker(self) -> None:
        """Force-clear the breaker AND legacy state flags, then persist.

        Use after an ops operator has verified the underlying
        overflow condition is resolved (e.g., session truncated
        manually, caller switched to context_collapse, etc.). This
        is the only supported path back to a healthy compactor
        short of constructing a fresh instance.
        """
        self._breaker.reset()
        self._state.circuit_open = False
        self._state.consecutive_failures = 0
        self._state.last_error = ""
        self.save()
