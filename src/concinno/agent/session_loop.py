"""Session-level typed agent loop borrowing PydanticAI patterns.

@module concinno.agent.session_loop
@responsibility Provide a single-agent session loop with typed tool I/O,
    per-tool retry policy, result types, auto-generated system-prompt
    tool schema, and a run context that carries dependencies and history
    across steps.

Pure stdlib: ``dataclasses``, ``typing``, ``functools``, ``inspect``,
``time``.  Zero new dependencies.

Borrowed patterns (no import of ``pydantic`` / ``pydantic_ai``):
    1. **Typed tool I/O** — tools declare ``input_type`` / ``output_type``
       as dataclasses; ``call_tool`` validates the raw dict against the
       schema before invoking the function.
    2. **Retry policy** — each :class:`ToolSpec` carries a
       :class:`RetryPolicy` (max retries + exponential back-off).  On
       failure the error message is surfaced to the caller so an LLM
       loop can feed it back as a correction prompt.
    3. **Result types** — every invocation returns a
       :class:`ToolResult` (``ok`` | ``retry`` | ``fail``), giving callers
       a uniform type to pattern-match on.
    4. **System-prompt builder** — :meth:`SessionLoop.render_system_prompt`
       appends a structured tool-schema block to the user-supplied base
       prompt, listing each tool's name, description, and typed fields.
    5. **Run context** — :class:`RunContext` threads ``deps`` (arbitrary
       objects the loop provides to tools) and a mutable ``history`` list
       across every :meth:`SessionLoop.step` call.

Boundary with ``mas_loop``
--------------------------
``mas_loop`` is a *multi*-agent system (solver → critic → judge cascade)
operating across multiple LLM calls with role-seeded orchestration.
``session_loop`` is a *single*-agent step harness: one agent, one tool
per step, typed I/O, retry policy.  The two are composable — a MAS
solver could use ``SessionLoop`` internally — but they do not overlap.

License: AGPL-3.0-or-later
"""

from __future__ import annotations

import dataclasses
import inspect
import time
import types
import typing
from dataclasses import dataclass, field
from typing import Any, Callable, Generic, Literal, TypeVar, cast, get_type_hints

__all__ = [
    "RetryPolicy",
    "RunContext",
    "SessionLoop",
    "ToolResult",
    "ToolSpec",
    "tool",
]

T_In = TypeVar("T_In")
T_Out = TypeVar("T_Out")

# ---------------------------------------------------------------------------
# Runtime type-check helpers (stdlib-only, no pydantic)
# ---------------------------------------------------------------------------


def _value_matches_hint(value: Any, hint: Any) -> bool:
    """Best-effort ``isinstance`` check of ``value`` against type ``hint``.

    Returns ``True`` whenever the value plausibly matches, including the
    "we cannot statically verify, so accept" case for ``Any``, ``Literal``,
    unresolved ``TypeVar``, and other non-``isinstance``-able forms. The
    intent is to catch the high-frequency "string in, int expected"
    mismatch that motivated this gate, not to be a full type checker.
    """
    # ``typing.Any`` accepts everything — let downstream code raise.
    if hint is Any:
        return True

    origin = typing.get_origin(hint)
    args = typing.get_args(hint)

    # Plain class (``int``, ``str``, custom dataclass, …).
    if origin is None:
        if isinstance(hint, type):
            return isinstance(value, hint)
        # Forward refs / TypeVar / other unresolved forms — accept and
        # rely on the dataclass constructor as final gate.
        return True

    # ``Union[A, B]`` / ``Optional[X]`` (== ``Union[X, None]``).
    # PEP 604 ``A | B`` syntax has origin ``types.UnionType`` instead of
    # ``typing.Union``; both must be handled.
    if origin is typing.Union or origin is types.UnionType:
        return any(_value_matches_hint(value, arg) for arg in args)

    # ``Literal[a, b, ...]`` — equality membership rather than isinstance.
    if origin is typing.Literal:
        return value in args

    # Generic alias whose origin IS ``isinstance``-able (``list[int]`` →
    # ``list``, ``dict[str, int]`` → ``dict``, ``tuple[...]`` → ``tuple``,
    # ``set[X]`` → ``set``). We only check the container type, not the
    # element types — verifying every element would balloon the surface
    # and is what pydantic exists for.
    if isinstance(origin, type):
        return isinstance(value, origin)

    # Anything else (``Callable[...]``, ``ClassVar``, exotic generics) —
    # cannot be checked cheaply; accept and defer to the constructor.
    return True


