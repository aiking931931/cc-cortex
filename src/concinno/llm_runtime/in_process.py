"""In-process llama-cpp-python backend — zero HTTP, zero subprocess.

This is what Sancio wants to be: a runtime that *owns* the weights
instead of talking to an Ollama-like sidecar over loopback HTTP.
Loading a GGUF via :class:`llama_cpp.Llama` keeps the model in the same
Python process that runs the agent loop, so:

* tool_call → tool_exec → tool_response round-trips stay in local
  memory (no SSE relay strip, no JSON envelope hiccups — the
  classic Gemma 4 thinking-mode chat-template token-eating bug is
  observed across the HTTP split; in-process the tokens just stream
  back into the same sampler state).
* Latency drops 30-80 ms per iteration (no socket + HTTP framing
  + json (de)serialisation).
* One process boot, one 18-30 s GGUF load, then ~0.2 s per
  generate — same-machine sidecar can't beat this.

Prefix caching
--------------
The underlying ``llama_cpp.Llama`` object keeps a single KV cache for
its lifetime. Sequential :meth:`chat` / :meth:`chat_with_tools` calls
that share a leading prompt (system + first N user / tool messages)
automatically reuse cached keys/values from the previous generate —
llama.cpp performs prefix-match on the token stream at the C layer.
This is on by default (no flag); see llama.cpp discussions #8860 and
#13606. Because :class:`InProcessLlamaCppBackend` constructs the
``Llama`` once and holds it for the backend's lifetime, every agent-
loop iteration after the first pays only the *delta* tokens, not the
full system-prompt re-encode.

Speculative decoding
--------------------
Pass ``draft_model=`` to enable speculative decoding. Two patterns
are supported by the Python binding:

* ``LlamaPromptLookupDecoding`` — n-gram lookup inside the existing
  context. Zero extra VRAM, ~1.3-1.8× speedup on repetitive /
  code-like workloads. The zero-cost default and what Sancio should
  prefer when VRAM headroom is tight. Construct via
  :func:`make_prompt_lookup_draft` to avoid the ``llama_cpp`` import
  in callers that do not otherwise touch it.
* A second ``llama_cpp.Llama`` instance — real small-model draft
  (e.g. Gemma-4 E2B / -1B). ~2× speedup, but pays the draft model's
  weights in VRAM. The caller owns the lifecycle of the draft
  ``Llama`` (close on shutdown).

Env var ``CONCINNO_LLM_SPECULATIVE`` selects an opt-in built-in
prompt-lookup profile (``"prompt_lookup"``) when the kwarg is unset,
so deployments can flip it on without code changes.

Minimum Concinno version: 2.21.0
"""

from __future__ import annotations

import os
import threading
from typing import Any

from concinno.llm_runtime.base import ChatMessage
from concinno.llm_runtime.tool_parsers import (
    DEFAULT_TOOL_CALL_CAP,
    GemmaToolCallParser,
    get_parser,
)

# Legacy re-export names — kept so callers that imported the 2.21.0-rc
# internal helpers directly don't break when we move the logic into
# :mod:`concinno.llm_runtime.tool_parsers`. New code should call
# :func:`concinno.llm_runtime.tool_parsers.get_parser` instead.
DEFAULT_GEMMA_TOOL_CALL_CAP = DEFAULT_TOOL_CALL_CAP


def _extract_gemma_tool_calls(
    content: str,
    max_calls: int = DEFAULT_GEMMA_TOOL_CALL_CAP,
) -> list[dict[str, Any]]:
    """DEPRECATED: thin wrapper over ``GemmaToolCallParser().parse``.

    Preserved for callers that imported this private helper directly
    (handoff tools, pod diagnostic scripts). New in-tree code should
    use :func:`concinno.llm_runtime.tool_parsers.get_parser` instead.
    """
    tool_calls, _cleaned = GemmaToolCallParser().parse(
        content, max_calls=max_calls,
    )
    return tool_calls


def _strip_gemma_tool_calls(content: str) -> str | None:
    """DEPRECATED: thin wrapper over ``GemmaToolCallParser().parse``.

    Preserved for callers that imported this private helper directly.
    New in-tree code should use
    :func:`concinno.llm_runtime.tool_parsers.get_parser` instead.
    """
    _calls, cleaned = GemmaToolCallParser().parse(content)
    return cleaned

