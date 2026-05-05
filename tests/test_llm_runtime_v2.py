"""Tests for Concinno 2.21.0 in-process runtime + multimodal router.

Covers ``concinno.llm_runtime.in_process.InProcessLlamaCppBackend``
(zero-HTTP GGUF load via ``llama_cpp.Llama``) and
``concinno.llm_runtime.multimodal_router.MultimodalRouter``
(dispatch across co-resident text / vision / audio backends).

The ``llama_cpp`` package is an optional runtime dep (``[llm-local]``
extra) and we avoid pulling it in for unit tests — every backend
that would touch :class:`llama_cpp.Llama` is constructed with an
injected fake or has the import path mocked so the suite runs on
the lightweight ``pip install -e .[dev]`` baseline.
"""

from __future__ import annotations

import sys
import types
from contextlib import contextmanager
from typing import Any

import pytest

from concinno.llm_runtime.in_process import (
    InProcessLlamaCppBackend,
    _extract_gemma_tool_calls,
    _strip_gemma_tool_calls,
)
from concinno.llm_runtime.multimodal_router import (
    MultimodalRouter,
    RoutingDecision,
    _has_audio,
    _has_image,
)

# ─────────────────── Fake llama_cpp.Llama ───────────────────


class _FakeLlama:
    """In-memory stand-in for :class:`llama_cpp.Llama`.

    Records ``create_chat_completion`` kwargs so tests can assert on
    message translation + tools passthrough, and returns whatever
    shape the test configures.
    """

    last_init_kwargs: dict[str, Any] | None = None

    def __init__(self, **kwargs: Any) -> None:
        _FakeLlama.last_init_kwargs = kwargs
        self.ccc_calls: list[dict[str, Any]] = []
        self.ccc_returns: list[dict[str, Any]] = []
        self.ccc_raises: Exception | None = None

    def create_chat_completion(
        self, **kwargs: Any,
    ) -> dict[str, Any]:
        if self.ccc_raises is not None:
            raise self.ccc_raises
        self.ccc_calls.append(kwargs)
        if self.ccc_returns:
            return self.ccc_returns.pop(0)
        return {
            "choices": [{
                "message": {"content": "ok", "tool_calls": []},
                "finish_reason": "stop",
            }],
        }


@contextmanager
def _mock_llama_module(*fake_instances: _FakeLlama):
    """Inject a fake ``llama_cpp`` module into ``sys.modules``.

    ``InProcessLlamaCppBackend._ensure_loaded`` does
    ``from llama_cpp import Llama`` lazily on the first chat, so
    the simplest portable shim is to put a minimal module-like
    object in ``sys.modules["llama_cpp"]`` whose ``Llama`` attribute
    pops fakes from a queue. Works on machines without the
    ``llama-cpp-python`` optional dep installed (tests run on
    ``pip install -e .[dev]`` by design).
    """
    queue = list(fake_instances) or [_FakeLlama()]
    call_idx = {"i": 0}

    def _ctor(**kwargs: Any) -> _FakeLlama:
        i = call_idx["i"]
        call_idx["i"] = i + 1
        if i < len(queue):
            fake = queue[i]
            _FakeLlama.last_init_kwargs = kwargs
            return fake
        return _FakeLlama(**kwargs)

    fake_module = types.SimpleNamespace(Llama=_ctor)
    prev = sys.modules.get("llama_cpp")
    sys.modules["llama_cpp"] = fake_module  # type: ignore[assignment]
    try:
        yield
    finally:
        if prev is None:
            sys.modules.pop("llama_cpp", None)
        else:
            sys.modules["llama_cpp"] = prev


# ─────────────────── InProcessLlamaCppBackend ───────────────────


def test_inproc_construction_uses_env_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONCINNO_LLM_GGUF_PATH", "/fake/model.gguf")
    monkeypatch.setenv("CONCINNO_LLM_N_CTX", "4096")
    monkeypatch.setenv("CONCINNO_LLM_N_GPU_LAYERS", "20")
    monkeypatch.setenv("CONCINNO_LLM_FLASH_ATTN", "0")
    # Env is read at module import time (module-level constants) — a
    # re-import is necessary for the env changes to take effect.
    import importlib

    from concinno.llm_runtime import in_process as inproc_mod
    importlib.reload(inproc_mod)
    backend = inproc_mod.InProcessLlamaCppBackend()
    assert backend._model_path == "/fake/model.gguf"
    assert backend._n_ctx == 4096
    assert backend._n_gpu_layers == 20
    assert backend._flash_attn is False
    # Restore the module so later tests see the default state.
    importlib.reload(inproc_mod)


def test_inproc_construction_kwargs_override_env() -> None:
    backend = InProcessLlamaCppBackend(
        model_path="/explicit.gguf",
        n_ctx=2048,
        n_gpu_layers=-1,
        flash_attn=True,
        chat_format="llama-3",
        clip_model_path="/mm.proj",
    )
    assert backend._model_path == "/explicit.gguf"
    assert backend._n_ctx == 2048
    assert backend._n_gpu_layers == -1
    assert backend._flash_attn is True
    assert backend._chat_format == "llama-3"
    assert backend._clip_model_path == "/mm.proj"


