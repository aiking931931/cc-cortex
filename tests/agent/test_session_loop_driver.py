"""Tests for the LLM driver layer of ``concinno.agent.session_loop``.

Covers:
    1.  ``LLMDriver`` Protocol runtime check (`@runtime_checkable`).
    2.  ``register_driver`` / ``get_driver`` round-trip.
    3.  ``LLMResponse`` dataclass shape + defaults.
    4.  ``run_session`` single-round flow with a mock driver.
    5.  ``run_session`` multi-round flow with tool dispatch.
    6.  ``ToolCall`` dispatch and ``tool_result`` injection.
    7.  Async ``acomplete`` round-trip (asyncio).
    8.  Usage tracking propagates through to the final response.
    9.  ``run_session`` accepts a string driver name (registry lookup).
   10.  ``DriverNotFoundError`` raised with helpful message on miss.
   11.  Anthropic example importability (skip if ``anthropic`` absent).
   12.  Regression: existing ``SessionLoop.step`` still works untouched.

All tests are deterministic — no real network calls, no SDK imports.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from concinno.agent.session_loop import (
    DriverNotFoundError,
    LLMDriver,
    LLMResponse,
    RunContext,
    SessionLoop,
    ToolCall,
    ToolResult,
    get_driver,
    list_drivers,
    register_driver,
    run_session,
    tool,
    unregister_driver,
)

# ---------------------------------------------------------------------------
# Fixtures: a tiny tool + a scriptable mock driver
# ---------------------------------------------------------------------------


@dataclass
class AddInput:
    a: int
    b: int


@dataclass
class AddOutput:
    sum: int


@tool(name="add")
def _add_tool(inp: AddInput, ctx: RunContext) -> AddOutput:
    """Add two integers."""
    return AddOutput(sum=inp.a + inp.b)


class MockDriver:
    """Driver that replays a pre-baked queue of :class:`LLMResponse`.

    Each ``complete`` / ``acomplete`` call pops the next scripted
    response. Useful for asserting orchestrator behaviour without an
    SDK in the loop.
    """

    def __init__(self, scripted: list[LLMResponse], model: str = "mock-1") -> None:
        self.scripted = list(scripted)
        self._model = model
        self.calls: list[dict[str, Any]] = []

    @property
    def model_id(self) -> str:
        return self._model

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        self.calls.append({"messages": messages, "tools": tools, "kwargs": kwargs})
        if not self.scripted:
            return LLMResponse(text="(empty)", stop_reason="end_turn")
        return self.scripted.pop(0)

    async def acomplete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        return self.complete(messages, tools=tools, **kwargs)


@pytest.fixture
def fresh_registry() -> Any:
    """Snapshot the registry, let the test mutate it, restore after."""
    # Import the registry dict directly so tests can inspect it.
    from concinno.agent import session_loop as sl

    snapshot = dict(sl._DRIVER_REGISTRY)
    try:
        sl._DRIVER_REGISTRY.clear()
        yield sl._DRIVER_REGISTRY
    finally:
        sl._DRIVER_REGISTRY.clear()
        sl._DRIVER_REGISTRY.update(snapshot)


# ---------------------------------------------------------------------------
# 1. LLMDriver Protocol runtime-check
# ---------------------------------------------------------------------------


def test_llmdriver_protocol_runtime_check() -> None:
    driver = MockDriver(scripted=[LLMResponse(text="hi")])
    assert isinstance(driver, LLMDriver)


def test_llmdriver_protocol_rejects_non_driver() -> None:
    class Incomplete:
        @property
        def model_id(self) -> str:
            return "x"
        # Missing complete / acomplete.

    # ``runtime_checkable`` Protocol with method members only checks
    # method *existence*, not signatures. Incomplete must NOT have
    # ``complete``/``acomplete`` to fail the check.
    assert not isinstance(Incomplete(), LLMDriver)


# ---------------------------------------------------------------------------
# 2. register_driver + get_driver round-trip
# ---------------------------------------------------------------------------


def test_register_and_get_driver_roundtrip(fresh_registry: dict[str, Any]) -> None:
    register_driver("mock", lambda: MockDriver(scripted=[]))
    drv = get_driver("mock")
    assert isinstance(drv, MockDriver)
    assert drv.model_id == "mock-1"
    assert "mock" in list_drivers()


def test_register_driver_factory_args_forwarded(
    fresh_registry: dict[str, Any],
) -> None:
    register_driver("mock", lambda model="mock-default": MockDriver([], model=model))
    drv = get_driver("mock", model="mock-override")
    assert drv.model_id == "mock-override"


def test_unregister_driver_removes_entry(fresh_registry: dict[str, Any]) -> None:
    register_driver("mock", lambda: MockDriver([]))
    assert "mock" in list_drivers()
    unregister_driver("mock")
    assert "mock" not in list_drivers()
    # Unregister of absent name is a no-op.
    unregister_driver("never-registered")


def test_register_driver_validates_inputs(fresh_registry: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        register_driver("", lambda: MockDriver([]))
    with pytest.raises(TypeError):
        register_driver("not-callable", "string-instead-of-fn")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 3. LLMResponse dataclass shape
# ---------------------------------------------------------------------------


def test_llmresponse_default_fields() -> None:
    r = LLMResponse()
    assert r.text == ""
    assert r.tool_calls == ()
    assert r.usage == {}
    assert r.stop_reason == "end_turn"
    assert r.raw is None


def test_llmresponse_immutable() -> None:
    r = LLMResponse(text="hi")
    with pytest.raises(Exception):  # FrozenInstanceError subclass of AttributeError
        r.text = "bye"  # type: ignore[misc]


def test_toolcall_immutable() -> None:
    call = ToolCall(id="abc", name="add", arguments={"a": 1, "b": 2})
    with pytest.raises(Exception):
        call.id = "xyz"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 4. run_session: single round, no tool calls (terminal end_turn)
# ---------------------------------------------------------------------------


def test_run_session_single_round_terminal() -> None:
    loop = SessionLoop(tools=[_add_tool], system_prompt="You are Test.")
    driver = MockDriver([
        LLMResponse(text="done", stop_reason="end_turn"),
    ])
    response = run_session(loop, driver, user_message="hi", max_rounds=3)
    assert response.text == "done"
    assert response.stop_reason == "end_turn"
    assert len(driver.calls) == 1
    # System prompt got rendered into kwargs
    assert "system" in driver.calls[0]["kwargs"]
    assert "You are Test." in driver.calls[0]["kwargs"]["system"]


# ---------------------------------------------------------------------------
# 5. run_session: multi-round with tool dispatch
# ---------------------------------------------------------------------------


def test_run_session_multi_round_tool_dispatch() -> None:
    loop = SessionLoop(tools=[_add_tool])
    driver = MockDriver([
        LLMResponse(
            text="thinking...",
            tool_calls=(ToolCall(id="t1", name="add", arguments={"a": 2, "b": 3}),),
            stop_reason="tool_use",
        ),
        LLMResponse(text="answer is 5", stop_reason="end_turn"),
    ])
    ctx = RunContext()
    response = run_session(
        loop, driver, user_message="What is 2+3?", ctx=ctx, max_rounds=5
    )
    assert response.text == "answer is 5"
    assert response.stop_reason == "end_turn"
    assert len(driver.calls) == 2

    # Tool dispatch updated ctx.history.
    assert len(ctx.history) == 1
    assert ctx.history[0]["tool"] == "add"
    assert ctx.history[0]["status"] == "ok"
    assert ctx.history[0]["value"] == AddOutput(sum=5)

    # Round 2 messages contain a user-side tool_result block.
    second_messages = driver.calls[1]["messages"]
    last = second_messages[-1]
    assert last["role"] == "user"
    assert isinstance(last["content"], list)
    assert last["content"][0]["type"] == "tool_result"
    assert last["content"][0]["tool_use_id"] == "t1"


# ---------------------------------------------------------------------------
# 6. run_session: max_rounds exhaustion returns "max_rounds" stop_reason
# ---------------------------------------------------------------------------


def test_run_session_max_rounds_exhausted() -> None:
    loop = SessionLoop(tools=[_add_tool])
    # Driver always asks for another tool call -> infinite loop without cap.
    looping = LLMResponse(
        text="more please",
        tool_calls=(ToolCall(id="t1", name="add", arguments={"a": 1, "b": 1}),),
        stop_reason="tool_use",
    )
    driver = MockDriver([looping, looping, looping, looping])
    response = run_session(loop, driver, user_message="loop", max_rounds=3)
    assert response.stop_reason == "max_rounds"
    assert len(driver.calls) == 3


# ---------------------------------------------------------------------------
# 7. async acomplete works with asyncio.run
# ---------------------------------------------------------------------------


def test_async_acomplete_roundtrip() -> None:
    driver = MockDriver([LLMResponse(text="async hi", stop_reason="end_turn")])

    async def _go() -> LLMResponse:
        return await driver.acomplete([{"role": "user", "content": "ping"}])

    response = asyncio.run(_go())
    assert response.text == "async hi"
    assert response.stop_reason == "end_turn"


# ---------------------------------------------------------------------------
# 8. Usage tracking flows through
# ---------------------------------------------------------------------------


def test_usage_propagates_through_response() -> None:
    driver = MockDriver([
        LLMResponse(
            text="ok",
            usage={"input_tokens": 12, "output_tokens": 4},
            stop_reason="end_turn",
        ),
    ])
    loop = SessionLoop(tools=[])
    seen: list[LLMResponse] = []
    response = run_session(
        loop,
        driver,
        user_message="hi",
        on_response=seen.append,
        max_rounds=1,
    )
    assert response.usage == {"input_tokens": 12, "output_tokens": 4}
    assert len(seen) == 1
    assert seen[0] is response


# ---------------------------------------------------------------------------
# 9. driver as string -> registry lookup
# ---------------------------------------------------------------------------


def test_run_session_string_driver_lookup(fresh_registry: dict[str, Any]) -> None:
    register_driver(
        "scripted",
        lambda: MockDriver([LLMResponse(text="from-registry", stop_reason="end_turn")]),
    )
    loop = SessionLoop(tools=[])
    response = run_session(loop, "scripted", user_message="ping", max_rounds=1)
    assert response.text == "from-registry"


# ---------------------------------------------------------------------------
# 10. DriverNotFoundError on missing name
# ---------------------------------------------------------------------------


def test_get_driver_unregistered_raises(fresh_registry: dict[str, Any]) -> None:
    with pytest.raises(DriverNotFoundError) as excinfo:
        get_driver("does-not-exist")
    assert "does-not-exist" in str(excinfo.value)
    # Subclasses KeyError for legacy callers.
    assert isinstance(excinfo.value, KeyError)


def test_run_session_invalid_max_rounds() -> None:
    loop = SessionLoop(tools=[])
    driver = MockDriver([LLMResponse()])
    with pytest.raises(ValueError):
        run_session(loop, driver, user_message="x", max_rounds=0)


# ---------------------------------------------------------------------------
# 11. Anthropic example file is importable iff anthropic is installed
# ---------------------------------------------------------------------------


def test_anthropic_example_importable_or_skipped() -> None:
    """Smoke-test that the reference driver file is syntactically valid.

    The file lives outside ``concinno`` package — load it via importlib
    by path. If ``anthropic`` is not installed we still expect the
    module body to import (the SDK import is wrapped in try/except).
    """
    import importlib.util
    import pathlib
    import sys

    examples_dir = (
        pathlib.Path(__file__).resolve().parents[2] / "examples"
    )
    target = examples_dir / "session_loop_anthropic_driver.py"
    if not target.exists():
        pytest.skip(f"reference example missing at {target}")

    module_name = "_concinno_anthropic_example"
    spec = importlib.util.spec_from_file_location(module_name, target)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # ``@dataclass`` looks the class's module up in ``sys.modules`` to
    # resolve forward refs — register before exec to avoid AttributeError.
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)

    # The driver class is always defined; only the SDK call paths
    # require ``anthropic``.
    assert hasattr(module, "AnthropicDriver")
    assert hasattr(module, "register_anthropic_driver")
    drv = module.AnthropicDriver(model="claude-opus-4-7[1m]")
    assert drv.model_id == "claude-opus-4-7[1m]"


# ---------------------------------------------------------------------------
# 12. Regression: SessionLoop.step still works (no driver involved)
# ---------------------------------------------------------------------------


def test_session_loop_step_still_works() -> None:
    loop = SessionLoop(tools=[_add_tool])
    ctx = RunContext()
    result = loop.step("add", {"a": 7, "b": 8}, ctx)
    assert isinstance(result, ToolResult)
    assert result.status == "ok"
    assert result.value == AddOutput(sum=15)
    assert ctx.step == 1
