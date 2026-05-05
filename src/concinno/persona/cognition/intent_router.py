"""concinno.persona.cognition.intent_router — Cleanroom port of Module A.

@module concinno.persona.cognition.intent_router
@responsibility Decide which work happens in the foreground (visible reply
    path) vs the background (state / memory / affect updates) for one
    persona turn. Equivalent to the TS ``consciousness-router`` contract
    described in spec §2.2 Module A — the *behaviour* is preserved; the
    naming, structure, and implementation are written from scratch in
    Python.
@dependencies stdlib only — ``asyncio``, ``dataclasses``, ``re``, ``time``,
    ``typing``. Optional ``concinno.agent.session_loop.LLMDriver`` Protocol
    is used purely as a structural type hint; no import-time dependency.
@exports IntentRouter, IntentRouteInput, IntentRouteOutput, DispatchDecision,
    BackgroundTask, MessageSignals, ProcessingLayer

Why a port (and not a transpile)
--------------------------------
The original TS module mixes its routing heuristics with PSYCHE-specific
naming that is **forbidden** from this OSS surface (death command #1 in
the parent decision doc). We re-implement the *contract* — five priority
heuristics, parallel background dispatch, foreground-context assembly —
using neutral Python idioms (dataclasses, ``re``, ``asyncio.gather``).

Six DoD compliance
------------------
1. **Switchable** — :class:`IntentRouter` accepts an optional ``llm_driver``
   conforming to ``concinno.agent.session_loop.LLMDriver``. ``None`` keeps
   routing pure-heuristic; passing a driver lets future variants escalate
   ambiguous cases. The route step itself is heuristic-only and never
   blocks.
2. **ZIQ-aligned** — every dispatch decision emits a tunable outcome to
   ``ziq_outcome_bus`` so the routing thresholds become online-learnable.
   Keyed under ``persona.intent_router.dispatch_layer``. Manual override
   still wins (per ``ziq_outcome_bus`` pin semantics).
3. **3-layer classification** — ``cognition/__init__.py`` (index) →
   :class:`IntentRouter` (summary) → :class:`IntentRouteInput` /
   :class:`IntentRouteOutput` dataclasses (full).
4. **Lazy-load** — no top-level imports of ``ziq_outcome_bus``; the bus
   is fetched inside ``_emit_outcome`` and any failure is swallowed so
   the router stays usable even when the bus is unavailable.
5. **CP-optimal** — pure-Python regex + dispatch table beats both a
   custom NLP pipeline (over-engineered) and a per-turn LLM call
   (slow / costly). Heuristic latency is sub-millisecond.
6. **CBUA-optimal** — Simple/Complicated path, no ToT branching. Routing
   is a B0 / B1 boundary by design.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "BackgroundTask",
    "DispatchDecision",
    "IntentRouteInput",
    "IntentRouteOutput",
    "IntentRouter",
    "MessageSignals",
    "ProcessingLayer",
]


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class ProcessingLayer(str, Enum):
    """Where a single piece of work executes for one turn.

    * ``FOREGROUND`` — visible LLM call that produces the user-facing
      reply. Always exactly one per turn.
    * ``BACKGROUND`` — out-of-band state / memory / affect work that
      must not block the reply. Zero or more per turn, dispatched in
      parallel.
    * ``HYBRID`` — both layers fire; foreground waits for nothing,
      background runs alongside and its result is merged into the
      *next* turn's foreground context.
    """

    FOREGROUND = "foreground"
    BACKGROUND = "background"
    HYBRID = "hybrid"


@dataclass(frozen=True)
class MessageSignals:
    """Pure-heuristic 0-cost classification signals from the user message.

    Flat boolean record — every field is independently testable.
    Equivalent to the TS ``classifyMessage`` contract.
    """

    is_emotional: bool = False
    needs_tool: bool = False
    is_command: bool = False
    is_trivial: bool = False
    is_question: bool = False
    has_memory_trigger: bool = False
    char_length: int = 0


@dataclass(frozen=True)
class BackgroundTask:
    """One unit of background work to dispatch.

    ``type`` is an open string — consumers register handlers via
    :meth:`IntentRouter.register_background_handler`. Built-in known
    types (handler-agnostic, just naming convention):

    * ``"memory_consolidation"`` — summarise turn into long-term store
    * ``"affect_update"`` — update affect / mood baseline
    * ``"recall"`` — fetch relevant pinned memories for next turn
    """

    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    priority: int = 0  # higher = run first when handlers serialize

    def __post_init__(self) -> None:
        if not self.type or not isinstance(self.type, str):
            raise ValueError("BackgroundTask.type must be a non-empty string")


@dataclass(frozen=True)
class DispatchDecision:
    """The router's verdict for one turn.

    :param layer: Which layer(s) fire this turn.
    :param reason: Short tag explaining which heuristic chose ``layer``.
        Used for ZIQ outcome metadata + audit logs. Stable string — do
        not localise.
    :param signals: The :class:`MessageSignals` snapshot the decision
        was made from. Preserved so callers can persist it for
        reproduction / debugging.
    """

    layer: ProcessingLayer
    reason: str
    signals: MessageSignals


@dataclass(frozen=True)
class IntentRouteInput:
    """Input contract for :meth:`IntentRouter.route`.

    :param user_message: Raw user text for this turn.
    :param persona_id: Persona slug — used for ZIQ outcome partitioning.
    :param conversation_history: Recent turns as ``{"role", "content"}``
        dicts (Anthropic-shape). May be empty.
    :param turn_index: 0-based turn counter. Used to detect first-turn
        cold start (memory_consolidation skipped).
    :param force_layer: Optional override that bypasses heuristics. None
        = run full router.
    """

    user_message: str
    persona_id: str
    conversation_history: tuple[dict[str, str], ...] = ()
    turn_index: int = 0
    force_layer: ProcessingLayer | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.user_message, str):
            raise TypeError("user_message must be str")
        if not self.persona_id or not isinstance(self.persona_id, str):
            raise ValueError("persona_id must be a non-empty string")
        if self.turn_index < 0:
            raise ValueError("turn_index must be >= 0")


@dataclass(frozen=True)
class IntentRouteOutput:
    """Output contract for :meth:`IntentRouter.route`.

    :param decision: The :class:`DispatchDecision` produced.
    :param foreground_task: Always present — the visible-reply task
        descriptor (a string label; the consumer decides what LLM call
        to issue with it).
    :param background_tasks: Zero or more :class:`BackgroundTask` to
        run in parallel via :meth:`IntentRouter.execute_background`.
    :param context_brief: Foreground-context assembly hint, derived from
        history + signals. Empty string when nothing to inject.
    """

    decision: DispatchDecision
    foreground_task: str
    background_tasks: tuple[BackgroundTask, ...] = ()
    context_brief: str = ""


# ---------------------------------------------------------------------------
# Optional driver Protocol (structural — no import dependency)
# ---------------------------------------------------------------------------


@runtime_checkable
class _MinimalLLMDriver(Protocol):
    """Local structural shadow of ``concinno.agent.session_loop.LLMDriver``.

    Declared here so the router does not import ``concinno.agent`` at
    module load. Any object with ``model_id`` + ``complete`` satisfies
    this Protocol — including the canonical ``LLMDriver``.
    """

    @property
    def model_id(self) -> str: ...  # pragma: no cover

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> Any: ...  # pragma: no cover


# ---------------------------------------------------------------------------
# Heuristic constants
# ---------------------------------------------------------------------------

# Trivial = short, low-information user message. Fast-path foreground only.
_TRIVIAL_MAX_CHARS = 12
_TRIVIAL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"^\s*(ok(ay)?|k|kk|got it|sure|yeah|yep|nope|no|yes|mm+|hmm+|haha+|lol)\s*[.!?]*\s*$",
        r"^\s*(thanks?|thank you|ty|cheers)\s*[.!?]*\s*$",
        r"^\s*(hi|hello|hey|yo|sup)\s*[.!?]*\s*$",
        r"^\s*[.!?]+\s*$",
    )
)

# Emotional cue regex (English + zh hint set; deliberately small).
_EMOTIONAL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(angry|sad|happy|anxious|worried|scared|frustrated|excited|love|hate|hurt|cry|tear)\b",
        r"\b(why (do|does|did|am|is|are) you|i feel|i'?m feeling|i can'?t stand|i miss)\b",
    )
)

# Tool-need heuristic: explicit calculation, lookup, search, file ops.
_TOOL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(search|look up|find|fetch|download|calculate|compute|run|execute)\b",
        r"\b(what is the (price|weather|time|date)|how many|how much)\b",
    )
)

# Slash-style imperatives or terse commands.
_COMMAND_PATTERN: re.Pattern[str] = re.compile(
    r"^\s*(/|!)[a-zA-Z][\w-]{1,32}\b"
)

# Question marker — interrogative punctuation OR leading wh-word.
_QUESTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\?\s*$"),
    re.compile(
        r"^\s*(who|what|where|when|why|how|is|are|do|does|did"
        r"|can|could|would|should)\b",
        re.IGNORECASE,
    ),
)

# "Remember / forget / I told you" — memory-recall trigger.
_MEMORY_TRIGGER_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(remember|recall|forgot|forget|earlier|before|last time|previously)\b",
        r"\b(i (told|said|mentioned)|as i (told|said))\b",
    )
)

# Default context-brief budget (chars) — keep small; consumers may grow.
_CONTEXT_BRIEF_MAX_CHARS = 800


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


_BackgroundHandler = Callable[[BackgroundTask], Awaitable[Any] | Any]


class IntentRouter:
    """Decide foreground vs background work for one persona turn.

    Routing is pure-heuristic and synchronous; background dispatch is
    async-aware and uses ``asyncio.gather`` for parallelism. The router
    is reusable across personas — there is no per-instance per-persona
    state held here. State lives in the consumer's persona session.

    Construction:

        router = IntentRouter()                  # heuristic only
        router = IntentRouter(llm_driver=...)    # carries a driver for
                                                 # consumer use (e.g. the
                                                 # foreground call); the
                                                 # router itself never
                                                 # invokes the driver.
    """

    def __init__(
        self,
        *,
        llm_driver: _MinimalLLMDriver | None = None,
        background_handlers: dict[str, _BackgroundHandler] | None = None,
        emit_outcomes: bool = True,
    ) -> None:
        self._llm_driver = llm_driver
        self._handlers: dict[str, _BackgroundHandler] = dict(
            background_handlers or {}
        )
        self._emit_outcomes = emit_outcomes

    # ── Public API ───────────────────────────────────────────────

    @property
    def llm_driver(self) -> _MinimalLLMDriver | None:
        """The injected driver, or ``None`` if running heuristic-only."""
        return self._llm_driver

    def register_background_handler(
        self, task_type: str, handler: _BackgroundHandler
    ) -> None:
        """Register a callable that consumes one :class:`BackgroundTask`.

        Handlers may be sync or async. Unknown task types are skipped
        with a stderr log when :meth:`execute_background` runs.
        """
        if not task_type or not isinstance(task_type, str):
            raise ValueError("task_type must be a non-empty string")
        if not callable(handler):
            raise TypeError("handler must be callable")
        self._handlers[task_type] = handler

    def classify(self, user_message: str) -> MessageSignals:
        """Compute :class:`MessageSignals` from raw text. 0-cost regex only."""
        text = user_message or ""
        stripped = text.strip()
        char_length = len(stripped)
        is_trivial = char_length <= _TRIVIAL_MAX_CHARS or any(
            p.match(stripped) for p in _TRIVIAL_PATTERNS
        )
        return MessageSignals(
            is_emotional=any(p.search(text) for p in _EMOTIONAL_PATTERNS),
            needs_tool=any(p.search(text) for p in _TOOL_PATTERNS),
            is_command=bool(_COMMAND_PATTERN.match(text)),
            is_trivial=is_trivial,
            is_question=any(p.search(text) for p in _QUESTION_PATTERNS),
            has_memory_trigger=any(
                p.search(text) for p in _MEMORY_TRIGGER_PATTERNS
            ),
            char_length=char_length,
        )

    def route(self, route_input: IntentRouteInput) -> IntentRouteOutput:
        """Run the full router for one turn.

        Priority order (first match wins):

            1. ``force_layer`` set — caller-pinned override
            2. tool / command intent → ``HYBRID`` (tool runs background,
               foreground composes reply around it)
            3. emotional intent → ``FOREGROUND`` (foreground LLM owns
               the empathic reply path; affect_update queued background)
            4. memory recall trigger → ``HYBRID`` (recall background +
               foreground reply uses the recalled brief)
            5. trivial → ``FOREGROUND`` (cheap fast-path, no background)
            6. default → ``HYBRID``
        """
        signals = self.classify(route_input.user_message)

        if route_input.force_layer is not None:
            decision = DispatchDecision(
                layer=route_input.force_layer,
                reason="force_layer",
                signals=signals,
            )
        elif signals.needs_tool or signals.is_command:
            decision = DispatchDecision(
                layer=ProcessingLayer.HYBRID,
                reason="tool_or_command",
                signals=signals,
            )
        elif signals.is_emotional:
            decision = DispatchDecision(
                layer=ProcessingLayer.FOREGROUND,
                reason="emotional",
                signals=signals,
            )
        elif signals.has_memory_trigger:
            decision = DispatchDecision(
                layer=ProcessingLayer.HYBRID,
                reason="memory_trigger",
                signals=signals,
            )
        elif signals.is_trivial:
            decision = DispatchDecision(
                layer=ProcessingLayer.FOREGROUND,
                reason="trivial",
                signals=signals,
            )
        else:
            decision = DispatchDecision(
                layer=ProcessingLayer.HYBRID,
                reason="default",
                signals=signals,
            )

        background_tasks = self._derive_background_tasks(
            decision, route_input
        )
        context_brief = self.build_conscious_context(
            route_input.conversation_history, signals
        )
        foreground_task = self._derive_foreground_label(decision, signals)

        self._emit_outcome(decision, route_input.persona_id)

        return IntentRouteOutput(
            decision=decision,
            foreground_task=foreground_task,
            background_tasks=background_tasks,
            context_brief=context_brief,
        )

    async def execute_background(
        self, tasks: Sequence[BackgroundTask]
    ) -> dict[str, Any]:
        """Run all ``tasks`` in parallel via ``asyncio.gather``.

        Returns a dict keyed by ``task.type`` (last-wins on duplicate
        type — callers should pre-deduplicate or use distinct types).
        Tasks whose type has no registered handler are skipped with a
        ``None`` value in the result so consumers can audit coverage.
        Handler exceptions are caught — one bad handler must not abort
        the others.
        """
        if not tasks:
            return {}

        async def _run_one(task: BackgroundTask) -> tuple[str, Any]:
            handler = self._handlers.get(task.type)
            if handler is None:
                return task.type, None
            try:
                result = handler(task)
                if asyncio.iscoroutine(result) or isinstance(
                    result, Awaitable
                ):
                    awaited = await result
                    return task.type, awaited
                return task.type, result
            except Exception as exc:  # pragma: no cover - defensive
                return task.type, {"error": repr(exc)}

        sorted_tasks = sorted(tasks, key=lambda t: -t.priority)
        outcomes = await asyncio.gather(*(_run_one(t) for t in sorted_tasks))
        merged: dict[str, Any] = {}
        for key, value in outcomes:
            merged[key] = value
        return merged

    def build_conscious_context(
        self,
        conversation_history: Sequence[dict[str, str]],
        signals: MessageSignals,
        *,
        max_chars: int = _CONTEXT_BRIEF_MAX_CHARS,
    ) -> str:
        """Assemble a short context brief for the foreground LLM call.

        Strategy (deterministic, no LLM):

            * Take the last user / assistant pair (most relevant).
            * Prepend a one-line signal summary so the foreground prompt
              can adapt tone (e.g. detected emotional cue → empathy).
            * Truncate to ``max_chars`` from the head of the history
              block, keeping the signal line intact.

        Returns ``""`` when history is empty and signals are unremarkable.
        """
        signal_line = self._format_signal_line(signals)
        history_block = self._render_recent_history(conversation_history)
        if not history_block and not signal_line:
            return ""

        parts: list[str] = []
        if signal_line:
            parts.append(signal_line)
        if history_block:
            parts.append(history_block)
        brief = "\n".join(parts)
        if len(brief) <= max_chars:
            return brief

        # Preserve the signal line; truncate the history tail.
        if signal_line:
            head = signal_line + "\n"
            remaining = max(0, max_chars - len(head))
            return head + history_block[:remaining]
        return brief[:max_chars]

    # ── Internals ────────────────────────────────────────────────

    def _derive_background_tasks(
        self,
        decision: DispatchDecision,
        route_input: IntentRouteInput,
    ) -> tuple[BackgroundTask, ...]:
        """Map a decision + input to a concrete background task tuple."""
        if decision.layer is ProcessingLayer.FOREGROUND:
            # Even on FOREGROUND we may want a deferred affect_update
            # when the message had emotional cues. Keep it cheap.
            if decision.signals.is_emotional:
                return (
                    BackgroundTask(
                        type="affect_update",
                        payload={"signals": decision.signals.__dict__},
                        priority=10,
                    ),
                )
            return ()

        tasks: list[BackgroundTask] = []
        if decision.signals.has_memory_trigger:
            tasks.append(
                BackgroundTask(
                    type="recall",
                    payload={
                        "query": route_input.user_message,
                        "persona_id": route_input.persona_id,
                    },
                    priority=20,
                )
            )
        if route_input.turn_index >= 1:
            # Only consolidate after at least one full prior turn.
            tasks.append(
                BackgroundTask(
                    type="memory_consolidation",
                    payload={"turn_index": route_input.turn_index},
                    priority=5,
                )
            )
        if decision.signals.is_emotional:
            tasks.append(
                BackgroundTask(
                    type="affect_update",
                    payload={"signals": decision.signals.__dict__},
                    priority=10,
                )
            )
        return tuple(tasks)

    def _derive_foreground_label(
        self, decision: DispatchDecision, signals: MessageSignals
    ) -> str:
        """Stable string label for the foreground call.

        Consumers turn this into a system-prompt or call shape. We do
        not produce the prompt here — that belongs to ``persona.prompt``.
        """
        if decision.reason == "tool_or_command":
            return "reply_with_tool"
        if signals.is_emotional:
            return "reply_empathic"
        if signals.is_question:
            return "reply_answer"
        if decision.reason == "trivial":
            return "reply_brief"
        return "reply_default"

    def _emit_outcome(self, decision: DispatchDecision, persona_id: str) -> None:
        """Best-effort emit to the ZIQ bus. Never raises."""
        if not self._emit_outcomes:
            return
        try:
            from concinno.ziq_outcome_bus import Outcome, get_bus
        except Exception:
            return  # pragma: no cover - bus optional
        try:
            bus = get_bus()
            # Layer enum ordinal: FOREGROUND=0 / BACKGROUND=1 / HYBRID=2
            layer_ordinal = {
                ProcessingLayer.FOREGROUND: 0,
                ProcessingLayer.BACKGROUND: 1,
                ProcessingLayer.HYBRID: 2,
            }[decision.layer]
            bus.emit(
                Outcome(
                    tunable="persona.intent_router.dispatch_layer",
                    value=layer_ordinal,
                    reward=1.0,
                    source=f"IntentRouter[{persona_id}]",
                    metadata={"reason": decision.reason},
                )
            )
        except Exception:  # pragma: no cover - defensive
            return

    @staticmethod
    def _format_signal_line(signals: MessageSignals) -> str:
        """Compact one-line signal summary (only flags that fired)."""
        flags: list[str] = []
        if signals.is_emotional:
            flags.append("emotional")
        if signals.needs_tool:
            flags.append("tool")
        if signals.is_command:
            flags.append("command")
        if signals.is_question:
            flags.append("question")
        if signals.has_memory_trigger:
            flags.append("memory")
        if signals.is_trivial:
            flags.append("trivial")
        if not flags:
            return ""
        return "[signals: " + ",".join(flags) + "]"

    @staticmethod
    def _render_recent_history(
        conversation_history: Sequence[dict[str, str]],
    ) -> str:
        """Render the last user / assistant pair as plain text."""
        if not conversation_history:
            return ""
        # Walk from the end picking up the latest assistant + user lines.
        lines: list[str] = []
        for entry in list(conversation_history)[-4:]:
            role = entry.get("role", "?")
            content = entry.get("content", "")
            if not isinstance(content, str):
                # Anthropic-shape blocks: best-effort flatten.
                try:
                    content = " ".join(
                        block.get("text", "")
                        for block in content
                        if isinstance(block, dict)
                    )
                except Exception:
                    content = str(content)
            lines.append(f"{role}: {content}")
        return "\n".join(lines)