def test_inproc_is_loaded_starts_false() -> None:
    backend = InProcessLlamaCppBackend(model_path="/x.gguf")
    assert backend.is_loaded is False


def test_inproc_chat_lazy_loads_and_returns_content() -> None:
    fake = _FakeLlama()
    fake.ccc_returns.append({
        "choices": [{
            "message": {"content": "4", "tool_calls": []},
            "finish_reason": "stop",
        }],
    })
    backend = InProcessLlamaCppBackend(model_path="/m.gguf")
    with _mock_llama_module(fake):
        result = backend.chat(
            "math helper",
            [{"role": "user", "content": "2+2"}],
            max_tokens=32,
            temperature=0.1,
        )
    assert result == "4"
    assert backend.is_loaded is True
    # System prepended as first message.
    sent = fake.ccc_calls[0]["messages"]
    assert sent[0] == {"role": "system", "content": "math helper"}
    assert sent[1] == {"role": "user", "content": "2+2"}
    assert fake.ccc_calls[0]["max_tokens"] == 32
    assert fake.ccc_calls[0]["temperature"] == 0.1


def test_inproc_chat_returns_empty_on_exception() -> None:
    fake = _FakeLlama()
    fake.ccc_raises = RuntimeError("cuda oom")
    backend = InProcessLlamaCppBackend(model_path="/m.gguf")
    with _mock_llama_module(fake):
        assert backend.chat("", [{"role": "user", "content": "x"}]) == ""


def test_inproc_chat_handles_missing_content_key() -> None:
    fake = _FakeLlama()
    fake.ccc_returns.append({
        "choices": [{"message": {}, "finish_reason": "stop"}],
    })
    backend = InProcessLlamaCppBackend(model_path="/m.gguf")
    with _mock_llama_module(fake):
        assert backend.chat("", [{"role": "user", "content": "x"}]) == ""


def test_inproc_chat_handles_malformed_response_shape() -> None:
    fake = _FakeLlama()
    fake.ccc_returns.append({"unexpected": "shape"})
    backend = InProcessLlamaCppBackend(model_path="/m.gguf")
    with _mock_llama_module(fake):
        assert backend.chat("", [{"role": "user", "content": "x"}]) == ""


def test_inproc_chat_reuses_same_llama_across_calls() -> None:
    fake = _FakeLlama()
    fake.ccc_returns.extend([
        {"choices": [{"message": {"content": "a"}, "finish_reason": "stop"}]},
        {"choices": [{"message": {"content": "b"}, "finish_reason": "stop"}]},
    ])
    backend = InProcessLlamaCppBackend(model_path="/m.gguf")
    with _mock_llama_module(fake):
        backend.chat("", [{"role": "user", "content": "1"}])
        backend.chat("", [{"role": "user", "content": "2"}])
    # Both calls hit the same fake (single Llama instance, not two
    # reconstructions each call).
    assert len(fake.ccc_calls) == 2


def test_inproc_chat_with_tools_forwards_tools_param() -> None:
    fake = _FakeLlama()
    fake.ccc_returns.append({
        "choices": [{
            "message": {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {"name": "foo", "arguments": "{}"},
                    },
                ],
            },
            "finish_reason": "tool_calls",
        }],
    })
    backend = InProcessLlamaCppBackend(model_path="/m.gguf")
    tools = [{
        "type": "function",
        "function": {"name": "foo", "description": "d", "parameters": {}},
    }]
    with _mock_llama_module(fake):
        choice = backend.chat_with_tools(
            system="be useful",
            messages=[{"role": "user", "content": "q"}],
            tools=tools,
            max_tokens=64,
            temperature=0.0,
        )
    assert choice["message"]["tool_calls"][0]["id"] == "call_1"
    # tools param surfaced to llama_cpp unchanged.
    assert fake.ccc_calls[0]["tools"] == tools
    assert fake.ccc_calls[0]["max_tokens"] == 64


def test_inproc_chat_with_tools_omits_tools_when_none() -> None:
    fake = _FakeLlama()
    fake.ccc_returns.append({
        "choices": [{
            "message": {"content": "hi", "tool_calls": []},
            "finish_reason": "stop",
        }],
    })
    backend = InProcessLlamaCppBackend(model_path="/m.gguf")
    with _mock_llama_module(fake):
        backend.chat_with_tools(
            system="", messages=[{"role": "user", "content": "x"}],
            tools=None,
        )
    assert "tools" not in fake.ccc_calls[0]


