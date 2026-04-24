"""LLM runtime backends — direct inference paths that bypass Ollama.

Ollama layer on top of llama.cpp introduces a degenerate loop on Gemma 4
Q4_K_M under synthesis-shape prompts (SYNTH_SYSTEM + multi-k-char evidence):
content returns empty, finish_reason=length, and latency exceeds 60s before
any token lands. See docs/2.20.0-llamacpp-runtime.md for A/B evidence and
root cause analysis.

This package provides direct llama.cpp (and, as a later plug, vLLM) runtimes
that keep the same OpenAI-compatible API shape persona-api already speaks.

Public API:
    LlamaCppBackend - thin openai.OpenAI wrapper pointing at a local server
    LlamaCppServer  - context-manager that spawns `python -m llama_cpp.server`
                      and health-checks /v1/models before yielding
"""

from __future__ import annotations

from concinno.llm_runtime.base import ChatMessage, LLMBackend
from concinno.llm_runtime.in_process import (
    InProcessLlamaCppBackend,
    make_prompt_lookup_draft,
)
from concinno.llm_runtime.llamacpp import LlamaCppBackend, LlamaCppServer
from concinno.llm_runtime.multimodal_router import (
    MultimodalRouter,
    RoutingDecision,
)
from concinno.llm_runtime.tool_parsers import (
    DEFAULT_TOOL_CALL_CAP,
    GemmaToolCallParser,
    ToolCallParser,
    get_parser,
    register_parser,
)

__all__ = [
    "DEFAULT_TOOL_CALL_CAP",
    "ChatMessage",
    "GemmaToolCallParser",
    "InProcessLlamaCppBackend",
    "LLMBackend",
    "LlamaCppBackend",
    "LlamaCppServer",
    "MultimodalRouter",
    "RoutingDecision",
    "ToolCallParser",
    "get_parser",
    "make_prompt_lookup_draft",
    "register_parser",
]