def _format_hint(hint: Any) -> str:
    """Render a type hint for inclusion in error messages."""
    if isinstance(hint, type):
        return hint.__name__
    return str(hint).replace("typing.", "")


# ---------------------------------------------------------------------------
# Core data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolResult(Generic[T_Out]):
    """Typed result of a single tool invocation.

    :param status: ``"ok"`` on success, ``"retry"`` when the retry budget
        is not yet exhausted (returned internally between attempts),
        ``"fail"`` when retries are exhausted or a schema error occurs.
    :param value: The tool's return value on success; ``None`` otherwise.
    :param error: Human-readable error message on ``retry`` / ``fail``.
    :param attempt: Which attempt number produced this result (1-based).
    """

    status: Literal["ok", "retry", "fail"]
    value: T_Out | None = None
    error: str | None = None
    attempt: int = 1


@dataclass
class RetryPolicy:
    """Per-tool retry configuration.

    :param max_retries: Total attempts allowed (including the first).
        ``1`` means no retries.
    :param base_delay: Seconds to sleep before the *second* attempt.
        ``0.0`` disables sleeping (deterministic for tests).
    :param backoff_factor: Multiplier applied to ``base_delay`` on each
        subsequent attempt.  ``2.0`` gives exponential back-off.
    """

    max_retries: int = 3
    base_delay: float = 0.0  # 0 = no sleep; deterministic for unit tests
    backoff_factor: float = 2.0


@dataclass
class ToolSpec(Generic[T_In, T_Out]):
    """Typed descriptor for a single tool the agent can call.

    :param name: Unique tool name used in ``call_tool`` / ``step`` dispatch.
    :param description: Human-readable description injected into the
        system prompt.
    :param input_type: A ``@dataclass`` class whose fields define the
        required input schema.
    :param output_type: The expected return type of the tool function.
    :param fn: The callable implementing the tool.  Signature must be
        ``(input: T_In, ctx: RunContext) -> T_Out``.
    :param retry: Retry policy for this tool.
    """

    name: str
    description: str
    input_type: type[T_In]
    output_type: type[T_Out]
    fn: Callable[[T_In, RunContext], T_Out]
    retry: RetryPolicy = field(default_factory=RetryPolicy)


@dataclass
class RunContext:
    """Mutable context threaded through every step of a session loop.

    :param deps: Arbitrary named dependencies (database handles, config,
        external clients, …) that tool functions can retrieve by key.
    :param history: Ordered list of step records appended by
        :meth:`SessionLoop.step`.  Each record is a plain dict with at
        least ``tool``, ``input``, ``status``, and ``attempt`` keys.
    :param step: Auto-incremented counter; bumped by
        :meth:`SessionLoop.step` after each call.
    """

    deps: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    step: int = 0


# ---------------------------------------------------------------------------
# @tool decorator
# ---------------------------------------------------------------------------


def tool(
    name: str | None = None,
    *,
    retry: RetryPolicy | None = None,
) -> Callable[[Callable[..., Any]], ToolSpec[Any, Any]]:
    """Decorator that converts a typed function into a :class:`ToolSpec`.

    The decorated function must have:
      - Exactly **one positional parameter** whose annotation is a
        ``@dataclass`` (becomes ``input_type``).
      - An optional second parameter annotated as :class:`RunContext`.
      - A **return type annotation** (becomes ``output_type``).

    Example::

        @tool(name="add_numbers", retry=RetryPolicy(max_retries=2))
        def add_numbers(inp: AddInput, ctx: RunContext) -> AddOutput:
            return AddOutput(result=inp.a + inp.b)

    :param name: Override the tool name.  Defaults to the function's
        ``__name__``.
    :param retry: Override the retry policy.  Defaults to
        :class:`RetryPolicy` with its defaults.
    :returns: A :class:`ToolSpec` instance wrapping the original function.
    :raises TypeError: If the function signature does not conform.
    """

    def decorator(fn: Callable[..., Any]) -> ToolSpec[Any, Any]:
        tool_name = name or fn.__name__
        retry_policy = retry if retry is not None else RetryPolicy()

        # --- introspect signature -----------------------------------------
        try:
            hints = get_type_hints(fn)
        except Exception as exc:  # pragma: no cover
            raise TypeError(
                f"tool '{tool_name}': cannot resolve type hints — {exc}"
            ) from exc

        sig = inspect.signature(fn)
        params = [
            p
            for p in sig.parameters.values()
            if p.name != "self"
        ]

        if not params:
            raise TypeError(
                f"tool '{tool_name}': function must have at least one positional "
                "parameter typed as a dataclass (input_type)."
            )

        # First positional param is input_type
        input_param = params[0]
        input_type = hints.get(input_param.name)
        if input_type is None:
            raise TypeError(
                f"tool '{tool_name}': first parameter '{input_param.name}' "
                "must have a type annotation."
            )
        if not dataclasses.is_dataclass(input_type):
            raise TypeError(
                f"tool '{tool_name}': first parameter '{input_param.name}' "
                f"must be annotated as a @dataclass, got {input_type!r}."
            )
        # After is_dataclass guard, input_type is a dataclass *class* (not an
        # instance).  mypy cannot narrow DataclassInstance|type[...] through
        # is_dataclass(), so we cast explicitly.
        input_type_cls: type[Any] = cast("type[Any]", input_type)

        output_type = hints.get("return")
        if output_type is None:
            raise TypeError(
                f"tool '{tool_name}': return type annotation is required."
            )

        return ToolSpec(
            name=tool_name,
            description=fn.__doc__ or "",
            input_type=input_type_cls,
            output_type=output_type,
            fn=fn,
            retry=retry_policy,
        )

    return decorator