def test_inproc_init_kwargs_forwarded_to_llama() -> None:
    """chat_format / chat_handler / clip_model_path / extra_init_kwargs
    all land on :class:`llama_cpp.Llama.__init__`."""
    fake = _FakeLlama()
    fake.ccc_returns.append({
        "choices": [{
            "message": {"content": "x"}, "finish_reason": "stop",
        }],
    })
    sentinel_handler = object()
    backend = InProcessLlamaCppBackend(
        model_path="/m.gguf",
        n_ctx=2048,
        n_gpu_layers=10,
        flash_attn=False,
        chat_format="gemma",
        chat_handler=sentinel_handler,
        clip_model_path="/mm.proj",
        verbose=True,
        extra_init_kwargs={"seed": 42},
    )
    with _mock_llama_module(fake):
        backend.chat("", [{"role": "user", "content": "go"}])
    init = _FakeLlama.last_init_kwargs
    assert init is not None
    assert init["model_path"] == "/m.gguf"
    assert init["n_ctx"] == 2048
    assert init["n_gpu_layers"] == 10
    assert init["flash_attn"] is False
    assert init["chat_format"] == "gemma"
    assert init["chat_handler"] is sentinel_handler
    assert init["clip_model_path"] == "/mm.proj"
    assert init["verbose"] is True
    assert init["seed"] == 42  # extra_init_kwargs merged in


def test_inproc_close_releases_llama_and_reloads_on_next_call() -> None:
    fake1 = _FakeLlama()
    fake1.ccc_returns.append({
        "choices": [{
            "message": {"content": "1"}, "finish_reason": "stop",
        }],
    })
    fake2 = _FakeLlama()
    fake2.ccc_returns.append({
        "choices": [{
            "message": {"content": "2"}, "finish_reason": "stop",
        }],
    })
    backend = InProcessLlamaCppBackend(model_path="/m.gguf")
    with _mock_llama_module(fake1, fake2):
        backend.chat("", [{"role": "user", "content": "a"}])
        assert backend.is_loaded is True
        backend.close()
        assert backend.is_loaded is False
        backend.chat("", [{"role": "user", "content": "b"}])
        assert backend.is_loaded is True
    # Second call triggered a fresh Llama construction.
    assert len(fake1.ccc_calls) == 1
    assert len(fake2.ccc_calls) == 1


# ─────────────────── Gemma 4 tool-call recovery ───────────────────


def test_extract_gemma_tool_calls_single_call() -> None:
    """Recovers one ``<|tool_call>...<tool_call|>`` block from content."""
    import json

    content = (
        "thinking...\n"
        "<|tool_call>call:python_exec{code:<|\"|>round(1.35, 3)"
        "<|\"|>}<tool_call|>\n"
        "more text"
    )
    calls = _extract_gemma_tool_calls(content)
    assert len(calls) == 1
    assert calls[0]["type"] == "function"
    assert calls[0]["function"]["name"] == "python_exec"
    args = json.loads(calls[0]["function"]["arguments"])
    assert args == {"code": "round(1.35, 3)"}


def test_extract_gemma_tool_calls_multiple_in_order() -> None:
    content = (
        "<|tool_call>call:read_file{path:<|\"|>a.txt<|\"|>}<tool_call|>"
        "<|tool_call>call:run_bash{cmd:<|\"|>ls<|\"|>}<tool_call|>"
    )
    calls = _extract_gemma_tool_calls(content)
    assert [c["function"]["name"] for c in calls] == ["read_file", "run_bash"]
    # IDs are unique + ordered so the agent loop can key tool_response
    # by call_id on the next round.
    assert calls[0]["id"] != calls[1]["id"]


def test_extract_gemma_tool_calls_dedups_thinking_mode_echoes() -> None:
    """Gemma 4 thinking-mode narrates its past calls — identical
    ``<|tool_call>`` blocks must collapse to one so the agent_loop
    doesn't execute the same tool N times (2026-04-23 tiny smoke
    observed 256 identical ``python_exec{code:12*8}`` echoes in one
    response, blowing the 8K ctx via tool_response accumulation)."""
    block = "<|tool_call>call:python_exec{code:<|\"|>12 * 8<|\"|>}<tool_call|>"
    content = block * 50
    calls = _extract_gemma_tool_calls(content)
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "python_exec"


def test_extract_gemma_tool_calls_keeps_distinct_calls() -> None:
    """Dedup is by (name, args) signature — different args count as
    distinct calls even for the same tool name."""
    content = (
        "<|tool_call>call:python_exec{code:<|\"|>1+1<|\"|>}<tool_call|>"
        "<|tool_call>call:python_exec{code:<|\"|>1+1<|\"|>}<tool_call|>"
        "<|tool_call>call:python_exec{code:<|\"|>2+2<|\"|>}<tool_call|>"
    )
    calls = _extract_gemma_tool_calls(content)
    assert len(calls) == 2
    import json as _json
    args0 = _json.loads(calls[0]["function"]["arguments"])
    args1 = _json.loads(calls[1]["function"]["arguments"])
    assert {args0["code"], args1["code"]} == {"1+1", "2+2"}


def test_extract_gemma_tool_calls_caps_distinct_burst() -> None:
    """Even for genuinely-distinct calls we cap per-response so a
    runaway thinking-mode doesn't flood the agent_loop."""
    # 10 distinct calls; default cap is 3.
    content = "".join(
        f"<|tool_call>call:python_exec{{code:<|\"|>{i}+{i}<|\"|>}}<tool_call|>"
        for i in range(10)
    )
    calls = _extract_gemma_tool_calls(content)
    assert len(calls) == 3


