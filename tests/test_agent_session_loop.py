"""Tests for concinno.agent.session_loop.

Covers: @tool decorator, ToolResult paths, retry policy, schema
validation, RunContext mutation, render_system_prompt, and
multi-tool isolation.

All tests are deterministic (base_delay=0.0 throughout).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import pytest

from concinno.agent.session_loop import (
    RetryPolicy,
    RunContext,
    SessionLoop,
    ToolSpec,
    tool,
)

# ---------------------------------------------------------------------------
# Shared dataclasses used across tests
# ---------------------------------------------------------------------------


@dataclass
class AddInput:
    a: int
    b: int


@dataclass
class AddOutput:
    result: int


@dataclass
class EchoInput:
    message: str


@dataclass
class EchoOutput:
    echoed: str


@dataclass
class OptionalInput:
    x: int
    label: str = "default"


@dataclass
class OptionalOutput:
    value: str


@dataclass
class GenericInput:
    """Mixed-generic input used by ``test_session_loop_accepts_generic_hints``.

    Defined at module scope so ``typing.get_type_hints`` can resolve the
    forward references created by ``from __future__ import annotations``.
    """

    maybe: int | None
    items: list[str]
    mode: Literal["fast", "slow"]


@dataclass
class GenericOutput:
    ok: bool


# ---------------------------------------------------------------------------
# 1. @tool decorator converts function to ToolSpec
# ---------------------------------------------------------------------------


def test_tool_decorator_creates_toolspec() -> None:
    @tool()
    def add(inp: AddInput, ctx: RunContext) -> AddOutput:
        """Add two numbers."""
        return AddOutput(result=inp.a + inp.b)

    assert isinstance(add, ToolSpec)
    assert add.name == "add"
    assert add.input_type is AddInput
    assert add.output_type is AddOutput
    assert add.description == "Add two numbers."


def test_tool_decorator_custom_name() -> None:
    @tool(name="my_adder")
    def add(inp: AddInput, ctx: RunContext) -> AddOutput:
        return AddOutput(result=inp.a + inp.b)

    assert add.name == "my_adder"


def test_tool_decorator_custom_retry() -> None:
    policy = RetryPolicy(max_retries=5, base_delay=0.0)

    @tool(retry=policy)
    def add(inp: AddInput, ctx: RunContext) -> AddOutput:
        return AddOutput(result=inp.a + inp.b)

    assert add.retry.max_retries == 5


# ---------------------------------------------------------------------------
# 2. ToolResult ok path
# ---------------------------------------------------------------------------


def test_toolresult_ok_path() -> None:
    @tool()
    def echo(inp: EchoInput, ctx: RunContext) -> EchoOutput:
        return EchoOutput(echoed=inp.message)

    loop = SessionLoop(tools=[echo])
    ctx = RunContext()
    result = loop.call_tool("echo", {"message": "hello"}, ctx)

    assert result.status == "ok"
    assert result.value == EchoOutput(echoed="hello")
    assert result.error is None
    assert result.attempt == 1


# ---------------------------------------------------------------------------
# 3. Retry policy: 3rd attempt succeeds
# ---------------------------------------------------------------------------


def test_retry_succeeds_on_third_attempt() -> None:
    attempts: list[int] = []

    @tool(retry=RetryPolicy(max_retries=3, base_delay=0.0))
    def flaky(inp: AddInput, ctx: RunContext) -> AddOutput:
        attempts.append(1)
        if len(attempts) < 3:
            raise ValueError("not yet")
        return AddOutput(result=inp.a + inp.b)

    loop = SessionLoop(tools=[flaky])
    ctx = RunContext()
    result = loop.call_tool("flaky", {"a": 1, "b": 2}, ctx)

    assert result.status == "ok"
    assert result.value == AddOutput(result=3)
    assert result.attempt == 3
    assert len(attempts) == 3


# ---------------------------------------------------------------------------
# 4. Retry policy: all retries exhausted → fail
# ---------------------------------------------------------------------------


def test_retry_all_fail() -> None:
    @tool(retry=RetryPolicy(max_retries=3, base_delay=0.0))
    def always_fail(inp: AddInput, ctx: RunContext) -> AddOutput:
        raise RuntimeError("always broken")

    loop = SessionLoop(tools=[always_fail])
    ctx = RunContext()
    result = loop.call_tool("always_fail", {"a": 1, "b": 2}, ctx)

    assert result.status == "fail"
    assert result.error is not None
    assert "RuntimeError" in result.error
    assert "always broken" in result.error
    assert result.attempt == 3


# ---------------------------------------------------------------------------
# 5. Schema mismatch → fail
# ---------------------------------------------------------------------------


def test_schema_mismatch_unexpected_field() -> None:
    @tool()
    def add(inp: AddInput, ctx: RunContext) -> AddOutput:
        return AddOutput(result=inp.a + inp.b)

    loop = SessionLoop(tools=[add])
    ctx = RunContext()
    result = loop.call_tool("add", {"a": 1, "b": 2, "c": 99}, ctx)

    assert result.status == "fail"
    assert result.error is not None
    assert "schema mismatch" in result.error
    assert "c" in result.error


def test_schema_mismatch_missing_required_field() -> None:
    @tool()
    def add(inp: AddInput, ctx: RunContext) -> AddOutput:
        return AddOutput(result=inp.a + inp.b)

    loop = SessionLoop(tools=[add])
    ctx = RunContext()
    result = loop.call_tool("add", {"a": 1}, ctx)  # missing 'b'

    assert result.status == "fail"
    assert result.error is not None
    assert "schema mismatch" in result.error


def test_schema_optional_field_with_default_ok() -> None:
    @tool()
    def opt(inp: OptionalInput, ctx: RunContext) -> OptionalOutput:
        return OptionalOutput(value=f"{inp.x}:{inp.label}")

    loop = SessionLoop(tools=[opt])
    ctx = RunContext()
    # Provide only required field; 'label' has default
    result = loop.call_tool("opt", {"x": 42}, ctx)

    assert result.status == "ok"
    assert result.value == OptionalOutput(value="42:default")


# ---------------------------------------------------------------------------
# 6. Missing tool name → fail
# ---------------------------------------------------------------------------


def test_unknown_tool_name() -> None:
    @tool()
    def echo(inp: EchoInput, ctx: RunContext) -> EchoOutput:
        return EchoOutput(echoed=inp.message)

    loop = SessionLoop(tools=[echo])
    ctx = RunContext()
    result = loop.call_tool("nonexistent", {"message": "hi"}, ctx)

    assert result.status == "fail"
    assert result.error is not None
    assert "unknown tool" in result.error
    assert "echo" in result.error  # lists available tools


# ---------------------------------------------------------------------------
# 7. RunContext history is appended correctly
# ---------------------------------------------------------------------------


def test_context_history_appended() -> None:
    @tool()
    def echo(inp: EchoInput, ctx: RunContext) -> EchoOutput:
        return EchoOutput(echoed=inp.message)

    loop = SessionLoop(tools=[echo])
    ctx = RunContext()
    loop.step("echo", {"message": "hi"}, ctx)
    loop.step("echo", {"message": "world"}, ctx)

    assert len(ctx.history) == 2
    assert ctx.history[0]["tool"] == "echo"
    assert ctx.history[0]["input"] == {"message": "hi"}
    assert ctx.history[0]["status"] == "ok"
    assert ctx.history[1]["input"] == {"message": "world"}


# ---------------------------------------------------------------------------
# 8. RunContext step counter is bumped
# ---------------------------------------------------------------------------


def test_context_step_bumped() -> None:
    @tool()
    def echo(inp: EchoInput, ctx: RunContext) -> EchoOutput:
        return EchoOutput(echoed=inp.message)

    loop = SessionLoop(tools=[echo])
    ctx = RunContext()
    assert ctx.step == 0

    loop.step("echo", {"message": "a"}, ctx)
    assert ctx.step == 1

    loop.step("echo", {"message": "b"}, ctx)
    assert ctx.step == 2


# ---------------------------------------------------------------------------
# 9. render_system_prompt contains tool names and dataclass field names
# ---------------------------------------------------------------------------


def test_render_system_prompt_contains_tool_info() -> None:
    @tool()
    def add(inp: AddInput, ctx: RunContext) -> AddOutput:
        """Add two integers."""
        return AddOutput(result=inp.a + inp.b)

    @tool()
    def echo(inp: EchoInput, ctx: RunContext) -> EchoOutput:
        """Echo the message."""
        return EchoOutput(echoed=inp.message)

    loop = SessionLoop(tools=[add, echo], system_prompt="Base prompt.\n")
    rendered = loop.render_system_prompt()

    assert "Base prompt." in rendered
    assert "add" in rendered
    assert "echo" in rendered
    # dataclass field names
    assert "a" in rendered
    assert "b" in rendered
    assert "message" in rendered


# ---------------------------------------------------------------------------
# 10. base_delay=0 — no sleep, deterministic
# ---------------------------------------------------------------------------


def test_base_delay_zero_no_sleep(monkeypatch: Any) -> None:
    """Verify time.sleep is not called when base_delay=0."""
    sleep_calls: list[float] = []
    monkeypatch.setattr("concinno.agent.session_loop.time.sleep", sleep_calls.append)

    @tool(retry=RetryPolicy(max_retries=3, base_delay=0.0))
    def always_fail(inp: AddInput, ctx: RunContext) -> AddOutput:
        raise ValueError("boom")

    loop = SessionLoop(tools=[always_fail])
    loop.call_tool("always_fail", {"a": 1, "b": 2})

    assert sleep_calls == [], f"Expected no sleep calls, got {sleep_calls}"


# ---------------------------------------------------------------------------
# 11. Multiple tools do not interfere with each other
# ---------------------------------------------------------------------------


def test_multiple_tools_no_interference() -> None:
    call_log: list[str] = []

    @tool(name="tool_a")
    def tool_a(inp: AddInput, ctx: RunContext) -> AddOutput:
        call_log.append("a")
        return AddOutput(result=inp.a + inp.b)

    @tool(name="tool_b")
    def tool_b(inp: EchoInput, ctx: RunContext) -> EchoOutput:
        call_log.append("b")
        return EchoOutput(echoed=inp.message)

    loop = SessionLoop(tools=[tool_a, tool_b])
    ctx = RunContext()

    r1 = loop.step("tool_a", {"a": 1, "b": 2}, ctx)
    r2 = loop.step("tool_b", {"message": "hi"}, ctx)

    assert r1.status == "ok"
    assert r2.status == "ok"
    assert call_log == ["a", "b"]
    assert len(ctx.history) == 2
    assert ctx.history[0]["tool"] == "tool_a"
    assert ctx.history[1]["tool"] == "tool_b"


# ---------------------------------------------------------------------------
# 12. tool fn raises → caught and retried
# ---------------------------------------------------------------------------


def test_tool_fn_raise_is_caught_and_retried() -> None:
    raise_count = 0

    @tool(retry=RetryPolicy(max_retries=2, base_delay=0.0))
    def fragile(inp: EchoInput, ctx: RunContext) -> EchoOutput:
        nonlocal raise_count
        raise_count += 1
        if raise_count == 1:
            raise ConnectionError("transient")
        return EchoOutput(echoed=inp.message)

    loop = SessionLoop(tools=[fragile])
    ctx = RunContext()
    result = loop.call_tool("fragile", {"message": "test"}, ctx)

    assert result.status == "ok"
    assert result.attempt == 2
    assert raise_count == 2


# ---------------------------------------------------------------------------
# 13. @tool raises TypeError on bad signatures
# ---------------------------------------------------------------------------


def test_tool_decorator_requires_dataclass_input() -> None:
    with pytest.raises(TypeError, match="dataclass"):
        @tool()
        def bad(inp: str, ctx: RunContext) -> EchoOutput:  # str is not a dataclass
            return EchoOutput(echoed=inp)


def test_tool_decorator_requires_return_annotation() -> None:
    with pytest.raises(TypeError, match="return type annotation"):
        @tool()
        def bad(inp: EchoInput, ctx: RunContext):  # type: ignore[return]
            return EchoOutput(echoed=inp.message)


def test_tool_decorator_requires_at_least_one_param() -> None:
    with pytest.raises(TypeError, match="at least one positional parameter"):
        @tool()
        def bad() -> EchoOutput:  # type: ignore[return]
            return EchoOutput(echoed="x")


# ---------------------------------------------------------------------------
# 14. call_tool without explicit ctx creates fresh RunContext
# ---------------------------------------------------------------------------


def test_call_tool_without_ctx_creates_fresh_context() -> None:
    @tool()
    def echo(inp: EchoInput, ctx: RunContext) -> EchoOutput:
        return EchoOutput(echoed=inp.message)

    loop = SessionLoop(tools=[echo])
    # No ctx passed — should not raise
    result = loop.call_tool("echo", {"message": "no ctx"})
    assert result.status == "ok"


# ---------------------------------------------------------------------------
# 15. render_system_prompt with no tools returns base prompt unchanged
# ---------------------------------------------------------------------------


def test_render_system_prompt_no_tools() -> None:
    loop = SessionLoop(tools=[], system_prompt="Only this.")
    assert loop.render_system_prompt() == "Only this."


# ---------------------------------------------------------------------------
# 16. Runtime type validation rejects wrong-type field values
# ---------------------------------------------------------------------------


def test_session_loop_rejects_wrong_field_type() -> None:
    """A dataclass without ``__post_init__`` previously accepted any value
    as long as the field name matched. The 4.2.4 patch adds an
    ``isinstance``-based runtime check so an LLM passing
    ``{"a": 1, "b": "not-a-number"}`` for an ``int``-typed field gets a
    ``fail`` result instead of a silent accept that lies to the caller.
    """

    @tool()
    def add(inp: AddInput, ctx: RunContext) -> AddOutput:
        return AddOutput(result=inp.a + inp.b)

    loop = SessionLoop(tools=[add])
    ctx = RunContext()
    result = loop.call_tool("add", {"a": 1, "b": "not-a-number"}, ctx)

    assert result.status == "fail"
    assert result.error is not None
    assert "schema mismatch" in result.error
    assert "'b'" in result.error
    assert "expected int" in result.error
    assert "got str" in result.error


# ---------------------------------------------------------------------------
# 17. Runtime type validation tolerates Optional / Union / Literal generics
# ---------------------------------------------------------------------------


def test_session_loop_accepts_generic_hints() -> None:
    """Generics like ``Optional[int]``, ``list[str]``, and ``Literal[...]``
    must remain accepted when the value plausibly matches; the new check
    must not over-reject and break existing dataclass-based tools.
    """

    @tool(name="generic_tool")
    def generic_tool(inp: GenericInput, ctx: RunContext) -> GenericOutput:
        return GenericOutput(ok=True)

    loop = SessionLoop(tools=[generic_tool])

    # Plausible values across each generic kind.
    ok = loop.call_tool(
        "generic_tool",
        {"maybe": None, "items": ["a", "b"], "mode": "fast"},
        RunContext(),
    )
    assert ok.status == "ok"

    # Wrong literal value still rejected.
    bad_lit = loop.call_tool(
        "generic_tool",
        {"maybe": 1, "items": [], "mode": "turbo"},
        RunContext(),
    )
    assert bad_lit.status == "fail"
    assert bad_lit.error is not None
    assert "'mode'" in bad_lit.error
