"""Abstract LLM backend protocol."""

from __future__ import annotations

from typing import Protocol, TypedDict


class ChatMessage(TypedDict):
    role: str
    content: str


class LLMBackend(Protocol):
    """Minimal chat surface shared by every runtime backend."""

    def chat(
        self,
        system: str,
        messages: list[ChatMessage],
        max_tokens: int = 2000,
        temperature: float = 0.3,
    ) -> str:
        """Return assistant content or empty string on failure.

        Implementations MUST NOT raise for transport / provider errors;
        callers rely on empty-string sentinel to drive retry logic upstream
        (see gaia_agent.py _gemma_chat for the canonical pattern).
        """
        ...