def test_extract_gemma_tool_calls_respects_max_calls_kwarg() -> None:
    content = "".join(
        f"<|tool_call>call:python_exec{{code:<|\"|>{i}<|\"|>}}<tool_call|>"
        for i in range(5)
    )
    assert len(_extract_gemma_tool_calls(content, max_calls=1)) == 1
    assert len(_extract_gemma_tool_calls(content, max_calls=5)) == 5
    # Cap exceeding actual distinct count truncates to available.
    assert len(_extract_gemma_tool_calls(content, max_calls=100)) == 5


def test_extract_gemma_tool_calls_no_markers_returns_empty() -> None:
    assert _extract_gemma_tool_calls("plain text with no markers") == []


def test_extract_gemma_tool_calls_handles_multi_arg() -> None:
    import json

    content = (
        "<|tool_call>call:edit_file{"
        "path:<|\"|>/a.py<|\"|>,"
        "content:<|\"|>print(1)<|\"|>"
        "}<tool_call|>"
    )
    calls = _extract_gemma_tool_calls(content)
    assert len(calls) == 1
    args = json.loads(calls[0]["function"]["arguments"])
    assert args == {"path": "/a.py", "content": "print(1)"}


def test_strip_gemma_tool_calls_removes_markers() -> None:
    content = (
        "before\n"
        "<|tool_call>call:foo{x:<|\"|>1<|\"|>}<tool_call|>\n"
        "after"
    )
    stripped = _strip_gemma_tool_calls(content)
    assert stripped is not None
    assert "tool_call" not in stripped
    assert "before" in stripped
    assert "after" in stripped


def test_strip_gemma_tool_calls_returns_none_when_only_markers() -> None:
    """Pure-markers content (no free-form text) strips to nothing — we
    return ``None`` so the message can be serialised as content-less
    tool-call-only (matches OpenAI ``message.content=null`` shape)."""
    content = (
        "<|tool_call>call:foo{x:<|\"|>1<|\"|>}<tool_call|>"
    )
    assert _strip_gemma_tool_calls(content) is None


def test_inproc_chat_with_tools_recovers_gemma_tool_calls() -> None:
    """End-to-end: model emits Gemma 4 raw tool-call tokens in content,
    no ``tool_calls`` key in the response — the backend lifts them into
    OpenAI shape and fixes up ``finish_reason`` so the agent loop sees
    the same contract as the HTTP path."""
    import json

    fake = _FakeLlama()
    fake.ccc_returns.append({
        "choices": [{
            "message": {
                "content": (
                    "thought: I need to compute the distance.\n"
                    "<|tool_call>call:python_exec{"
                    "code:<|\"|>from Bio import PDB<|\"|>"
                    "}<tool_call|>"
                ),
                "tool_calls": [],
            },
            "finish_reason": "stop",
        }],
    })
    backend = InProcessLlamaCppBackend(model_path="/m.gguf")
    with _mock_llama_module(fake):
        choice = backend.chat_with_tools(
            system="helpful",
            messages=[{"role": "user", "content": "distance?"}],
            tools=[{
                "type": "function",
                "function": {
                    "name": "python_exec",
                    "description": "run python",
                    "parameters": {"type": "object"},
                },
            }],
        )
    msg = choice["message"]
    assert len(msg["tool_calls"]) == 1
    assert msg["tool_calls"][0]["function"]["name"] == "python_exec"
    args = json.loads(msg["tool_calls"][0]["function"]["arguments"])
    assert args == {"code": "from Bio import PDB"}
    assert choice["finish_reason"] == "tool_calls"
    # Surrounding free-form thought survives; raw markers stripped.
    assert "distance" in (msg["content"] or "")
    assert "<|tool_call>" not in (msg["content"] or "")


def test_inproc_chat_with_tools_skips_recovery_when_tools_empty() -> None:
    """Without ``tools=[...]`` the recovery branch does not fire —
    lets callers use the plain-chat path without paying regex cost.
    """
    fake = _FakeLlama()
    fake.ccc_returns.append({
        "choices": [{
            "message": {
                "content": (
                    "<|tool_call>call:foo{x:<|\"|>1<|\"|>}<tool_call|>"
                ),
                "tool_calls": [],
            },
            "finish_reason": "stop",
        }],
    })
    backend = InProcessLlamaCppBackend(model_path="/m.gguf")
    with _mock_llama_module(fake):
        choice = backend.chat_with_tools(
            system="", messages=[{"role": "user", "content": "x"}],
            tools=None,
        )
    assert choice["message"].get("tool_calls") in (None, [])
    # Raw markers preserved — caller didn't ask for tool handling.
    assert "<|tool_call>" in choice["message"]["content"]


