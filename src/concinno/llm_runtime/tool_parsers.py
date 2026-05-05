"""Family-specific tool-call recovery for in-process llama.cpp runs.

Some model families (Gemma 4, Qwen 2.5-Coder) emit ``tool_calls`` as
raw special-token text inside the assistant ``content`` stream rather
than populating OpenAI ``message.tool_calls``. The HTTP layer
(``python -m llama_cpp.server``) normalises the stream into OpenAI
shape before returning, but in-process
``llama_cpp.Llama.create_chat_completion`` does NOT — llama-cpp-python
0.3.x ships no chat handler for these families.

This module pulls the family-specific recovery logic that used to live
inside :class:`concinno.llm_runtime.in_process.InProcessLlamaCppBackend`
into a :class:`ToolCallParser` protocol plus a registry keyed on
``chat_format``. Adding a new family = one concrete parser + one
registry entry; the agent loop itself does not change.

Public API:
    ToolCallParser        - Protocol every parser implements
    GemmaToolCallParser   - Gemma 4 text-token recovery (current ship)
    get_parser(fmt)       - Dispatch by chat_format / None
    register_parser(key)  - Extension hook for sub-packages
"""

from __future__ import annotations

import json
import re
from typing import Any, Protocol, runtime_checkable

# Hard cap on distinct tool calls recovered from a single assistant
# response. Gemma 4 thinking-mode self-narrates — the model echoes
# its own past tool calls inside ``<|channel>thought|>`` blocks, so
# a naïve "extract every match" pass on one turn can surface dozens
# of identical ``python_exec`` calls. Observed 2026-04-23 tiny smoke:
# 256 identical ``python_exec{"code":"12 * 8"}`` calls in a single
# response blew the 8K ctx via tool_response accumulation. Cap keeps
# the recovery bounded per turn; dedup (below) removes the echo
# duplicates even when they stay under the cap.
DEFAULT_TOOL_CALL_CAP = 3


