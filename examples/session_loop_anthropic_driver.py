"""Anthropic LLM driver for ``SessionLoop`` — reference implementation.

This file is **not** part of the importable ``concinno`` package; it
lives in ``examples/`` so that ``concinno`` itself stays free of any
hard dependency on ``anthropic``. Users wanting to drive
:class:`concinno.agent.session_loop.SessionLoop` with Claude either:

  * Copy this file into their own project and tweak as needed, or
  * Import it via path / install ``concinno[llm]`` extras (which
    pulls in ``anthropic>=0.40``) and adapt.

Usage sketch::

    from concinno.agent import SessionLoop, register_driver, run_session
    from session_loop_anthropic_driver import (
        AnthropicDriver, register_anthropic_driver,
    )

    register_anthropic_driver()  # registers under name "anthropic"
    loop = SessionLoop(tools=[...], system_prompt="You are ...")
    response = run_session(
        loop,
        "anthropic",
        user_message="What's 2 + 2?",
        max_rounds=4,
    )
    print(response.text)

The driver maps the Anthropic ``messages.create`` SDK return value
into a provider-agnostic :class:`LLMResponse`:

  * ``content`` text blocks → ``LLMResponse.text``
  * ``content`` tool_use blocks → ``LLMResponse.tool_calls``
  * ``usage`` → ``LLMResponse.usage``
  * ``stop_reason`` is passed through verbatim (Anthropic's vocab —
    ``end_turn`` / ``tool_use`` / ``max_tokens`` / ``stop_sequence`` —
    already matches the ``LLMResponse`` convention).

License: AGPL-3.0-or-later (matches ``concinno``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from concinno.agent.session_loop import (
    LLMResponse,
    ToolCall,
    register_driver,
)

# ``anthropic`` is an optional dependency. Importing at module load
# would force every consumer of ``examples/`` to install it; instead
# we defer to first-call and fail with a clear error message.
try:
    import anthropic as _anthropic
    _HAS_ANTHROPIC = True
except ImportError:  # pragma: no cover — covered via the import-skip test
    _anthropic = None  # type: ignore[assignment]
    _HAS_ANTHROPIC = False


DEFAULT_MODEL = "claude-opus-4-7[1m]"


@dataclass
class AnthropicDriver:
    """Anthropic SDK driver satisfying :class:`LLMDriver`.

    :param model: Anthropic model identifier (e.g. ``claude-opus-4-7[1m]``).
    :param api_key: Optional explicit API key. Falls back to
        ``ANTHROPIC_API_KEY`` env var if ``None``.
    :param max_tokens: Default ``max_tokens`` per call. Overridable per
        ``complete`` invocation via ``**kwargs``.
    :param client: Optional pre-built ``anthropic.Anthropic`` client.
        Useful in tests for injecting a fake; if ``None`` a fresh client
        is constructed lazily on first ``complete`` call.
    """

    model: str = DEFAULT_MODEL
    api_key: str | None = None
    max_tokens: int = 4096
    client: Any = None

    def _ensure_client(self) -> Any:
        if self.client is not None:
            return self.client
        if not _HAS_ANTHROPIC:
            raise RuntimeError(
                "AnthropicDriver requires the 'anthropic' package. Install "
                "with: pip install 'concinno[llm]' or pip install anthropic"
            )
        key = self.api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError(
                "AnthropicDriver: no api_key passed and ANTHROPIC_API_KEY "
                "is not set"
            )
        self.client = _anthropic.Anthropic(api_key=key)
        return self.client

    @property
    def model_id(self) -> str:
        return self.model

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Call ``messages.create`` and normalise the result."""
        client = self._ensure_client()
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": kwargs.pop("max_tokens", self.max_tokens),
            "messages": messages,
        }
        if tools:
            params["tools"] = tools
        params.update(kwargs)
        raw = client.messages.create(**params)
        return self._normalise(raw)

    async def acomplete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Async variant — uses ``anthropic.AsyncAnthropic`` if available.

        For brevity this reference impl spins up an
        ``AsyncAnthropic`` client per call. Real production code should
        cache the async client on the driver instance.
        """
        if not _HAS_ANTHROPIC:
            raise RuntimeError(
                "AnthropicDriver.acomplete requires the 'anthropic' package"
            )
        key = self.api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError(
                "AnthropicDriver.acomplete: no api_key and "
                "ANTHROPIC_API_KEY not set"
            )
        async_client = _anthropic.AsyncAnthropic(api_key=key)
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": kwargs.pop("max_tokens", self.max_tokens),
            "messages": messages,
        }
        if tools:
            params["tools"] = tools
        params.update(kwargs)
        raw = await async_client.messages.create(**params)
        return self._normalise(raw)

    @staticmethod
    def _normalise(raw: Any) -> LLMResponse:
        """Map an ``anthropic.types.Message`` into :class:`LLMResponse`."""
        text_chunks: list[str] = []
        calls: list[ToolCall] = []
        for block in getattr(raw, "content", []) or []:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text_chunks.append(getattr(block, "text", "") or "")
            elif block_type == "tool_use":
                arguments = getattr(block, "input", None) or {}
                if not isinstance(arguments, dict):
                    arguments = dict(arguments)  # best-effort
                calls.append(
                    ToolCall(
                        id=str(getattr(block, "id", "")),
                        name=str(getattr(block, "name", "")),
                        arguments=arguments,
                    )
                )

        usage_obj = getattr(raw, "usage", None)
        usage: dict[str, int] = {}
        if usage_obj is not None:
            for field_name in (
                "input_tokens",
                "output_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            ):
                value = getattr(usage_obj, field_name, None)
                if value is not None:
                    usage[field_name] = int(value)

        stop_reason = str(getattr(raw, "stop_reason", "end_turn") or "end_turn")

        return LLMResponse(
            text="".join(text_chunks),
            tool_calls=tuple(calls),
            usage=usage,
            stop_reason=stop_reason,
            raw=raw,
        )


def register_anthropic_driver(name: str = "anthropic", **factory_defaults: Any) -> None:
    """Register :class:`AnthropicDriver` under the given name.

    Default factory_kwargs are baked into the closure; per-call
    overrides via :func:`get_driver` ``*args/**kwargs`` still work.
    """

    def factory(**kwargs: Any) -> AnthropicDriver:
        merged = {**factory_defaults, **kwargs}
        return AnthropicDriver(**merged)

    register_driver(name, factory)


if __name__ == "__main__":  # pragma: no cover — manual smoke test
    if not _HAS_ANTHROPIC:
        print("anthropic package not installed; skipping smoke test")
    else:
        register_anthropic_driver()
        from concinno.agent.session_loop import SessionLoop, run_session

        loop = SessionLoop(tools=[], system_prompt="You are a helpful assistant.")
        out = run_session(
            loop,
            "anthropic",
            user_message="Reply with the single word OK.",
            max_rounds=1,
        )
        print(f"text={out.text!r} stop_reason={out.stop_reason} usage={out.usage}")