def test_inproc_chat_with_tools_preserves_native_tool_calls() -> None:
    """If the underlying handler DID populate ``tool_calls`` (future
    llama_cpp Gemma 4 native tool parser, or non-Gemma model with a
    real tool-aware handler), we do NOT double-parse the content."""
    fake = _FakeLlama()
    fake.ccc_returns.append({
        "choices": [{
            "message": {
                "content": (
                    "noise <|tool_call>call:other{y:<|\"|>2<|\"|>}<tool_call|>"
                ),
                "tool_calls": [
                    {
                        "id": "native_1",
                        "function": {
                            "name": "native_tool",
                            "arguments": "{\"k\": \"v\"}",
                        },
                    },
                ],
            },
            "finish_reason": "tool_calls",
        }],
    })
    backend = InProcessLlamaCppBackend(model_path="/m.gguf")
    with _mock_llama_module(fake):
        choice = backend.chat_with_tools(
            system="", messages=[{"role": "user", "content": "x"}],
            tools=[{"type": "function", "function": {
                "name": "native_tool", "description": "",
                "parameters": {},
            }}],
        )
    # Native call preserved as-is; recovery did not touch it.
    assert len(choice["message"]["tool_calls"]) == 1
    assert choice["message"]["tool_calls"][0]["id"] == "native_1"
    # Content still contains the stray marker (native tool-call path
    # skipped recovery — the scenario is "well-formed response" so we
    # don't risk double-stripping).
    assert "<|tool_call>" in choice["message"]["content"]


# ─────────────────── _has_image / _has_audio ───────────────────


def test_has_image_openai_style_block() -> None:
    msgs = [{
        "role": "user",
        "content": [{"type": "image_url", "image_url": {"url": "data:..."}}],
    }]
    assert _has_image(msgs) is True
    assert _has_audio(msgs) is False


def test_has_image_anthropic_style_block() -> None:
    msgs = [{
        "role": "user",
        "content": [{
            "type": "image",
            "image": {"data": "b64", "media_type": "image/png"},
        }],
    }]
    assert _has_image(msgs) is True


def test_has_image_legacy_sidecar_key() -> None:
    msgs = [{"role": "user", "content": "desc", "image": "/p.png"}]
    assert _has_image(msgs) is True


def test_has_audio_openai_style_block() -> None:
    msgs = [{
        "role": "user",
        "content": [{"type": "input_audio", "input_audio": {"data": "b"}}],
    }]
    assert _has_audio(msgs) is True
    assert _has_image(msgs) is False


def test_has_image_returns_false_for_text_only() -> None:
    msgs = [{"role": "user", "content": "plain text"}]
    assert _has_image(msgs) is False
    assert _has_audio(msgs) is False


# ─────────────────── MultimodalRouter ───────────────────


class _StubBackend:
    """Bare-minimum ``LLMBackend`` satisfying the router's interface."""

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.chat_calls: list[dict[str, Any]] = []

    def chat(
        self,
        system: str,
        messages: list[dict[str, Any]],
        max_tokens: int = 2000,
        temperature: float = 0.3,
    ) -> str:
        self.chat_calls.append({
            "system": system, "messages": messages,
            "max_tokens": max_tokens, "temperature": temperature,
        })
        return f"{self.tag}:{len(messages)}"


def test_router_text_only_picks_text_backend() -> None:
    text = _StubBackend("T")
    vision = _StubBackend("V")
    router = MultimodalRouter(text_backend=text, vision_backend=vision)
    decision = router.decide([{"role": "user", "content": "hi"}])
    assert decision.modality == "text"
    assert decision.fallback is False
    assert decision.backend == "_StubBackend"


def test_router_image_routes_to_vision_when_configured() -> None:
    text = _StubBackend("T")
    vision = _StubBackend("V")
    router = MultimodalRouter(text_backend=text, vision_backend=vision)
    msgs = [{
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": "data:..."}},
        ],
    }]
    decision = router.decide(msgs)
    assert decision.modality == "vision"
    assert decision.fallback is False


def test_router_image_falls_back_to_text_when_vision_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CONCINNO_LLM_VISION_GGUF_PATH", raising=False)
    text = _StubBackend("T")
    router = MultimodalRouter(text_backend=text)  # no vision
    msgs = [{
        "role": "user",
        "content": [{"type": "image", "image": {"data": "b"}}],
    }]
    decision = router.decide(msgs)
    assert decision.modality == "vision"
    assert decision.fallback is True


def test_router_audio_routes_or_falls_back() -> None:
    text = _StubBackend("T")
    audio = _StubBackend("A")
    router_with = MultimodalRouter(text_backend=text, audio_backend=audio)
    router_no = MultimodalRouter(text_backend=text)
    msgs = [{
        "role": "user",
        "content": [{"type": "audio", "audio": {"data": "b"}}],
    }]
    assert router_with.decide(msgs).fallback is False
    assert router_no.decide(msgs).fallback is True


def test_router_chat_dispatches_to_vision_backend() -> None:
    text = _StubBackend("T")
    vision = _StubBackend("V")
    router = MultimodalRouter(text_backend=text, vision_backend=vision)
    result = router.chat(
        "sys",
        [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "x"}},
                ],
            },
        ],
    )
    # Vision backend saw the call; text backend did not.
    assert result.startswith("V:")
    assert len(vision.chat_calls) == 1
    assert len(text.chat_calls) == 0


def test_router_chat_dispatches_to_text_backend() -> None:
    text = _StubBackend("T")
    vision = _StubBackend("V")
    router = MultimodalRouter(text_backend=text, vision_backend=vision)
    result = router.chat("sys", [{"role": "user", "content": "text"}])
    assert result.startswith("T:")
    assert len(text.chat_calls) == 1
    assert len(vision.chat_calls) == 0