@runtime_checkable
class ToolCallParser(Protocol):
    """Family-specific recovery of tool_calls from text content.

    Implementations recover OpenAI-shaped ``tool_calls`` dicts from
    model-family-specific text markers (Gemma 4 ``<|tool_call>`` tokens,
    Qwen 2.5-Coder bare-JSON, ...). They are stateless — the agent
    loop constructs and discards parsers per response.

    Attributes:
        family: Short identifier matching the chat_format prefix this
            parser handles (e.g. ``"gemma"`` matches ``"gemma"`` /
            ``"gemma-3"`` / ``"gemma-4-it"``).
    """

    family: str

    def should_attempt(
        self,
        *,
        content: str,
        tools_given: list[dict[str, Any]] | None,
        native_tool_calls: list[Any] | None,
    ) -> bool:
        """True if the parser should run on this response.

        Standard gate: caller asked for tools AND the underlying
        handler did not already populate ``tool_calls`` (avoids
        clobbering a future native handler's output).
        """

    def parse(
        self,
        content: str,
        *,
        max_calls: int = DEFAULT_TOOL_CALL_CAP,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Return ``(tool_calls, cleaned_content)`` extracted from content.

        Args:
            content: Raw assistant ``message.content`` text.
            max_calls: Hard cap on distinct tool_calls returned.

        Returns:
            Two-tuple of:
              * list of OpenAI-shaped ``{id, type, function:
                {name, arguments}}`` dicts (possibly empty),
              * content with recovery markers stripped (``None`` if
                stripping consumed the entire string).
        """


# ── Gemma 4 thinking-mode recovery ─────────────────────────────────
#
# Pattern observed on gemma-4-31B-it Q4_K_M (pod v0ggvz5dcsu9gu,
# 2026-04-23):
#
#     <|tool_call>call:python_exec{code:<|"|>round(x, 3)<|"|>}<tool_call|>
#
# * Open / close markers are asymmetric: ``<|tool_call>`` (pipe
#   inside LEFT angle) ... ``<tool_call|>`` (pipe inside RIGHT angle).
# * Argument values are wrapped in the Gemma-tokenizer-specific quote
#   ``<|"|>`` rather than standard JSON quotes.
# * Multiple tool calls can appear in one content block; we extract
#   every match in document order so the agent loop sees them in the
#   same sequence the model intended, deduplicated.
_GEMMA_TOOL_CALL_RE = re.compile(
    r"<\|tool_call\>call:(\w+)\{(.*?)\}<tool_call\|>",
    re.DOTALL,
)
_GEMMA_ARG_RE = re.compile(
    r"(\w+)\s*:\s*<\|\"\|>(.*?)<\|\"\|>",
    re.DOTALL,
)


class GemmaToolCallParser:
    """Recover Gemma 4 ``<|tool_call>...<tool_call|>`` blocks from text.

    Dedup + cap policy (Gemma 4 thinking-mode echo defence)
    -------------------------------------------------------
    Two orthogonal guards, both required:

    * **Dedup by (name, arguments) signature** — Gemma 4 emits the
      same ``<|tool_call>`` block multiple times inside one response
      when the ``<|channel>thought|>`` narrates a past call. Lifting
      every match into ``message.tool_calls`` tells the agent_loop to
      execute the tool N times and stuff N identical tool_responses
      back into the history; the next LLM round then blows context.
      Canonicalising on the signature keeps each distinct call once.
    * **Hard cap ``max_calls``** — a pathological thinking-mode loop
      can emit dozens of *distinct* tool calls (different args but
      all noise). Cap bounds worst case so a single response can't
      exceed the agent_loop's sane iteration budget.
    """

    family: str = "gemma"

    def should_attempt(
        self,
        *,
        content: str,
        tools_given: list[dict[str, Any]] | None,
        native_tool_calls: list[Any] | None,
    ) -> bool:
        # Match legacy in_process.py gate: run when tools were asked
        # for AND native tool_calls weren't already populated.
        return bool(tools_given) and not native_tool_calls

    def parse(
        self,
        content: str,
        *,
        max_calls: int = DEFAULT_TOOL_CALL_CAP,
    ) -> tuple[list[dict[str, Any]], str | None]:
        out: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for m in _GEMMA_TOOL_CALL_RE.finditer(content):
            name = m.group(1)
            body = m.group(2)
            args = {
                k: v
                for k, v in _GEMMA_ARG_RE.findall(body)
            }
            arg_json = json.dumps(args, ensure_ascii=False, sort_keys=True)
            signature = (name, arg_json)
            if signature in seen:
                continue
            seen.add(signature)
            out.append({
                "id": f"call_gemma_{len(out)}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(args, ensure_ascii=False),
                },
            })
            if len(out) >= max_calls:
                break
        cleaned = _GEMMA_TOOL_CALL_RE.sub("", content).strip() or None
        return out, cleaned


# ── Registry ───────────────────────────────────────────────────────
#
# ``_REGISTRY`` maps family-key strings to parser **classes**. Lookup
# is ``startswith``-based so ``chat_format="gemma-4-it"`` dispatches
# to the ``"gemma"`` parser without an exact-match entry per variant.
#
# Entries are ordered: the first match wins. Keep longer / more
# specific keys before shorter prefixes if they ever collide.

_REGISTRY: dict[str, type[ToolCallParser]] = {
    "gemma": GemmaToolCallParser,
}


def register_parser(family: str, parser_cls: type[ToolCallParser]) -> None:
    """Register an additional parser for extension sub-packages.

    ``concinno-skills-*`` sub-packages (or downstream deployers) that
    add a new model family call this once at import time to wire a
    custom parser without touching Concinno core.

    Args:
        family: Family-key string (e.g. ``"qwen"``, ``"llama-3"``).
            Lookup is case-insensitive; store canonicalised lowercase.
        parser_cls: Class implementing :class:`ToolCallParser`.
    """
    _REGISTRY[family.lower()] = parser_cls


def get_parser(chat_format: str | None) -> ToolCallParser | None:
    """Return a parser instance for the given ``chat_format``.

    Dispatch rules (top-down):

    1. ``chat_format is None`` → :class:`GemmaToolCallParser`. The
       legacy ``InProcessLlamaCppBackend`` ran Gemma recovery
       unconditionally — preserve that for callers who never set
       ``chat_format`` and happen to load a Gemma GGUF (the pod
       default).
    2. ``chat_format`` starts with a registered family key
       (case-insensitive) → that parser.
    3. Otherwise → ``None``. Caller keeps the handler's native
       ``tool_calls`` / ``content`` unchanged.

    Add new families by calling :func:`register_parser`. A format
    that is explicitly native (Llama 3 / Mistral / functionary /
    chatml with a proper handler) simply stays out of the registry
    and falls through to ``None``.
    """
    if chat_format is None:
        return GemmaToolCallParser()
    lowered = chat_format.lower()
    for key, cls in _REGISTRY.items():
        if lowered.startswith(key):
            return cls()
    return None


__all__ = [
    "DEFAULT_TOOL_CALL_CAP",
    "GemmaToolCallParser",
    "ToolCallParser",
    "get_parser",
    "register_parser",
]
