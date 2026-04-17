"""concinno.tool_executor — Goal-driven multi-step tool execution loop.

@module tool_executor
@responsibility Orchestrate an observe -> think (LLM) -> act (tool call) ->
    observe loop over a caller-supplied list of :class:`Tool` protocol
    implementations. Stateful, resumable via
    :class:`concinno.core.state_store.StateStore`. Each tool gets
    retry + circuit breaker wrapping.
@dependencies concinno.core.state_store (hard),
    concinno.escalation.LLMEscalator (lazy, optional)
@exports Tool, ToolStep, ExecutionState, ToolNotFound, CircuitOpen,
    MaxStepsExceeded, ToolExecutor

This is dispatch plumbing, not a reasoning guarantee. The LLM JSON parser
is best-effort fail-soft; tools must be idempotent across retries since
state restoration may replay completed steps. The executor does not verify
that the LLM's chain-of-thought is sound, only that the transport between
"LLM said do X" and "X ran" is honest: every step is recorded, every
failure is surfaced, every resume starts from the last persisted step.
Callers who want cognitive guarantees should compose this with
``ThinkingDepthGuard``, ``ConfidenceGate``, or the CBUA router -- those
are the primitives that judge *what* to do; this module only executes
*how*.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol, Sequence, runtime_checkable

from .core.state_store import StateStore

logger = logging.getLogger("concinno.tool_executor")

_NAMESPACE = "tool_executor"
_DEFAULT_SYSTEM_PROMPT_HEAD = (
    "You are a tool-using agent. Respond with JSON in one of these schemas:"
)
_TRANSIENT_KEYWORDS = ("timeout", "timed out", "connection", "temporarily", "unavailable")


@runtime_checkable
class Tool(Protocol):
    """Minimal tool contract. Implementations supply a name, a short
    description, and a ``call`` method that accepts keyword arguments.

    Optional attribute ``is_concurrency_safe`` (bool) flags the tool as
    safe to run in parallel with other concurrency-safe tools. When
    missing it is treated as ``False`` (serial). Mirrors CC's
    ``services/tools/toolOrchestration.ts`` partitioning rule: read-only
    tools get batched, mutating tools stay serial.
    """

    name: str
    description: str
    # Default is False; implementations may override to True.
    is_concurrency_safe: bool  # type: ignore[misc]

    def call(self, **kwargs: Any) -> Any:
        """Execute the tool. May raise for permanent or transient errors."""
        ...


@dataclass(frozen=True)
class ToolCall:
    """One pending tool invocation, tagged by name + kwargs.

    Used by :func:`partition_tool_calls` and :meth:`ToolExecutor.run_batched`
    to mirror CC's batched tool dispatch. ``call_id`` is an opaque trace
    handle the caller may set; the executor never reads it.
    """

    tool_name: str
    kwargs: dict[str, Any]
    call_id: str = ""


@dataclass
class ToolStep:
    """One iteration of the observe/think/act loop."""

    index: int
    thought: str
    tool_name: str
    args: dict[str, Any]
    result: Any | None
    error: str | None
    retries: int
    elapsed_ms: int


@dataclass
class ExecutionState:
    """Serializable state of a single goal-driven run."""

    goal: str
    steps: list[ToolStep] = field(default_factory=list)
    completed: bool = False
    failed: bool = False
    reason: str = ""
    answer: str = ""


class ToolNotFound(KeyError):
    """Raised (or recorded) when the LLM requests an unknown tool."""


class CircuitOpen(RuntimeError):
    """Raised when a tool fails past ``circuit_threshold`` times in a run."""


class MaxStepsExceeded(RuntimeError):
    """Reserved for callers that prefer an exception over ``reason`` checks."""


def _classify_transient(exc: BaseException) -> bool:
    msg = f"{type(exc).__name__}: {exc}".lower()
    return any(k in msg for k in _TRANSIENT_KEYWORDS)


def _now_ms() -> int:
    return int(time.monotonic() * 1000)


def _is_concurrency_safe(tool: Tool | None) -> bool:
    """Return True only if the tool explicitly opts in. Missing → False.

    Mirrors CC's conservative default: anything we don't know about is
    treated as a write and stays serial.
    """
    if tool is None:
        return False
    return bool(getattr(tool, "is_concurrency_safe", False))


def partition_tool_calls(
    calls: Sequence[ToolCall],
    tools: dict[str, Tool],
) -> list[list[ToolCall]]:
    """Partition consecutive concurrency-safe tool calls into parallel batches.

    Mirrors CC's ``services/tools/toolOrchestration.ts:91-116``. Consecutive
    calls whose tool has ``is_concurrency_safe=True`` collapse into one
    batch; non-safe calls are their own singleton batch. Order preserved.

    Calls referencing an unknown tool are treated as non-safe (singleton).

    Example::

        [Read, Read, Read, Write, Read, Grep] →
        [[Read, Read, Read], [Write], [Read, Grep]]
    """
    batches: list[list[ToolCall]] = []
    last_safe: bool | None = None
    for call in calls:
        tool = tools.get(call.tool_name)
        safe = _is_concurrency_safe(tool)
        if safe and last_safe is True:
            batches[-1].append(call)
        else:
            batches.append([call])
        last_safe = safe
    return batches


def _default_max_concurrency() -> int:
    raw = os.environ.get("CONCINNO_MAX_TOOL_USE_CONCURRENCY", "10")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 10
    return n if n > 0 else 10


async def _async_raise(exc: BaseException) -> Any:
    """Awaitable that raises ``exc``. Used to slot errors into gather()."""
    raise exc


async def _invoke_tool_async(tool: Tool, kwargs: dict[str, Any]) -> Any:
    """Call a tool, awaiting if it returns a coroutine.

    Sync tools run via ``asyncio.to_thread`` so they don't block the loop.
    Async tools (``async def call``) are awaited directly.
    """
    call_fn = tool.call
    if inspect.iscoroutinefunction(call_fn):
        return await call_fn(**kwargs)
    # Sync: offload to thread pool.
    result = await asyncio.to_thread(call_fn, **kwargs)
    if inspect.isawaitable(result):
        # Tool returned a coroutine from a sync wrapper; honour it.
        return await result
    return result


class ToolExecutor:
    """Run a goal to completion by iterating observe -> think -> act."""

    def __init__(
        self,
        tools: Sequence[Tool],
        *,
        escalator: Any = None,
        cache_dir: str | None = None,
        max_steps: int = 10,
        max_retries_per_tool: int = 2,
        circuit_threshold: int = 3,
        system_prompt: str | None = None,
    ) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            if tool.name in self._tools:
                msg = f"duplicate tool name: {tool.name!r}"
                raise ValueError(msg)
            self._tools[tool.name] = tool

        self._escalator = escalator
        self.max_steps = int(max_steps)
        self.max_retries_per_tool = int(max_retries_per_tool)
        self.circuit_threshold = int(circuit_threshold)
        self.system_prompt = system_prompt or self._default_system_prompt()

        base_dir = cache_dir or ".concinno_cache"
        self._store = StateStore(base_dir)

        self._fail_count: dict[str, int] = {}
        self._unparseable_streak = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        goal: str,
        *,
        session_id: str | None = None,
        resume: bool = False,
    ) -> ExecutionState:
        """Execute the loop until the goal is reached or a stop reason hits."""
        sid = session_id or "_default"

        if resume:
            loaded = self.load(sid)
            state = loaded if loaded is not None else ExecutionState(goal=goal, reason="running")
            if loaded is None:
                state.reason = "running"
        else:
            state = ExecutionState(goal=goal, reason="running")

        self._fail_count = {}
        self._unparseable_streak = 0

        step_idx = len(state.steps)
        if state.steps and state.steps[-1].result is not None:
            observation: Any = state.steps[-1].result
        elif state.steps and state.steps[-1].error is not None:
            observation = state.steps[-1].error
        else:
            observation = "initial"

        while step_idx < self.max_steps:
            reply = self._think(goal, state.steps, observation)
            parsed = self._parse_think(reply)

            if parsed.get("terminal"):
                state.completed = True
                state.reason = "goal_reached"
                state.answer = str(parsed.get("answer", ""))
                self.save(sid, state)
                return state

            if parsed.get("_unparseable"):
                self._unparseable_streak += 1
                if self._unparseable_streak >= 2:
                    state.failed = True
                    state.reason = "exhausted"
                    self.save(sid, state)
                    return state
            else:
                self._unparseable_streak = 0

            thought = str(parsed.get("thought", ""))
            action = str(parsed.get("action", ""))
            args = parsed.get("args", {}) or {}
            if not isinstance(args, dict):
                args = {}

            tool = self._lookup(action)
            if tool is None:
                available = list(self._tools.keys())
                error_msg = f"tool not found: {action!r}. available: {available}"
                step = ToolStep(
                    index=step_idx,
                    thought=thought,
                    tool_name=action,
                    args=args,
                    result=None,
                    error=error_msg,
                    retries=0,
                    elapsed_ms=0,
                )
                state.steps.append(step)
                observation = error_msg
                self.save(sid, state)
                step_idx += 1
                continue

            result, error, retries, elapsed = self._call_with_retry(tool, args)

            if error is not None:
                self._fail_count[tool.name] = self._fail_count.get(tool.name, 0) + 1
                if self._fail_count[tool.name] >= self.circuit_threshold:
                    step = ToolStep(
                        index=step_idx,
                        thought=thought,
                        tool_name=tool.name,
                        args=args,
                        result=None,
                        error=error,
                        retries=retries,
                        elapsed_ms=elapsed,
                    )
                    state.steps.append(step)
                    state.failed = True
                    state.reason = "circuit_open"
                    self.save(sid, state)
                    raise CircuitOpen(tool.name)
            else:
                self._fail_count[tool.name] = 0

            step = ToolStep(
                index=step_idx,
                thought=thought,
                tool_name=tool.name,
                args=args,
                result=result,
                error=error,
                retries=retries,
                elapsed_ms=elapsed,
            )
            state.steps.append(step)
            observation = result if result is not None else error
            self.save(sid, state)
            step_idx += 1

        if not state.completed:
            state.reason = "max_steps"
            state.failed = True
        self.save(sid, state)
        return state

    async def run_batched(
        self,
        calls: Sequence[ToolCall],
        tools: dict[str, Tool] | None = None,
        max_concurrency: int | None = None,
    ) -> list[Any]:
        """Execute ``calls`` with CC-style concurrency partitioning.

        Consecutive ``is_concurrency_safe=True`` calls run in parallel via
        :func:`asyncio.gather`; non-safe calls run serially. Sync tools
        offload to a thread via :func:`asyncio.to_thread` so the event loop
        stays free.

        ``max_concurrency`` defaults to
        ``int(os.environ["CONCINNO_MAX_TOOL_USE_CONCURRENCY"])`` (or 10).
        It caps how many parallel tasks run inside a single safe batch;
        oversized batches are sliced into ``ceil(n / max_concurrency)``
        chunks executed back-to-back.

        Errors do not abort the batch: each failing call's slot in the
        returned list holds the exception instance (gather
        ``return_exceptions=True``). Non-batch serial calls behave the
        same way for symmetry.
        """
        registry = tools if tools is not None else self._tools
        cap = max_concurrency if max_concurrency is not None else _default_max_concurrency()
        if cap <= 0:
            cap = 1

        results: list[Any] = []
        batches = partition_tool_calls(calls, registry)

        for batch in batches:
            first_tool = registry.get(batch[0].tool_name)
            safe = _is_concurrency_safe(first_tool) and len(batch) > 1

            if not safe:
                # Singleton or non-safe: serial execution, one at a time.
                for call in batch:
                    tool = registry.get(call.tool_name)
                    if tool is None:
                        results.append(ToolNotFound(call.tool_name))
                        continue
                    try:
                        results.append(await _invoke_tool_async(tool, call.kwargs))
                    except Exception as exc:  # noqa: BLE001 — surface, don't swallow
                        results.append(exc)
                continue

            # Concurrency-safe batch: chunk by max_concurrency, gather each.
            for start in range(0, len(batch), cap):
                chunk = batch[start : start + cap]
                coros = []
                for call in chunk:
                    tool = registry.get(call.tool_name)
                    if tool is None:
                        coros.append(_async_raise(ToolNotFound(call.tool_name)))
                    else:
                        coros.append(_invoke_tool_async(tool, call.kwargs))
                chunk_results = await asyncio.gather(*coros, return_exceptions=True)
                results.extend(chunk_results)

        return results

    def run_batched_sync(
        self,
        calls: Sequence[ToolCall],
        tools: dict[str, Tool] | None = None,
        max_concurrency: int | None = None,
    ) -> list[Any]:
        """Synchronous wrapper around :meth:`run_batched` via ``asyncio.run``.

        Safe to call from non-async code. Do not call from inside a
        running event loop — use :meth:`run_batched` directly there.
        """
        return asyncio.run(self.run_batched(calls, tools, max_concurrency))

    def resume(self, session_id: str) -> ExecutionState:
        """Continue an interrupted run from the last persisted step."""
        state = self.load(session_id)
        if state is None:
            return ExecutionState(goal="", reason="running")
        return self.run(state.goal, session_id=session_id, resume=True)

    def save(self, session_id: str, state: ExecutionState) -> None:
        """Persist state for ``session_id``."""
        data = {
            "goal": state.goal,
            "completed": state.completed,
            "failed": state.failed,
            "reason": state.reason,
            "answer": state.answer,
            "steps": [asdict(s) for s in state.steps],
        }
        self._store.write(_NAMESPACE, session_id, data)

    def load(self, session_id: str) -> ExecutionState | None:
        """Return a previously saved state or ``None`` if none exists."""
        raw = self._store.read(_NAMESPACE, session_id, default=None)
        if not raw or not isinstance(raw, dict) or "goal" not in raw:
            return None
        steps_raw = raw.get("steps", [])
        steps: list[ToolStep] = []
        if isinstance(steps_raw, list):
            for s in steps_raw:
                if not isinstance(s, dict):
                    continue
                steps.append(
                    ToolStep(
                        index=int(s.get("index", 0)),
                        thought=str(s.get("thought", "")),
                        tool_name=str(s.get("tool_name", "")),
                        args=dict(s.get("args", {}) or {}),
                        result=s.get("result"),
                        error=s.get("error"),
                        retries=int(s.get("retries", 0)),
                        elapsed_ms=int(s.get("elapsed_ms", 0)),
                    )
                )
        return ExecutionState(
            goal=str(raw.get("goal", "")),
            steps=steps,
            completed=bool(raw.get("completed", False)),
            failed=bool(raw.get("failed", False)),
            reason=str(raw.get("reason", "")),
            answer=str(raw.get("answer", "")),
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _default_system_prompt(self) -> str:
        lines = [
            _DEFAULT_SYSTEM_PROMPT_HEAD,
            '  {"thought": "...", "action": "tool_name", "args": {...}}',
            '  {"thought": "...", "done": true, "answer": "..."}',
            "",
            "Available tools:",
        ]
        if not self._tools:
            lines.append("  (none — you may only respond with the terminal schema)")
        else:
            for name, tool in self._tools.items():
                lines.append(f"  - {name}: {tool.description}")
        return "\n".join(lines)

    def _lookup(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def _think(self, goal: str, steps: list[ToolStep], observation: Any) -> str:
        """Call the LLM escalator for the next JSON instruction."""
        escalator = self._escalator
        if escalator is None:
            try:
                from .escalation import LLMEscalator  # noqa: F401  (lazy import)
            except ImportError as exc:  # pragma: no cover - defensive
                msg = (
                    "tool_executor requires an escalator: either pass "
                    "escalator=... or install concinno.escalation"
                )
                raise RuntimeError(msg) from exc
            msg = (
                "tool_executor requires an escalator to be supplied at "
                "construction time; default LLMEscalator wiring is the "
                "caller's responsibility"
            )
            raise RuntimeError(msg)

        hint = ""
        if self._unparseable_streak >= 1:
            hint = (
                " NOTE: your last reply was unparseable. Return strict JSON "
                "in one of the documented schemas, nothing else."
            )

        user_parts = [
            f"GOAL: {goal}",
            f"OBSERVATION: {observation}",
            f"STEPS_TAKEN: {len(steps)}",
        ]
        if steps:
            last = steps[-1]
            user_parts.append(
                f"LAST_STEP: {last.tool_name}({last.args}) -> "
                f"{'error=' + str(last.error) if last.error else 'ok'}"
            )
        if hint:
            user_parts.append(hint)

        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": "\n".join(user_parts)},
        ]

        result = escalator.escalate(messages)
        text_attr = getattr(result, "final", None)
        if text_attr is not None and hasattr(text_attr, "text"):
            return str(text_attr.text)
        if isinstance(result, str):
            return result
        return str(result)

    def _parse_think(self, reply: str) -> dict[str, Any]:
        """Best-effort parse of the LLM reply. Fail-soft by design."""
        parsed = self._try_json(reply)
        if parsed is None:
            match = re.search(r"\{.*\}", reply, re.DOTALL)
            if match is not None:
                parsed = self._try_json(match.group(0))

        if parsed is None or not isinstance(parsed, dict):
            preview = reply[:100]
            return {
                "thought": f"<unparseable: {preview}>",
                "action": "_noop",
                "args": {},
                "terminal": False,
                "_unparseable": True,
            }

        if parsed.get("done") is True:
            return {
                "thought": str(parsed.get("thought", "")),
                "terminal": True,
                "answer": parsed.get("answer", ""),
            }

        return {
            "thought": str(parsed.get("thought", "")),
            "action": str(parsed.get("action", "")),
            "args": parsed.get("args", {}) or {},
            "terminal": False,
        }

    @staticmethod
    def _try_json(text: str) -> Any:
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            return None

    def _call_with_retry(
        self,
        tool: Tool,
        args: dict[str, Any],
    ) -> tuple[Any, str | None, int, int]:
        retries = 0
        start = _now_ms()
        last_error: str | None = None
        while True:
            try:
                result = tool.call(**args)
                elapsed = _now_ms() - start
                return result, None, retries, elapsed
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if not _classify_transient(exc):
                    elapsed = _now_ms() - start
                    return None, last_error, retries, elapsed
                if retries >= self.max_retries_per_tool:
                    elapsed = _now_ms() - start
                    return None, last_error, retries, elapsed
                retries += 1