def test_router_capabilities_reflects_backends() -> None:
    text = _StubBackend("T")
    vision = _StubBackend("V")
    audio = _StubBackend("A")
    full = MultimodalRouter(
        text_backend=text, vision_backend=vision, audio_backend=audio,
    )
    assert full.capabilities == {
        "text": True, "vision": True, "audio": True,
    }
    text_only = MultimodalRouter(text_backend=text)
    assert text_only.capabilities["text"] is True
    assert text_only.capabilities["vision"] is False
    assert text_only.capabilities["audio"] is False


def test_router_lazy_inits_vision_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CONCINNO_LLM_VISION_GGUF_PATH`` + no injected vision backend
    triggers lazy vision backend construction — the router should
    still advertise ``vision=True`` in capabilities without loading
    the vision weights until an image message actually arrives
    (lazy-load is owned by :class:`InProcessLlamaCppBackend` itself)."""
    monkeypatch.setenv("CONCINNO_LLM_VISION_GGUF_PATH", "/fake/v.gguf")
    text = _StubBackend("T")
    router = MultimodalRouter(text_backend=text)
    assert router.capabilities["vision"] is True


def test_routing_decision_is_frozen() -> None:
    d = RoutingDecision(modality="text", backend="X", fallback=False)
    with pytest.raises(Exception):  # noqa: BLE001 — frozen=True raises FrozenInstanceError
        d.modality = "vision"  # type: ignore[misc]


# ─────────────────── Sanity — env flag end-to-end ───────────────────


def test_env_flash_attn_respects_truthy_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flash_attn env parse accepts 1/true/yes/on case-insensitively."""
    for truthy in ("1", "TRUE", "yes", "ON"):
        monkeypatch.setenv("CONCINNO_LLM_FLASH_ATTN", truthy)
        import importlib

        from concinno.llm_runtime import in_process as inproc_mod
        importlib.reload(inproc_mod)
        b = inproc_mod.InProcessLlamaCppBackend()
        assert b._flash_attn is True, f"{truthy!r} should parse as True"
    # Falsy values.
    for falsy in ("0", "false", "off", "NO", ""):
        monkeypatch.setenv("CONCINNO_LLM_FLASH_ATTN", falsy)
        import importlib

        from concinno.llm_runtime import in_process as inproc_mod
        importlib.reload(inproc_mod)
        b = inproc_mod.InProcessLlamaCppBackend()
        assert b._flash_attn is False, f"{falsy!r} should parse as False"
    # Restore clean state.
    monkeypatch.delenv("CONCINNO_LLM_FLASH_ATTN", raising=False)
    import importlib

    from concinno.llm_runtime import in_process as inproc_mod
    importlib.reload(inproc_mod)


# ─────────────────── Speculative decoding ───────────────────
#
# ``InProcessLlamaCppBackend`` exposes ``draft_model=`` to opt into
# llama.cpp speculative decoding. Two wiring paths:
#
#   1. Kwarg — caller passes a ready ``LlamaDraftModel`` instance.
#   2. Env   — ``CONCINNO_LLM_SPECULATIVE=prompt_lookup`` triggers a
#              zero-VRAM ``LlamaPromptLookupDecoding`` factory call at
#              load time (n-gram / num-pred tuneables read from paired
#              env vars, fall back to llama-cpp-python's defaults of
#              2 / 10 on parse failure).
#
# The tests inject a fake ``llama_cpp.llama_speculative`` module whose
# ``LlamaPromptLookupDecoding`` records the ngram + num_pred kwargs it
# was called with, so we can assert end-to-end env-parse correctness
# without requiring the heavy ``llama-cpp-python`` optional dep.


class _FakePromptLookup:
    """Stand-in for ``llama_cpp.llama_speculative.LlamaPromptLookupDecoding``."""

    last_init_kwargs: dict[str, Any] | None = None

    def __init__(
        self,
        max_ngram_size: int = 2,
        num_pred_tokens: int = 10,
    ) -> None:
        _FakePromptLookup.last_init_kwargs = {
            "max_ngram_size": max_ngram_size,
            "num_pred_tokens": num_pred_tokens,
        }


@contextmanager
def _mock_llama_speculative():
    """Inject a fake ``llama_cpp.llama_speculative`` module."""
    fake_module = types.SimpleNamespace(
        LlamaPromptLookupDecoding=_FakePromptLookup,
    )
    prev = sys.modules.get("llama_cpp.llama_speculative")
    sys.modules["llama_cpp.llama_speculative"] = (
        fake_module  # type: ignore[assignment]
    )
    _FakePromptLookup.last_init_kwargs = None
    try:
        yield
    finally:
        if prev is None:
            sys.modules.pop("llama_cpp.llama_speculative", None)
        else:
            sys.modules["llama_cpp.llama_speculative"] = prev


def test_make_prompt_lookup_draft_defaults() -> None:
    """Factory calls ``LlamaPromptLookupDecoding(2, 10)`` by default —
    the exact llama-cpp-python 0.3.x constructor defaults."""
    from concinno.llm_runtime.in_process import make_prompt_lookup_draft

    with _mock_llama_speculative():
        draft = make_prompt_lookup_draft()
    assert isinstance(draft, _FakePromptLookup)
    assert _FakePromptLookup.last_init_kwargs == {
        "max_ngram_size": 2,
        "num_pred_tokens": 10,
    }