_DEFAULT_MODEL_PATH = os.environ.get(
    "CONCINNO_LLM_GGUF_PATH",
    "/workspace/gemma4-31b-it-gguf/gemma-4-31B-it-Q4_K_M.gguf",
)
_DEFAULT_N_CTX = int(os.environ.get("CONCINNO_LLM_N_CTX", "8192"))
_DEFAULT_N_GPU_LAYERS = int(os.environ.get("CONCINNO_LLM_N_GPU_LAYERS", "-1"))
_DEFAULT_FLASH_ATTN = os.environ.get(
    "CONCINNO_LLM_FLASH_ATTN", "1",
).strip().lower() in ("1", "true", "yes", "on")


# KV-cache quantisation — GGML ``type_k`` / ``type_v`` integer enum.
# Letting callers shrink the KV cache by 2-4x via 8-bit / 4-bit
# quantisation lets Sancio raise ``n_ctx`` without going OOM on the
# same VRAM envelope. Without this the FP16 KV cache at 8K ctx for
# gemma-4 31B consumes ~8GB, capping n_ctx at 8192 on a 32GB 5090
# (18GB weights + 8GB KV + overhead ≈ 26GB; 16K ctx would overflow).
# Q8_0 KV cuts that to ~4GB at 8K / ~8GB at 16K — headroom for the
# agent loop's multi-round tool_result accumulation without touching
# weight quantisation.
#
# Mapping (see ggml.h / llama_cpp.llama_types):
#     1  = GGML_TYPE_F16         (default, highest quality)
#     2  = GGML_TYPE_Q4_0        (~4x smaller, 1-2% quality loss)
#     8  = GGML_TYPE_Q8_0        (~2x smaller, negligible quality loss)
#
# Env vars ``CONCINNO_LLM_KV_TYPE_K`` / ``..._KV_TYPE_V`` take raw int.
# Unset → llama_cpp default (F16 on both keys and values).
_KV_TYPE_K_RAW = os.environ.get("CONCINNO_LLM_KV_TYPE_K")
_KV_TYPE_V_RAW = os.environ.get("CONCINNO_LLM_KV_TYPE_V")