# ---------------------------------------------------------------------------
# SessionLoop
# ---------------------------------------------------------------------------


@dataclass
class SessionLoop:
    """Single-agent session loop with typed tools and retry policy.

    Usage::

        loop = SessionLoop(tools=[spec_a, spec_b], system_prompt="You are ...")
        ctx = RunContext(deps={"db": my_db})
        result = loop.step("tool_a", {"field": "value"}, ctx)
        if result.status == "ok":
            ...  # use result.value

    :param tools: List of :class:`ToolSpec` instances available to the loop.
    :param system_prompt: Base system prompt.
        :meth:`render_system_prompt` appends auto-generated tool schemas.
    """

    tools: list[ToolSpec[Any, Any]]
    system_prompt: str = ""

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_tool(self, name: str) -> ToolSpec[Any, Any] | None:
        """Return the :class:`ToolSpec` with the given name, or ``None``."""
        for t in self.tools:
            if t.name == name:
                return t
        return None

    def _validate_input(
        self, spec: ToolSpec[Any, Any], raw_input: dict[str, Any]
    ) -> tuple[Any | None, str | None]:
        """Instantiate ``spec.input_type`` from ``raw_input``.

        Performs three layers of validation, in order:

        1. **Field name presence** — reject unexpected keys and missing
           required keys (no defaults).
        2. **Runtime type check** — for each provided value, verify it
           matches the field's annotated type using ``isinstance`` against
           the resolved hint (or the generic origin for ``list[X]`` /
           ``dict[K, V]`` / ``Optional[X]`` / ``Union[A, B]``). Generics
           whose origin cannot be ``isinstance``-checked (e.g. ``Literal``,
           ``Any``, unresolved type variables) are skipped — the dataclass
           constructor remains the final gate for those.
        3. **Constructor call** — instantiate the dataclass; any
           remaining ``TypeError`` is surfaced as a schema mismatch.

        Layer 2 closes the gap where a dataclass without
        ``__post_init__`` silently accepted ``{"a": 1, "b": "x"}`` for an
        ``int``-typed field, giving the agent a false trust signal.

        Returns ``(instance, None)`` on success or ``(None, error_msg)``
        on schema mismatch.
        """
        expected_fields = {f.name for f in dataclasses.fields(spec.input_type)}
        unexpected = set(raw_input) - expected_fields
        if unexpected:
            return None, (
                f"schema mismatch: unexpected fields {sorted(unexpected)} "
                f"for tool '{spec.name}' (expected: {sorted(expected_fields)})"
            )
        missing = expected_fields - set(raw_input)
        # Allow missing fields only if they carry a default
        for f in dataclasses.fields(spec.input_type):
            if f.name in missing:
                has_default = (
                    f.default is not dataclasses.MISSING
                    or f.default_factory is not dataclasses.MISSING  # type: ignore[misc]
                )
                if not has_default:
                    return None, (
                        f"schema mismatch: missing required field '{f.name}' "
                        f"for tool '{spec.name}'"
                    )

        # Runtime type check (layer 2) — resolve forward refs once, then
        # inspect each provided value against its annotated hint.
        try:
            type_hints = get_type_hints(spec.input_type)
        except Exception:
            # Forward refs that cannot be resolved at this point (e.g. they
            # reference a name in a caller scope) — fall back to the
            # constructor-only check below rather than block validation.
            type_hints = {}
        for field_name, value in raw_input.items():
            hint = type_hints.get(field_name)
            if hint is None:
                continue
            if not _value_matches_hint(value, hint):
                expected_repr = _format_hint(hint)
                return None, (
                    f"schema mismatch: field '{field_name}' for tool "
                    f"'{spec.name}' expected {expected_repr}, got "
                    f"{type(value).__name__}"
                )

        try:
            instance = spec.input_type(**raw_input)
        except TypeError as exc:
            return None, f"schema mismatch: {exc}"
        return instance, None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def render_system_prompt(self) -> str:
        """Return ``system_prompt`` with auto-appended tool-schema block.

        Each tool's name, description, and dataclass input fields are
        listed so an LLM can understand what tools are available and
        what arguments they require.

        Example appended block::

            ## Available Tools

            ### tool_name
            Description of the tool.
            Input fields:
              - field_a (str)
              - field_b (int)

        """
        if not self.tools:
            return self.system_prompt

        lines: list[str] = ["", "## Available Tools"]
        for t in self.tools:
            lines.append(f"\n### {t.name}")
            if t.description.strip():
                # Use first non-empty line of description
                first_line = next(
                    (ln.strip() for ln in t.description.splitlines() if ln.strip()),
                    "",
                )
                if first_line:
                    lines.append(first_line)
            fields = dataclasses.fields(t.input_type)
            if fields:
                lines.append("Input fields:")
                for f in fields:
                    type_name = (
                        f.type.__name__
                        if isinstance(f.type, type)
                        else str(f.type)
                    )
                    lines.append(f"  - {f.name} ({type_name})")

        return self.system_prompt + "\n".join(lines)

    def call_tool(
        self,
        name: str,
        raw_input: dict[str, Any],
        ctx: RunContext | None = None,
    ) -> ToolResult[Any]:
        """Validate ``raw_input`` and invoke the named tool with retry.

        :param name: Name of the tool to call.
        :param raw_input: Dict that will be validated against the tool's
            ``input_type`` dataclass and used to construct the input instance.
        :param ctx: :class:`RunContext` passed to the tool function.  A
            fresh context is created if ``None``.
        :returns: :class:`ToolResult` with ``status`` one of ``"ok"`` /
            ``"fail"``.
        """
        if ctx is None:
            ctx = RunContext()

        spec = self._get_tool(name)
        if spec is None:
            return ToolResult(
                status="fail",
                error=f"unknown tool '{name}'; available: "
                + ", ".join(t.name for t in self.tools),
                attempt=0,
            )

        # Schema validation (before entering the retry loop)
        instance, schema_error = self._validate_input(spec, raw_input)
        if schema_error is not None:
            return ToolResult(status="fail", error=schema_error, attempt=0)

        # Retry loop
        delay = spec.retry.base_delay
        for attempt in range(1, spec.retry.max_retries + 1):
            try:
                value = spec.fn(instance, ctx)  # type: ignore[arg-type]
                return ToolResult(status="ok", value=value, attempt=attempt)
            except Exception as exc:
                error_msg = f"{type(exc).__name__}: {exc}"
                if attempt < spec.retry.max_retries:
                    if delay > 0:
                        time.sleep(delay)
                    delay *= spec.retry.backoff_factor
                    # continue to next attempt
                else:
                    return ToolResult(
                        status="fail",
                        error=error_msg,
                        attempt=attempt,
                    )

        # Should be unreachable, but satisfies type checker
        return ToolResult(  # pragma: no cover
            status="fail", error="retry budget exhausted", attempt=spec.retry.max_retries
        )

    def step(
        self,
        tool_name: str,
        raw_input: dict[str, Any],
        ctx: RunContext,
    ) -> ToolResult[Any]:
        """Execute one loop step: call the tool, record history, bump step.

        :param tool_name: Name of the tool to invoke.
        :param raw_input: Input dict forwarded to :meth:`call_tool`.
        :param ctx: Mutable :class:`RunContext`; ``history`` and ``step``
            are updated in place after the call returns.
        :returns: The :class:`ToolResult` from :meth:`call_tool`.
        """
        result = self.call_tool(tool_name, raw_input, ctx)
        ctx.history.append(
            {
                "tool": tool_name,
                "input": raw_input,
                "status": result.status,
                "attempt": result.attempt,
                "error": result.error,
                "value": result.value,
            }
        )
        ctx.step += 1
        return result