def test_make_prompt_lookup_draft_forwards_kwargs() -> None:
    """Explicit tuneables land on ``LlamaPromptLookupDecoding``."""
    from concinno.llm_runtime.in_process import make_prompt_lookup_draft

    with _mock_llama_speculative():
        draft = make_prompt_lookup_draft(max_ngram_size=4, num_pred_tokens=20)
    assert isinstance(draft, _FakePromptLookup)
    assert _FakePromptLookup.last_init_kwargs == {
        "max_ngram_size": 4,
        "num_pred_tokens": 20,
    }


def test_make_prompt_lookup_draft_is_reexported_by_package() -> None:
    """Public API surface — callers can import without the submodule path.

    Uses name + module identity rather than ``is`` because earlier
    tests in this suite reload ``in_process`` via ``importlib.reload``,
    which rebinds the module-level function and leaves the package-
    level ``from ... import`` dangling at the pre-reload object. Both
    still point at the same qualified callable.
    """
    import concinno.llm_runtime as pkg

    assert "make_prompt_lookup_draft" in pkg.__all__
    assert hasattr(pkg, "make_prompt_lookup_draft")
    factory = pkg.make_prompt_lookup_draft
    assert callable(factory)
    assert factory.__name__ == "make_prompt_lookup_draft"
    # Qualified module path — confirms the symbol actually comes from
    # :mod:`concinno.llm_runtime.in_process`, not a shadow definition.
    assert factory.__module__ == "concinno.llm_runtime.in_process"