def _parse_kv_type(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        value = int(raw.strip())
    except (ValueError, AttributeError):
        return None
    return value if value > 0 else None


_DEFAULT_KV_TYPE_K = _parse_kv_type(_KV_TYPE_K_RAW)
_DEFAULT_KV_TYPE_V = _parse_kv_type(_KV_TYPE_V_RAW)

# Speculative-decoding env chain (resolved at ``_ensure_loaded`` time so
# tests that monkey-patch env see fresh values — unlike the module-level
# KV constants which cache at import). Supported modes:
#
#   "" / unset         → no draft model (default)
#   "prompt_lookup"    → construct ``LlamaPromptLookupDecoding`` with the
#                        two tuneables below. Zero extra VRAM; relies on
#                        n-gram matching inside the active context. The
#                        default safe opt-in.
#
# The ngram/num-pred tuneables are read with ints + fall back to the
# llama-cpp defaults (2 / 10) on any parse failure so a mangled env
# value degrades gracefully instead of crashing GGUF load.
_SPECULATIVE_ENV = "CONCINNO_LLM_SPECULATIVE"
_SPECULATIVE_NGRAM_ENV = "CONCINNO_LLM_SPECULATIVE_NGRAM_SIZE"
_SPECULATIVE_NUM_PRED_ENV = "CONCINNO_LLM_SPECULATIVE_NUM_PRED_TOKENS"


def _parse_positive_int(raw: str | None, default: int) -> int:
    """Return ``int(raw)`` when strictly positive, else ``default``."""
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except (ValueError, AttributeError):
        return default
    return value if value > 0 else default


def make_prompt_lookup_draft(
    max_ngram_size: int = 2,
    num_pred_tokens: int = 10,
) -> Any:
    """Build :class:`llama_cpp.llama_speculative.LlamaPromptLookupDecoding`.

    Thin factory so ``concinno`` callers that want the zero-VRAM
    prompt-lookup speculative path do not have to import
    ``llama_cpp.llama_speculative`` themselves — the ``llama_cpp`` dep
    is an optional extra (``[llm-local]``) that only the in-process
    backend pulls in.

    Args:
        max_ngram_size: Maximum n-gram size the prompt-lookup matcher
            scans for inside the active context. ``llama-cpp-python``
            default is ``2``.
        num_pred_tokens: Number of tokens the draft proposes per step.
            ``llama-cpp-python`` default is ``10``; higher values trade
            acceptance rate for per-step work.

    Returns:
        A ``LlamaPromptLookupDecoding`` instance suitable to pass as
        ``draft_model=`` to :class:`llama_cpp.Llama` (or to
        :class:`InProcessLlamaCppBackend`).

    Raises:
        ImportError: if ``llama-cpp-python`` is not installed (the
            ``[llm-local]`` extra is missing).
    """
    from llama_cpp.llama_speculative import LlamaPromptLookupDecoding
    return LlamaPromptLookupDecoding(
        max_ngram_size=max_ngram_size,
        num_pred_tokens=num_pred_tokens,
    )


class InProcessLlamaCppBackend:
    """Direct :class:`llama_cpp.Llama` wrapper — one weight load per process.

    The ``Llama`` object is NOT thread-safe for concurrent generate
    calls (shared sampler state), so every ``chat`` invocation takes
    a per-instance lock. Throughput under concurrent load is capped
    at one in-flight generate; callers that need fan-out should
    shard across multiple ``InProcessLlamaCppBackend`` instances (one
    per GPU) or queue above this layer.

    The ``Llama`` object is lazily constructed on the first ``chat``
    call so agent loops that import this module without ever running
    a generate don't pay the 18-30 s cold-start weight-load cost.
    """

    def __init__(
        self,
        model_path: str | None = None,
        n_ctx: int | None = None,
        n_gpu_layers: int | None = None,
        flash_attn: bool | None = None,
        chat_format: str | None = None,
        chat_handler: Any | None = None,
        clip_model_path: str | None = None,
        verbose: bool = False,
        type_k: int | None = None,
        type_v: int | None = None,
        draft_model: Any | None = None,
        extra_init_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self._model_path = model_path or _DEFAULT_MODEL_PATH
        self._n_ctx = n_ctx if n_ctx is not None else _DEFAULT_N_CTX
        self._n_gpu_layers = (
            n_gpu_layers if n_gpu_layers is not None
            else _DEFAULT_N_GPU_LAYERS
        )
        self._flash_attn = (
            flash_attn if flash_attn is not None else _DEFAULT_FLASH_ATTN
        )
        # ``chat_format`` / ``chat_handler`` / ``clip_model_path`` make
        # this backend model-family agnostic — any GGUF that llama-cpp
        # understands (Gemma 4 / Qwen / Llama / Mistral / Phi / Mixtral
        # / LLaVA / ...) loads via the same class. ``chat_format`` names
        # the template ("gemma", "llama-3", "qwen", "chatml", "llava-1-5");
        # ``chat_handler`` is llama-cpp's multimodal handler class.
        self._chat_format = chat_format or os.environ.get(
            "CONCINNO_LLM_CHAT_FORMAT",
        ) or None
        self._chat_handler = chat_handler
        self._clip_model_path = clip_model_path or os.environ.get(
            "CONCINNO_LLM_CLIP_MODEL_PATH",
        ) or None
        self._verbose = verbose
        # KV-cache quantisation — kwarg overrides env, env overrides
        # None (llama_cpp default FP16). Matched-int parse already
        # applied at module load for the env values.
        self._type_k = type_k if type_k is not None else _DEFAULT_KV_TYPE_K
        self._type_v = type_v if type_v is not None else _DEFAULT_KV_TYPE_V
        # ``draft_model`` is an arbitrary llama-cpp draft object
        # (``LlamaDraftModel`` subclass or duck-typed). Kwarg wins over
        # env; when both are unset the env is re-read at load time so
        # test monkey-patching works without re-constructing the
        # backend. ``None`` disables speculative decoding (default).
        self._draft_model = draft_model
        self._extra_init_kwargs = extra_init_kwargs or {}
        self._llm: Any = None
        self._llm_lock = threading.Lock()
        self._generate_lock = threading.Lock()

    def _ensure_loaded(self) -> Any:
        with self._llm_lock:
            if self._llm is not None:
                return self._llm
            from llama_cpp import Llama
            kwargs: dict[str, Any] = {
                "model_path": self._model_path,
                "n_ctx": self._n_ctx,
                "n_gpu_layers": self._n_gpu_layers,
                "flash_attn": self._flash_attn,
                "verbose": self._verbose,
            }
            if self._chat_format:
                kwargs["chat_format"] = self._chat_format
            if self._chat_handler is not None:
                kwargs["chat_handler"] = self._chat_handler
            if self._clip_model_path:
                # llava-style multimodal: mmproj model path. Enables
                # image message content blocks when the base GGUF is a
                # vision-ready checkpoint (gemma-3 vision / llava / etc).
                kwargs["clip_model_path"] = self._clip_model_path
            if self._type_k is not None:
                kwargs["type_k"] = self._type_k
            if self._type_v is not None:
                kwargs["type_v"] = self._type_v
            draft = self._resolve_draft_model()
            if draft is not None:
                kwargs["draft_model"] = draft
            kwargs.update(self._extra_init_kwargs)
            self._llm = Llama(**kwargs)
            return self._llm

    def _resolve_draft_model(self) -> Any | None:
        """Pick the draft model to pass to ``llama_cpp.Llama``.

        Precedence (first match wins):

        1. Kwarg ``draft_model`` from ``__init__`` — caller is explicit,
           always honoured even over env.
        2. Env ``CONCINNO_LLM_SPECULATIVE`` == ``"prompt_lookup"`` —
           construct a :class:`LlamaPromptLookupDecoding` with the
           paired ``_NGRAM_SIZE`` / ``_NUM_PRED_TOKENS`` tuneables.
        3. Anything else → ``None`` (speculative decoding off).

        Malformed env values (non-positive ints, non-numeric) fall
        back to the llama-cpp-python defaults (2 / 10) so a typo in
        deploy config degrades to "works slower" instead of "won't
        load at all".
        """
        if self._draft_model is not None:
            return self._draft_model
        mode = (os.environ.get(_SPECULATIVE_ENV) or "").strip().lower()
        if mode != "prompt_lookup":
            return None
        ngram = _parse_positive_int(
            os.environ.get(_SPECULATIVE_NGRAM_ENV), default=2,
        )
        num_pred = _parse_positive_int(
            os.environ.get(_SPECULATIVE_NUM_PRED_ENV), default=10,
        )
        try:
            return make_prompt_lookup_draft(
                max_ngram_size=ngram,
                num_pred_tokens=num_pred,
            )
        except ImportError:
            # llama-cpp-python not installed — silently skip
            # speculative decoding. The main ``Llama(...)``
            # construction on the next line will hit the same
            # ImportError if the extra is missing, producing the
            # canonical error at the caller's expected site.
            return None

    def chat(
        self,
        system: str,
        messages: list[ChatMessage],
        max_tokens: int = 2000,
        temperature: float = 0.3,
    ) -> str:
        llm = self._ensure_loaded()
        msgs: list[dict[str, Any]] = [{"role": "system", "content": system}]
        msgs.extend(dict(m) for m in messages)
        try:
            with self._generate_lock:
                resp = llm.create_chat_completion(
                    messages=msgs,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
        except Exception:
            return ""
        try:
            return resp["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError):
            return ""

    def chat_with_tools(
        self,
        system: str,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 2000,
        temperature: float = 0.3,
    ) -> dict[str, Any]:
        """Raw chat-completion passthrough including ``tool_calls``.

        Returns the full ``choices[0]`` dict so agent loops can route
        ``message.tool_calls`` without needing an HTTP shim. Unlike
        :meth:`chat`, this method does NOT flatten to a string — the
        agent loop layer decides how to dispatch tool calls.
        """
        llm = self._ensure_loaded()
        msgs: list[dict[str, Any]] = [{"role": "system", "content": system}]
        msgs.extend(dict(m) for m in messages)
        kwargs: dict[str, Any] = {
            "messages": msgs,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools
        with self._generate_lock:
            resp = llm.create_chat_completion(**kwargs)
        choice = resp["choices"][0]
        # Family-aware tool-call recovery — dispatch via registry. For
        # ``chat_format=None`` (legacy default, pod loads Gemma GGUF)
        # ``get_parser`` returns :class:`GemmaToolCallParser` so the
        # behaviour matches pre-refactor 2.21.0-rc. Native-tool-calling
        # formats (Llama 3 / Mistral / functionary with proper handlers)
        # can be wired by leaving them out of the registry — ``get_parser``
        # returns ``None`` and the choice passes through untouched.
        msg = choice.get("message") or {}
        content = msg.get("content") or ""
        native_tool_calls = msg.get("tool_calls")
        parser = get_parser(self._chat_format)
        if parser and parser.should_attempt(
            content=content,
            tools_given=tools,
            native_tool_calls=native_tool_calls,
        ):
            recovered, cleaned = parser.parse(content)
            if recovered:
                msg["tool_calls"] = recovered
                msg["content"] = cleaned
                choice["message"] = msg
                # Keep the stop reason aligned with HTTP-layer output —
                # once tool_calls are surfaced, the agent loop expects
                # ``finish_reason="tool_calls"`` (OpenAI) / ``tool_use``
                # (Anthropic map) to drive the next round.
                choice["finish_reason"] = "tool_calls"
        return choice

    @property
    def is_loaded(self) -> bool:
        return self._llm is not None

    def close(self) -> None:
        """Free the GGUF weights. Calling ``chat`` after close reloads."""
        with self._llm_lock:
            self._llm = None
