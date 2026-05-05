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
from typing import (
    Any,
    Awaitable,
    Callable,
    Generic,
    Literal,
    Protocol,
    TypeVar,
    cast,
    get_type_hints,
    runtime_checkable,
)

__all__ = [
    "DriverNotFoundError",
    "LLMDriver",
    "LLMResponse",
    "RetryPolicy",
    "RunContext",
    "SessionLoop",
    "ToolCall",
    "ToolResult",
    "ToolSpec",
    "get_driver",
    "list_drivers",
    "register_driver",
    "run_session",
    "tool",
    "unregister_driver",
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
                    or f.default_factory is not dataclasses.MISSING
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
                value = spec.fn(instance, ctx)
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


# ---------------------------------------------------------------------------
# LLM driver layer (4.3.0 — Week 1)
# ---------------------------------------------------------------------------
#
# The :class:`LLMDriver` Protocol decouples ``SessionLoop`` from any
# specific LLM SDK. Concrete drivers (Anthropic, OpenAI, vLLM, mock) live
# *outside* this module — typically in ``examples/`` for reference impls
# and in user code for production. The registry pattern lets callers
# swap drivers by string name without re-importing, and the optional
# ``ziq_emit`` hook exposes a single point for ZIQ outcome-bus wiring
# without coupling this module to the bus implementation (W2 lands the
# bus; W1 only exposes the surface).
#
# Design choices (per L0 鐵律 #6 DoD):
#   * Switchable: drivers are pluggable via ``register_driver`` /
#     by passing an instance directly to ``run_session``.
#   * 3-layer: this section (L1) is the contract; drivers (L2) are
#     concrete; ``examples/`` (L3) demonstrates real wiring.
#   * Lazy-load: ``anthropic`` / ``openai`` are NOT imported here —
#     drivers in ``examples/`` are the optional-dep boundary.
#   * CP-optimal: registry over abstract base class — adding a driver
#     is one ``register_driver`` call, no inheritance chain to manage.


@dataclass(frozen=True)
class ToolCall:
    """Tool-call request emitted by an LLM driver.

    :param id: Provider-assigned identifier used to correlate the call
        with its result on the next round-trip (Anthropic uses
        ``tool_use.id``; OpenAI uses ``tool_calls[i].id``).
    :param name: Tool name; matches a :class:`ToolSpec.name` registered
        on the :class:`SessionLoop`.
    :param arguments: Already-parsed argument dict. Drivers are
        responsible for JSON-decoding the provider payload before
        constructing this; the loop assumes a dict and validates against
        the ToolSpec ``input_type`` dataclass.
    """

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class LLMResponse:
    """Provider-agnostic completion result.

    Drivers normalise their SDK-specific response objects into this
    shape so callers (tests, ``run_session``, harness code) never depend
    on the underlying SDK. ``raw`` is preserved escape-hatch-style for
    callers that need provider-specific fields (cache markers, log
    probs, etc.) without forcing the loop to know about them.

    :param text: Concatenated assistant text content. Empty string when
        the response was tool-calls-only.
    :param tool_calls: Zero or more :class:`ToolCall` requests. When
        non-empty, the orchestrator dispatches each one to the
        registered :class:`ToolSpec`.
    :param usage: Token usage dict. Convention: ``input_tokens``,
        ``output_tokens``, optional ``cache_read_input_tokens`` /
        ``cache_creation_input_tokens``. Empty dict if unavailable.
    :param stop_reason: Provider-normalised stop reason. Convention:
        ``"end_turn"`` (terminal), ``"tool_use"`` (caller should
        dispatch tools and continue), ``"max_tokens"``, ``"stop_sequence"``,
        ``"error"``. Drivers should map their SDK-native value to one of
        these; unknown strings are passed through verbatim.
    :param raw: The raw provider response object (e.g. the
        ``anthropic.types.Message`` instance). Opaque to the loop.
    """

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    usage: dict[str, int] = field(default_factory=dict)
    stop_reason: str = "end_turn"
    raw: Any = None


@runtime_checkable
class LLMDriver(Protocol):
    """Protocol for any LLM provider plugged into ``SessionLoop``.

    Implementations must provide:

      * ``model_id`` — opaque string identifying the model (used for
        logging / ZIQ outcome partitioning).
      * ``complete(messages, tools=None, **kwargs)`` — synchronous
        completion. Returns :class:`LLMResponse`.
      * ``acomplete(messages, tools=None, **kwargs)`` — async variant.
        Returns an awaitable that resolves to :class:`LLMResponse`.
        Drivers without native async MAY raise ``NotImplementedError``
        and force callers to use ``complete``.

    The ``messages`` argument is a list of ``{"role", "content"}`` dicts
    using Anthropic-style structure (``role`` ∈ ``{"user", "assistant"}``,
    ``content`` either a string or a list of typed blocks). Drivers
    targeting OpenAI-shaped APIs are expected to translate internally.

    The ``tools`` argument is the provider's tool-spec dict list (same
    shape Anthropic / OpenAI accept) or ``None``. Use
    :meth:`SessionLoop.tool_specs_for_llm` to derive these from the
    registered :class:`ToolSpec` instances.
    """

    @property
    def model_id(self) -> str: ...  # pragma: no cover

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> LLMResponse: ...  # pragma: no cover

    def acomplete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> Awaitable[LLMResponse]: ...  # pragma: no cover


# ---------------------------------------------------------------------------
# Driver registry
# ---------------------------------------------------------------------------


DriverFactory = Callable[..., LLMDriver]
_DRIVER_REGISTRY: dict[str, DriverFactory] = {}


class DriverNotFoundError(KeyError):
    """Raised by :func:`get_driver` when a name is not registered.

    Subclasses ``KeyError`` so callers that already catch ``KeyError``
    keep working, while new code can match the more specific type.
    """


def register_driver(name: str, factory: DriverFactory) -> None:
    """Register a driver factory under ``name``.

    The factory is any zero-or-more-arg callable returning an object
    that satisfies the :class:`LLMDriver` Protocol. It is invoked
    lazily by :func:`get_driver`, so importing optional SDKs can be
    deferred to first use.

    Re-registering an existing name silently overwrites — this is
    intentional for test isolation (a fixture can ``register_driver``
    a mock then restore the original on teardown).

    :raises ValueError: If ``name`` is empty or not a string.
    :raises TypeError: If ``factory`` is not callable.
    """
    if not isinstance(name, str) or not name:
        raise ValueError("driver name must be a non-empty string")
    if not callable(factory):
        raise TypeError(
            f"factory for driver '{name}' must be callable, got "
            f"{type(factory).__name__}"
        )
    _DRIVER_REGISTRY[name] = factory


def unregister_driver(name: str) -> None:
    """Remove a driver from the registry. No-op if absent."""
    _DRIVER_REGISTRY.pop(name, None)


def get_driver(name: str, /, *args: Any, **kwargs: Any) -> LLMDriver:
    """Instantiate the driver registered under ``name``.

    Forwards ``*args`` and ``**kwargs`` to the factory, so drivers that
    need configuration (api_key, base_url, model_id override) can
    accept them at construction time.

    :raises DriverNotFoundError: If ``name`` is not registered.
    """
    factory = _DRIVER_REGISTRY.get(name)
    if factory is None:
        available = sorted(_DRIVER_REGISTRY)
        raise DriverNotFoundError(
            f"no driver registered under '{name}'; available: "
            + (", ".join(available) if available else "(none)")
        )
    return factory(*args, **kwargs)


def list_drivers() -> list[str]:
    """Return the names of currently-registered drivers, sorted."""
    return sorted(_DRIVER_REGISTRY)


# ---------------------------------------------------------------------------
# run_session orchestrator
# ---------------------------------------------------------------------------


def _resolve_driver(driver: LLMDriver | str) -> LLMDriver:
    """Return ``driver`` if already an instance, else look it up."""
    if isinstance(driver, str):
        return get_driver(driver)
    return driver


def _tool_specs_for_llm(loop: SessionLoop) -> list[dict[str, Any]]:
    """Render the loop's tools into Anthropic-shaped tool specs.

    Each :class:`ToolSpec` becomes one entry with ``name``,
    ``description``, and an ``input_schema`` derived from the input
    dataclass fields. The schema is a minimal JSON-schema dict (one
    level of ``type`` per field, no ``$ref`` resolution); drivers that
    need richer schemas should call back into :func:`dataclasses.fields`
    directly.
    """
    specs: list[dict[str, Any]] = []
    for t in loop.tools:
        properties: dict[str, dict[str, Any]] = {}
        required: list[str] = []
        for f in dataclasses.fields(t.input_type):
            json_type = _python_to_json_type(f.type)
            properties[f.name] = {"type": json_type}
            has_default = (
                f.default is not dataclasses.MISSING
                or f.default_factory is not dataclasses.MISSING
            )
            if not has_default:
                required.append(f.name)
        specs.append(
            {
                "name": t.name,
                "description": t.description.strip() or t.name,
                "input_schema": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            }
        )
    return specs


_PYTHON_TO_JSON_TYPES: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _python_to_json_type(hint: Any) -> str:
    """Best-effort Python type → JSON-schema type-name mapping.

    Returns ``"string"`` for anything we cannot statically map — this
    is permissive on purpose. The dataclass constructor remains the
    final type gate (see :meth:`SessionLoop._validate_input`).
    """
    if isinstance(hint, type):
        return _PYTHON_TO_JSON_TYPES.get(hint, "string")
    origin = typing.get_origin(hint)
    if origin is list or origin is tuple or origin is set:
        return "array"
    if origin is dict:
        return "object"
    return "string"


def _format_tool_result_for_llm(
    call: ToolCall, result: ToolResult[Any]
) -> dict[str, Any]:
    """Render a :class:`ToolResult` into an Anthropic-style tool_result block.

    The ``content`` is a string for compatibility with the widest set
    of providers; structured payloads can be re-serialised by the
    driver if needed.
    """
    if result.status == "ok":
        body = repr(result.value)
        is_error = False
    else:
        body = result.error or f"tool failed (status={result.status})"
        is_error = True
    return {
        "type": "tool_result",
        "tool_use_id": call.id,
        "content": body,
        "is_error": is_error,
    }


def run_session(
    loop: SessionLoop,
    driver: LLMDriver | str,
    *,
    user_message: str,
    ctx: RunContext | None = None,
    max_rounds: int = 10,
    system: str | None = None,
    extra_messages: list[dict[str, Any]] | None = None,
    on_response: Callable[[LLMResponse], None] | None = None,
    **driver_kwargs: Any,
) -> LLMResponse:
    """Run a multi-round LLM ↔ tool loop until the model stops calling tools.

    On each round:

    1. Build the message list (system prompt + extras + history).
    2. Call ``driver.complete(messages, tools=...)``.
    3. If ``stop_reason != "tool_use"`` → return the response.
    4. Otherwise, dispatch every tool_call through :meth:`SessionLoop.step`,
       append the assistant turn + ``tool_result`` blocks to history,
       and continue.

    The ``ctx`` is mutated in place: each tool dispatch pushes a record
    onto ``ctx.history`` (uniform with the existing
    :meth:`SessionLoop.step` contract). ``max_rounds`` caps the loop —
    exhaustion returns the last response with ``stop_reason="max_rounds"``.

    :param loop: The :class:`SessionLoop` whose tools the driver may call.
    :param driver: An :class:`LLMDriver` instance OR the registered name
        of one (string lookup via :func:`get_driver`).
    :param user_message: The initial user turn.
    :param ctx: Mutable :class:`RunContext`. A fresh one is created if
        ``None``.
    :param max_rounds: Hard cap on round-trips; protects against
        runaway tool-use loops. ``1`` disables tool-loop iteration
        (returns after the first response unconditionally).
    :param system: Optional system prompt override; defaults to
        :meth:`SessionLoop.render_system_prompt`.
    :param extra_messages: Pre-pended message turns (e.g. few-shot
        examples). Inserted after ``user_message`` is *not* yet wrapped
        — these go BEFORE the user turn.
    :param on_response: Optional callback fired on every LLM response;
        useful for streaming usage telemetry to a ZIQ outcome bus
        (W2 wiring point).
    :param driver_kwargs: Forwarded to ``driver.complete`` on every call.
    """
    if ctx is None:
        ctx = RunContext()
    resolved = _resolve_driver(driver)
    if max_rounds < 1:
        raise ValueError(f"max_rounds must be >= 1, got {max_rounds!r}")

    rendered_system = system if system is not None else loop.render_system_prompt()
    tool_specs = _tool_specs_for_llm(loop) if loop.tools else None

    messages: list[dict[str, Any]] = []
    if extra_messages:
        messages.extend(extra_messages)
    messages.append({"role": "user", "content": user_message})

    last_response: LLMResponse = LLMResponse(stop_reason="error")
    for _round in range(max_rounds):
        call_kwargs = dict(driver_kwargs)
        if rendered_system:
            call_kwargs.setdefault("system", rendered_system)
        response = resolved.complete(messages, tools=tool_specs, **call_kwargs)
        last_response = response
        if on_response is not None:
            on_response(response)

        if response.stop_reason != "tool_use" or not response.tool_calls:
            return response

        # Append assistant turn (text + tool_use blocks) so the next
        # round sees what the model just emitted.
        assistant_blocks: list[dict[str, Any]] = []
        if response.text:
            assistant_blocks.append({"type": "text", "text": response.text})
        for call in response.tool_calls:
            assistant_blocks.append(
                {
                    "type": "tool_use",
                    "id": call.id,
                    "name": call.name,
                    "input": call.arguments,
                }
            )
        messages.append({"role": "assistant", "content": assistant_blocks})

        # Dispatch every tool call and gather user-side tool_result blocks.
        result_blocks: list[dict[str, Any]] = []
        for call in response.tool_calls:
            result = loop.step(call.name, call.arguments, ctx)
            result_blocks.append(_format_tool_result_for_llm(call, result))
        messages.append({"role": "user", "content": result_blocks})

    # max_rounds exhausted — return last response with overridden stop_reason.
    return LLMResponse(
        text=last_response.text,
        tool_calls=last_response.tool_calls,
        usage=last_response.usage,
        stop_reason="max_rounds",
        raw=last_response.raw,
    )