def test_inproc_draft_model_kwarg_forwarded_to_llama(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit ``draft_model=<instance>`` passes straight to
    ``llama_cpp.Llama(draft_model=...)`` with no env intervention."""
    monkeypatch.delenv("CONCINNO_LLM_SPECULATIVE", raising=False)
    sentinel_draft = object()
    fake = _FakeLlama()
    fake.ccc_returns.append({
        "choices": [{
            "message": {"content": "ok"}, "finish_reason": "stop",
        }],
    })
    backend = InProcessLlamaCppBackend(
        model_path="/m.gguf",
        draft_model=sentinel_draft,
    )
    with _mock_llama_module(fake):
        backend.chat("", [{"role": "user", "content": "x"}])
    init = _FakeLlama.last_init_kwargs
    assert init is not None
    assert init["draft_model"] is sentinel_draft


def test_inproc_no_draft_model_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No kwarg, no env → ``Llama.__init__`` kwargs omit ``draft_model``."""
    monkeypatch.delenv("CONCINNO_LLM_SPECULATIVE", raising=False)
    fake = _FakeLlama()
    fake.ccc_returns.append({
        "choices": [{
            "message": {"content": "ok"}, "finish_reason": "stop",
        }],
    })
    backend = InProcessLlamaCppBackend(model_path="/m.gguf")
    with _mock_llama_module(fake):
        backend.chat("", [{"role": "user", "content": "x"}])
    init = _FakeLlama.last_init_kwargs
    assert init is not None
    assert "draft_model" not in init


def test_inproc_env_prompt_lookup_builds_default_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CONCINNO_LLM_SPECULATIVE=prompt_lookup`` → factory runs with
    llama-cpp-python defaults (2 / 10)."""
    monkeypatch.setenv("CONCINNO_LLM_SPECULATIVE", "prompt_lookup")
    monkeypatch.delenv("CONCINNO_LLM_SPECULATIVE_NGRAM_SIZE", raising=False)
    monkeypatch.delenv(
        "CONCINNO_LLM_SPECULATIVE_NUM_PRED_TOKENS", raising=False,
    )
    fake = _FakeLlama()
    fake.ccc_returns.append({
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
    })
    backend = InProcessLlamaCppBackend(model_path="/m.gguf")
    with _mock_llama_module(fake), _mock_llama_speculative():
        backend.chat("", [{"role": "user", "content": "x"}])
    init = _FakeLlama.last_init_kwargs
    assert init is not None
    draft = init["draft_model"]
    assert isinstance(draft, _FakePromptLookup)
    assert _FakePromptLookup.last_init_kwargs == {
        "max_ngram_size": 2,
        "num_pred_tokens": 10,
    }


def test_inproc_env_prompt_lookup_uses_custom_tuneables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Paired env vars flow through to the factory."""
    monkeypatch.setenv("CONCINNO_LLM_SPECULATIVE", "prompt_lookup")
    monkeypatch.setenv("CONCINNO_LLM_SPECULATIVE_NGRAM_SIZE", "3")
    monkeypatch.setenv("CONCINNO_LLM_SPECULATIVE_NUM_PRED_TOKENS", "7")
    fake = _FakeLlama()
    fake.ccc_returns.append({
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
    })
    backend = InProcessLlamaCppBackend(model_path="/m.gguf")
    with _mock_llama_module(fake), _mock_llama_speculative():
        backend.chat("", [{"role": "user", "content": "x"}])
    assert _FakePromptLookup.last_init_kwargs == {
        "max_ngram_size": 3,
        "num_pred_tokens": 7,
    }


def test_inproc_env_ngram_fallback_on_malformed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Typo in env degrades to llama-cpp defaults, not a GGUF load crash."""
    monkeypatch.setenv("CONCINNO_LLM_SPECULATIVE", "prompt_lookup")
    monkeypatch.setenv("CONCINNO_LLM_SPECULATIVE_NGRAM_SIZE", "garbage")
    monkeypatch.setenv("CONCINNO_LLM_SPECULATIVE_NUM_PRED_TOKENS", "-5")
    fake = _FakeLlama()
    fake.ccc_returns.append({
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
    })
    backend = InProcessLlamaCppBackend(model_path="/m.gguf")
    with _mock_llama_module(fake), _mock_llama_speculative():
        backend.chat("", [{"role": "user", "content": "x"}])
    # Bad values → library defaults.
    assert _FakePromptLookup.last_init_kwargs == {
        "max_ngram_size": 2,
        "num_pred_tokens": 10,
    }


def test_inproc_env_unknown_mode_disables_speculative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env set to anything other than ``prompt_lookup`` is treated as off."""
    monkeypatch.setenv("CONCINNO_LLM_SPECULATIVE", "garbage")
    fake = _FakeLlama()
    fake.ccc_returns.append({
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
    })
    backend = InProcessLlamaCppBackend(model_path="/m.gguf")
    with _mock_llama_module(fake), _mock_llama_speculative():
        backend.chat("", [{"role": "user", "content": "x"}])
    init = _FakeLlama.last_init_kwargs
    assert init is not None
    assert "draft_model" not in init
    # Factory never fired.
    assert _FakePromptLookup.last_init_kwargs is None


def test_inproc_env_prompt_lookup_is_case_insensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deploy configs sometimes shout — normalise the env value."""
    monkeypatch.setenv("CONCINNO_LLM_SPECULATIVE", "PROMPT_LOOKUP")
    fake = _FakeLlama()
    fake.ccc_returns.append({
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
    })
    backend = InProcessLlamaCppBackend(model_path="/m.gguf")
    with _mock_llama_module(fake), _mock_llama_speculative():
        backend.chat("", [{"role": "user", "content": "x"}])
    assert isinstance(
        _FakeLlama.last_init_kwargs["draft_model"],  # type: ignore[index]
        _FakePromptLookup,
    )


def test_inproc_kwarg_wins_over_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit kwarg beats env — precedence rule from the docstring."""
    monkeypatch.setenv("CONCINNO_LLM_SPECULATIVE", "prompt_lookup")
    sentinel = object()
    fake = _FakeLlama()
    fake.ccc_returns.append({
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
    })
    backend = InProcessLlamaCppBackend(
        model_path="/m.gguf", draft_model=sentinel,
    )
    with _mock_llama_module(fake), _mock_llama_speculative():
        backend.chat("", [{"role": "user", "content": "x"}])
    init = _FakeLlama.last_init_kwargs
    assert init is not None
    # The kwarg sentinel, not a _FakePromptLookup instance, reached Llama.
    assert init["draft_model"] is sentinel
    # Factory was not invoked — kwarg short-circuited env path.
    assert _FakePromptLookup.last_init_kwargs is None


def test_inproc_env_prompt_lookup_survives_missing_optional_dep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the ``[llm-local]`` extra is absent the import of
    ``llama_cpp.llama_speculative`` raises ``ImportError`` — the backend
    must silently drop speculative (the main ``Llama()`` load will
    surface the canonical import error a line later)."""
    monkeypatch.setenv("CONCINNO_LLM_SPECULATIVE", "prompt_lookup")
    fake = _FakeLlama()
    fake.ccc_returns.append({
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
    })
    # Poison the import: no fake speculative module in sys.modules.
    sys.modules.pop("llama_cpp.llama_speculative", None)
    backend = InProcessLlamaCppBackend(model_path="/m.gguf")
    with _mock_llama_module(fake):
        backend.chat("", [{"role": "user", "content": "x"}])
    init = _FakeLlama.last_init_kwargs
    assert init is not None
    # ImportError inside _resolve_draft_model → no draft_model kwarg.
    assert "draft_model" not in init


def test_resolve_draft_model_kwarg_short_circuits_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unit-level: ``_resolve_draft_model`` returns the kwarg verbatim
    when set, never consulting env. Catches regressions where a later
    refactor accidentally merges env with kwarg instead of choosing."""
    monkeypatch.setenv("CONCINNO_LLM_SPECULATIVE", "prompt_lookup")
    sentinel = object()
    backend = InProcessLlamaCppBackend(
        model_path="/m.gguf", draft_model=sentinel,
    )
    # No need to mock the speculative module — env path must not run.
    assert backend._resolve_draft_model() is sentinel


def test_resolve_draft_model_returns_none_when_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CONCINNO_LLM_SPECULATIVE", raising=False)
    backend = InProcessLlamaCppBackend(model_path="/m.gguf")
    assert backend._resolve_draft_model() is None
