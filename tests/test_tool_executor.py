"""Tests for cc_cortex.tool_executor."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from cc_cortex.tool_executor import (
    CircuitOpen,
    ExecutionState,
    Tool,
    ToolCall,
    ToolExecutor,
    ToolNotFound,
    ToolStep,
    partition_tool_calls,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeTierResult:
    text: str


@dataclass
class _FakeEscalationResult:
    final: _FakeTierResult


class FakeEscalator:
    """Returns canned JSON replies in order. Records all messages seen."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict[str, str]]] = []

    def escalate(self, messages: list[dict[str, str]], **_: Any) -> _FakeEscalationResult:
        self.calls.append(messages)
        if not self.replies:
            text = json.dumps({"thought": "no more replies", "done": True, "answer": ""})
        else:
            text = self.replies.pop(0)
        return _FakeEscalationResult(final=_FakeTierResult(text=text))


class EchoTool:
    name = "echo"
    description = "Return whatever you pass in as 'msg'."

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def call(self, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        return kwargs.get("msg", "")


class AddTool:
    name = "add"
    description = "Add a and b."

    def call(self, **kwargs: Any) -> Any:
        return int(kwargs.get("a", 0)) + int(kwargs.get("b", 0))


class FlakyTool:
    """First N calls raise TimeoutError (transient), then succeed."""

    name = "flaky"
    description = "Fails a few times then works."

    def __init__(self, fail_count: int = 1) -> None:
        self.fail_count = fail_count
        self.total_calls = 0

    def call(self, **_: Any) -> Any:
        self.total_calls += 1
        if self.total_calls <= self.fail_count:
            raise TimeoutError("connection timeout")
        return "ok"


class PermanentFailTool:
    name = "bad"
    description = "Always raises ValueError (permanent)."

    def __init__(self) -> None:
        self.calls = 0

    def call(self, **_: Any) -> Any:
        self.calls += 1
        raise ValueError("cannot process")


class ExplodingTool:
    name = "explode"
    description = "Raises a regular exception."

    def call(self, **_: Any) -> Any:
        raise RuntimeError("kaboom")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reply_action(action: str, **args: Any) -> str:
    return json.dumps({"thought": f"calling {action}", "action": action, "args": args})


def _reply_done(answer: str = "done") -> str:
    return json.dumps({"thought": "finished", "done": True, "answer": answer})


def _make_executor(
    tmp_path: Any,
    tools: list[Tool],
    replies: list[str],
    **kwargs: Any,
) -> tuple[ToolExecutor, FakeEscalator]:
    esc = FakeEscalator(replies)
    ex = ToolExecutor(tools, escalator=esc, cache_dir=str(tmp_path), **kwargs)
    return ex, esc


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_single_step_goal_reached(tmp_path: Any) -> None:
    ex, _ = _make_executor(tmp_path, [EchoTool()], [_reply_done("hello")])
    state = ex.run("say hello", session_id="s1")
    assert state.completed is True
    assert state.failed is False
    assert state.reason == "goal_reached"
    assert state.answer == "hello"


def test_multi_step_with_intermediate_results(tmp_path: Any) -> None:
    ex, _ = _make_executor(
        tmp_path,
        [AddTool()],
        [
            _reply_action("add", a=1, b=2),
            _reply_action("add", a=3, b=4),
            _reply_done("summed"),
        ],
    )
    state = ex.run("sum things", session_id="multi")
    assert len(state.steps) == 2
    assert state.steps[0].result == 3
    assert state.steps[1].result == 7
    assert state.completed is True


def test_max_steps_exceeded_returns_failed_state(tmp_path: Any) -> None:
    ex, _ = _make_executor(
        tmp_path,
        [EchoTool()],
        [_reply_action("echo", msg=f"x{i}") for i in range(20)],
        max_steps=3,
    )
    state = ex.run("loop forever", session_id="loop")
    assert state.completed is False
    assert state.failed is True
    assert state.reason == "max_steps"
    assert len(state.steps) == 3


def test_unknown_tool_recorded_and_fed_back(tmp_path: Any) -> None:
    ex, _ = _make_executor(
        tmp_path,
        [EchoTool()],
        [_reply_action("does_not_exist"), _reply_done()],
    )
    state = ex.run("bad tool", session_id="bad")
    assert len(state.steps) == 1
    assert state.steps[0].error is not None
    assert "tool not found" in state.steps[0].error
    assert state.completed is True


def test_tool_transient_error_retried(tmp_path: Any) -> None:
    flaky = FlakyTool(fail_count=1)
    ex, _ = _make_executor(
        tmp_path,
        [flaky],
        [_reply_action("flaky"), _reply_done()],
        max_retries_per_tool=2,
    )
    state = ex.run("retry", session_id="retry")
    assert state.completed is True
    assert state.steps[0].result == "ok"
    assert state.steps[0].retries == 1
    assert flaky.total_calls == 2


def test_tool_permanent_fail_circuit_increments(tmp_path: Any) -> None:
    bad = PermanentFailTool()
    ex, _ = _make_executor(
        tmp_path,
        [bad],
        [_reply_action("bad"), _reply_done()],
        circuit_threshold=5,
    )
    state = ex.run("fail", session_id="pf")
    assert state.steps[0].error is not None
    assert "ValueError" in state.steps[0].error
    assert bad.calls == 1  # permanent = no retry
    assert state.completed is True


def test_circuit_breaker_opens_at_threshold(tmp_path: Any) -> None:
    ex, _ = _make_executor(
        tmp_path,
        [PermanentFailTool()],
        [_reply_action("bad")] * 10,
        circuit_threshold=2,
    )
    with pytest.raises(CircuitOpen):
        ex.run("break circuit", session_id="cb")
    state = ex.load("cb")
    assert state is not None
    assert state.reason == "circuit_open"
    assert state.failed is True


def test_state_persisted_after_each_step(tmp_path: Any) -> None:
    ex, _ = _make_executor(
        tmp_path,
        [EchoTool()],
        [_reply_action("echo", msg="a"), _reply_action("echo", msg="b"), _reply_done()],
    )
    ex.run("persist", session_id="persist")
    loaded = ex.load("persist")
    assert loaded is not None
    assert len(loaded.steps) == 2
    assert loaded.completed is True


def test_resume_continues_from_saved_state(tmp_path: Any) -> None:
    ex1, _ = _make_executor(
        tmp_path,
        [EchoTool()],
        [_reply_action("echo", msg="first")],
        max_steps=1,
    )
    state1 = ex1.run("two step", session_id="resume")
    assert len(state1.steps) == 1

    ex2, _ = _make_executor(
        tmp_path,
        [EchoTool()],
        [_reply_action("echo", msg="second"), _reply_done()],
        max_steps=5,
    )
    state2 = ex2.run("two step", session_id="resume", resume=True)
    assert len(state2.steps) == 2
    assert state2.completed is True


def test_resume_nonexistent_session_returns_empty_state(tmp_path: Any) -> None:
    ex, _ = _make_executor(tmp_path, [], [_reply_done()])
    state = ex.resume("never_existed")
    assert state.goal == ""
    assert state.steps == []


def test_unparseable_llm_reply_recovers_with_hint(tmp_path: Any) -> None:
    ex, esc = _make_executor(
        tmp_path,
        [EchoTool()],
        ["this is not json at all", _reply_done("recovered")],
    )
    state = ex.run("recover", session_id="recover")
    assert state.completed is True
    assert state.answer == "recovered"
    # Second think call must include the hint.
    assert len(esc.calls) == 2
    second_user = esc.calls[1][-1]["content"]
    assert "unparseable" in second_user.lower()


def test_unparseable_twice_fails_with_exhausted(tmp_path: Any) -> None:
    ex, _ = _make_executor(
        tmp_path,
        [EchoTool()],
        ["not json", "still not json"],
    )
    state = ex.run("exhaust", session_id="ex")
    assert state.failed is True
    assert state.reason == "exhausted"


def test_tool_not_found_lists_available_in_error(tmp_path: Any) -> None:
    ex, _ = _make_executor(
        tmp_path,
        [EchoTool(), AddTool()],
        [_reply_action("ghost"), _reply_done()],
    )
    state = ex.run("list available", session_id="lst")
    err = state.steps[0].error or ""
    assert "echo" in err
    assert "add" in err


def test_custom_system_prompt_passed_to_escalator(tmp_path: Any) -> None:
    ex, esc = _make_executor(
        tmp_path,
        [EchoTool()],
        [_reply_done()],
        system_prompt="CUSTOM-PROMPT-XYZ",
    )
    ex.run("x", session_id="cp")
    assert esc.calls[0][0]["role"] == "system"
    assert esc.calls[0][0]["content"] == "CUSTOM-PROMPT-XYZ"


def test_default_system_prompt_mentions_tools(tmp_path: Any) -> None:
    ex, esc = _make_executor(
        tmp_path,
        [EchoTool(), AddTool()],
        [_reply_done()],
    )
    ex.run("x", session_id="dp")
    system = esc.calls[0][0]["content"]
    assert system.startswith("You are a tool-using agent. Respond with JSON")
    assert "echo" in system
    assert "add" in system


def test_result_passed_to_next_observation(tmp_path: Any) -> None:
    ex, esc = _make_executor(
        tmp_path,
        [EchoTool()],
        [_reply_action("echo", msg="observation-payload"), _reply_done()],
    )
    ex.run("obs", session_id="obs")
    second_user = esc.calls[1][-1]["content"]
    assert "observation-payload" in second_user


def test_terminal_done_returns_answer(tmp_path: Any) -> None:
    ex, _ = _make_executor(tmp_path, [], [_reply_done("final-answer")])
    state = ex.run("finish", session_id="fin")
    assert state.answer == "final-answer"
    assert state.completed is True


def test_escalator_called_once_per_think(tmp_path: Any) -> None:
    ex, esc = _make_executor(
        tmp_path,
        [EchoTool()],
        [_reply_action("echo", msg="a"), _reply_action("echo", msg="b"), _reply_done()],
    )
    ex.run("count", session_id="cnt")
    assert len(esc.calls) == 3


def test_empty_tool_registry_allowed(tmp_path: Any) -> None:
    ex, _ = _make_executor(tmp_path, [], [_reply_done("empty-ok")])
    state = ex.run("no tools", session_id="empty")
    assert state.completed is True
    assert state.answer == "empty-ok"


def test_duplicate_tool_name_in_init_raises_valueerror(tmp_path: Any) -> None:
    with pytest.raises(ValueError, match="duplicate tool name"):
        ToolExecutor([EchoTool(), EchoTool()], escalator=FakeEscalator([]), cache_dir=str(tmp_path))


def test_elapsed_ms_recorded_per_step(tmp_path: Any) -> None:
    ex, _ = _make_executor(
        tmp_path,
        [EchoTool()],
        [_reply_action("echo", msg="x"), _reply_done()],
    )
    state = ex.run("time", session_id="t")
    assert state.steps[0].elapsed_ms >= 0


def test_thought_recorded_in_step(tmp_path: Any) -> None:
    reply = json.dumps({"thought": "my-thought-here", "action": "echo", "args": {"msg": "m"}})
    ex, _ = _make_executor(tmp_path, [EchoTool()], [reply, _reply_done()])
    state = ex.run("think", session_id="th")
    assert state.steps[0].thought == "my-thought-here"


def test_args_passed_to_tool_call(tmp_path: Any) -> None:
    tool = EchoTool()
    ex, _ = _make_executor(
        tmp_path,
        [tool],
        [_reply_action("echo", msg="payload", extra=1), _reply_done()],
    )
    ex.run("args", session_id="args")
    assert tool.calls == [{"msg": "payload", "extra": 1}]


def test_tool_exception_does_not_crash_run(tmp_path: Any) -> None:
    ex, _ = _make_executor(
        tmp_path,
        [ExplodingTool()],
        [_reply_action("explode"), _reply_done()],
        circuit_threshold=99,
    )
    state = ex.run("survive", session_id="survive")
    assert state.completed is True
    assert state.steps[0].error is not None
    assert "RuntimeError" in state.steps[0].error


def test_load_before_run_returns_none_for_unknown(tmp_path: Any) -> None:
    ex, _ = _make_executor(tmp_path, [], [_reply_done()])
    assert ex.load("never-saved") is None


def test_json_embedded_in_text_extracted_via_regex(tmp_path: Any) -> None:
    embedded = (
        "Sure, here is my plan: "
        + json.dumps({"thought": "via-regex", "action": "echo", "args": {"msg": "x"}})
        + " -- end."
    )
    ex, _ = _make_executor(tmp_path, [EchoTool()], [embedded, _reply_done()])
    state = ex.run("regex", session_id="re")
    assert state.steps[0].thought == "via-regex"
    assert state.steps[0].result == "x"


def test_goal_reached_short_circuits_loop(tmp_path: Any) -> None:
    ex, esc = _make_executor(
        tmp_path,
        [EchoTool()],
        [_reply_done("immediate"), _reply_action("echo", msg="never")],
        max_steps=10,
    )
    state = ex.run("fast", session_id="fast")
    assert state.completed is True
    assert len(state.steps) == 0
    assert len(esc.calls) == 1  # loop broke after the first done reply


def test_save_then_load_roundtrip(tmp_path: Any) -> None:
    ex, _ = _make_executor(tmp_path, [], [])
    state = ExecutionState(
        goal="roundtrip",
        steps=[
            ToolStep(
                index=0,
                thought="t",
                tool_name="echo",
                args={"msg": "hello"},
                result="hello",
                error=None,
                retries=0,
                elapsed_ms=12,
            ),
        ],
        completed=True,
        reason="goal_reached",
        answer="done",
    )
    ex.save("rt", state)
    loaded = ex.load("rt")
    assert loaded is not None
    assert loaded.goal == "roundtrip"
    assert loaded.completed is True
    assert loaded.answer == "done"
    assert len(loaded.steps) == 1
    assert loaded.steps[0].args == {"msg": "hello"}
    assert loaded.steps[0].elapsed_ms == 12


# ---------------------------------------------------------------------------
# P0.4 — Concurrency partitioner & batched executor
# ---------------------------------------------------------------------------


import asyncio  # noqa: E402
import os  # noqa: E402
import time  # noqa: E402


class _ReadTool:
    """Concurrency-safe read-only tool. Sleeps to expose parallelism."""

    description = "read-only stub"
    is_concurrency_safe = True

    def __init__(self, name: str = "read", delay: float = 0.0, payload: Any = "ok") -> None:
        self.name = name
        self.delay = delay
        self.payload = payload
        self.calls = 0

    def call(self, **_: Any) -> Any:
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        return self.payload


class _WriteTool:
    """Mutating tool. Not concurrency-safe."""

    description = "mutating stub"
    is_concurrency_safe = False

    def __init__(self, name: str = "write", delay: float = 0.0) -> None:
        self.name = name
        self.delay = delay
        self.calls = 0

    def call(self, **_: Any) -> Any:
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        return f"wrote-{self.calls}"


class _LegacyTool:
    """Tool without the is_concurrency_safe attribute → defaults to unsafe."""

    name = "legacy"
    description = "no flag"

    def call(self, **_: Any) -> Any:
        return "legacy"


class _AsyncReadTool:
    """Native-async concurrency-safe tool."""

    description = "async read"
    is_concurrency_safe = True

    def __init__(self, name: str = "aread", delay: float = 0.0) -> None:
        self.name = name
        self.delay = delay
        self.calls = 0

    async def call(self, **_: Any) -> Any:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return f"async-{self.calls}"


class _BoomTool:
    name = "boom"
    description = "always raises"
    is_concurrency_safe = True

    def call(self, **_: Any) -> Any:
        raise RuntimeError("boom")


def _registry(*tools: Any) -> dict[str, Any]:
    return {t.name: t for t in tools}


# --- partition_tool_calls -------------------------------------------------


def test_partition_empty_returns_empty_list() -> None:
    assert partition_tool_calls([], {}) == []


def test_partition_single_safe_call() -> None:
    r = _ReadTool()
    batches = partition_tool_calls([ToolCall("read", {})], _registry(r))
    assert len(batches) == 1
    assert len(batches[0]) == 1


def test_partition_single_unsafe_call() -> None:
    w = _WriteTool()
    batches = partition_tool_calls([ToolCall("write", {})], _registry(w))
    assert len(batches) == 1
    assert len(batches[0]) == 1


def test_partition_all_safe_collapses_into_one_batch() -> None:
    r = _ReadTool()
    calls = [ToolCall("read", {"i": i}) for i in range(5)]
    batches = partition_tool_calls(calls, _registry(r))
    assert len(batches) == 1
    assert len(batches[0]) == 5


def test_partition_all_unsafe_each_singleton() -> None:
    w = _WriteTool()
    calls = [ToolCall("write", {"i": i}) for i in range(4)]
    batches = partition_tool_calls(calls, _registry(w))
    assert len(batches) == 4
    assert all(len(b) == 1 for b in batches)


def test_partition_mixed_safe_unsafe_safe() -> None:
    r = _ReadTool("read")
    w = _WriteTool("write")
    calls = [
        ToolCall("read", {}),
        ToolCall("read", {}),
        ToolCall("read", {}),
        ToolCall("write", {}),
        ToolCall("read", {}),
        ToolCall("read", {}),
    ]
    batches = partition_tool_calls(calls, _registry(r, w))
    # [Read,Read,Read] [Write] [Read,Read]
    assert [len(b) for b in batches] == [3, 1, 2]
    assert all(c.tool_name == "read" for c in batches[0])
    assert batches[1][0].tool_name == "write"


def test_partition_eleven_safe_still_one_batch_partition_caps_only_at_run() -> None:
    """max_concurrency caps execution width, not partition width."""
    r = _ReadTool()
    calls = [ToolCall("read", {}) for _ in range(11)]
    batches = partition_tool_calls(calls, _registry(r))
    assert len(batches) == 1
    assert len(batches[0]) == 11


def test_partition_legacy_tool_treated_as_unsafe() -> None:
    legacy = _LegacyTool()
    r = _ReadTool()
    calls = [
        ToolCall("read", {}),
        ToolCall("legacy", {}),
        ToolCall("read", {}),
    ]
    batches = partition_tool_calls(calls, _registry(r, legacy))
    assert [len(b) for b in batches] == [1, 1, 1]


def test_partition_unknown_tool_treated_as_unsafe() -> None:
    r = _ReadTool()
    calls = [
        ToolCall("read", {}),
        ToolCall("ghost", {}),
        ToolCall("read", {}),
    ]
    batches = partition_tool_calls(calls, _registry(r))
    assert [len(b) for b in batches] == [1, 1, 1]


# --- Tool Protocol attribute defaults -------------------------------------


def test_tool_concurrency_flag_true() -> None:
    r = _ReadTool()
    assert getattr(r, "is_concurrency_safe", False) is True


def test_tool_concurrency_flag_false() -> None:
    w = _WriteTool()
    assert getattr(w, "is_concurrency_safe", False) is False


def test_tool_concurrency_flag_missing_defaults_false() -> None:
    legacy = _LegacyTool()
    assert getattr(legacy, "is_concurrency_safe", False) is False


# --- run_batched ----------------------------------------------------------


def test_run_batched_three_safe_run_in_parallel(tmp_path: Any) -> None:
    delay = 0.15
    tools = [_ReadTool(f"r{i}", delay=delay) for i in range(3)]
    ex = ToolExecutor([], escalator=None, cache_dir=str(tmp_path))
    calls = [ToolCall(t.name, {}) for t in tools]
    registry = _registry(*tools)

    start = time.monotonic()
    results = ex.run_batched_sync(calls, registry)
    elapsed = time.monotonic() - start

    assert len(results) == 3
    assert all(t.calls == 1 for t in tools)
    # 3x serial would be ~0.45s; parallel should be well under 0.35s.
    assert elapsed < 0.35, f"expected parallel, got {elapsed:.2f}s"


def test_run_batched_write_blocks_subsequent_reads(tmp_path: Any) -> None:
    r1 = _ReadTool("r1", delay=0.05)
    w = _WriteTool("w", delay=0.05)
    r2 = _ReadTool("r2", delay=0.05)
    ex = ToolExecutor([], escalator=None, cache_dir=str(tmp_path))
    calls = [ToolCall("r1", {}), ToolCall("w", {}), ToolCall("r2", {})]

    start = time.monotonic()
    results = ex.run_batched_sync(calls, _registry(r1, w, r2))
    elapsed = time.monotonic() - start

    assert len(results) == 3
    # 3 serial batches: roughly 3 * 0.05 = 0.15s. Allow generous slack.
    assert elapsed >= 0.13, f"write should serialize, got {elapsed:.2f}s"


def test_run_batched_max_concurrency_caps_width(tmp_path: Any) -> None:
    delay = 0.1
    tools = [_ReadTool(f"r{i}", delay=delay) for i in range(5)]
    ex = ToolExecutor([], escalator=None, cache_dir=str(tmp_path))
    calls = [ToolCall(t.name, {}) for t in tools]

    start = time.monotonic()
    results = ex.run_batched_sync(calls, _registry(*tools), max_concurrency=2)
    elapsed = time.monotonic() - start

    assert len(results) == 5
    # 5 calls / 2 = 3 chunks, ~3 * 0.1 = 0.3s minimum.
    assert elapsed >= 0.25, f"max_concurrency=2 should chunk, got {elapsed:.2f}s"


def test_run_batched_env_var_override(tmp_path: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("CC_CORTEX_MAX_TOOL_USE_CONCURRENCY", "3")
    delay = 0.1
    tools = [_ReadTool(f"r{i}", delay=delay) for i in range(6)]
    ex = ToolExecutor([], escalator=None, cache_dir=str(tmp_path))
    calls = [ToolCall(t.name, {}) for t in tools]

    start = time.monotonic()
    ex.run_batched_sync(calls, _registry(*tools))
    elapsed = time.monotonic() - start

    # 6 / 3 = 2 chunks, ~2 * 0.1 = 0.2s.
    assert elapsed >= 0.18, f"env override should chunk to 3, got {elapsed:.2f}s"
    assert elapsed < 0.5
    # Confirm env-derived default reads correctly even when reverted.
    monkeypatch.delenv("CC_CORTEX_MAX_TOOL_USE_CONCURRENCY", raising=False)
    assert os.environ.get("CC_CORTEX_MAX_TOOL_USE_CONCURRENCY") is None


def test_run_batched_error_in_one_tool_others_complete(tmp_path: Any) -> None:
    r1 = _ReadTool("r1")
    boom = _BoomTool()
    r2 = _ReadTool("r2")
    ex = ToolExecutor([], escalator=None, cache_dir=str(tmp_path))
    calls = [ToolCall("r1", {}), ToolCall("boom", {}), ToolCall("r2", {})]

    results = ex.run_batched_sync(calls, _registry(r1, boom, r2))
    assert len(results) == 3
    assert results[0] == "ok"
    assert isinstance(results[1], RuntimeError)
    assert results[2] == "ok"
    # Both reads still ran.
    assert r1.calls == 1
    assert r2.calls == 1


def test_run_batched_async_tool_supported(tmp_path: Any) -> None:
    a1 = _AsyncReadTool("a1", delay=0.1)
    a2 = _AsyncReadTool("a2", delay=0.1)
    a3 = _AsyncReadTool("a3", delay=0.1)
    ex = ToolExecutor([], escalator=None, cache_dir=str(tmp_path))
    calls = [ToolCall("a1", {}), ToolCall("a2", {}), ToolCall("a3", {})]

    start = time.monotonic()
    results = ex.run_batched_sync(calls, _registry(a1, a2, a3))
    elapsed = time.monotonic() - start

    assert len(results) == 3
    assert results == ["async-1", "async-1", "async-1"]
    # Parallel async ≈ 0.1s, not 0.3.
    assert elapsed < 0.25, f"async parallel slow: {elapsed:.2f}s"


def test_run_batched_unknown_tool_returns_toolnotfound(tmp_path: Any) -> None:
    ex = ToolExecutor([], escalator=None, cache_dir=str(tmp_path))
    results = ex.run_batched_sync([ToolCall("ghost", {})], {})
    assert len(results) == 1
    assert isinstance(results[0], ToolNotFound)


def test_run_batched_sync_wrapper_returns_list(tmp_path: Any) -> None:
    r = _ReadTool()
    ex = ToolExecutor([], escalator=None, cache_dir=str(tmp_path))
    out = ex.run_batched_sync([ToolCall("read", {}), ToolCall("read", {})], _registry(r))
    assert isinstance(out, list)
    assert len(out) == 2


def test_run_batched_async_native_call(tmp_path: Any) -> None:
    """Call run_batched directly inside an asyncio loop."""
    r = _ReadTool(delay=0.05)
    ex = ToolExecutor([], escalator=None, cache_dir=str(tmp_path))

    async def driver() -> list[Any]:
        return await ex.run_batched(
            [ToolCall("read", {}), ToolCall("read", {})],
            _registry(r),
        )

    results = asyncio.run(driver())
    assert results == ["ok", "ok"]
    assert r.calls == 2


def test_run_batched_falls_back_to_executor_registry(tmp_path: Any) -> None:
    """When tools= omitted, executor uses its own registry."""
    r = _ReadTool()
    ex = ToolExecutor([r], escalator=None, cache_dir=str(tmp_path))
    results = ex.run_batched_sync([ToolCall("read", {})])
    assert results == ["ok"]


def test_run_batched_zero_max_concurrency_coerced_to_one(tmp_path: Any) -> None:
    r = _ReadTool()
    ex = ToolExecutor([], escalator=None, cache_dir=str(tmp_path))
    out = ex.run_batched_sync(
        [ToolCall("read", {}), ToolCall("read", {})],
        _registry(r),
        max_concurrency=0,
    )
    assert out == ["ok", "ok"]
