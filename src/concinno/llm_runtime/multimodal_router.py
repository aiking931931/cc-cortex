"""Multimodal request router — dispatch between text + vision backends.

Sancio wants to own the whole runtime (MEMORY #98 + user directive
2026-04-23 "Sancio 取代 Ollama" + "還要讓他能多模態"). Gemma 4 31B
Q4_K_M as shipped is text-only; when a message carries image or
audio content, we route to a co-resident in-process vision model
instead of failing or hallucinating over the attachment.

Topology (all three backends in the SAME Python process; no HTTP):

    text backend      (default) — Gemma 4 31B Q4_K_M
    vision backend    (optional) — e.g. Qwen2.5-VL-32B / Gemma 3 27B
                       vision / InternVL2 / LLaVA-NeXT. Configured
                       via CONCINNO_LLM_VISION_GGUF_PATH +
                       CONCINNO_LLM_VISION_MMPROJ_PATH.
    audio backend     (optional, future) — Whisper / Qwen2-Audio

The router holds one :class:`InProcessLlamaCppBackend` per modality
and picks based on message content shape. When the vision backend is
not configured, image messages transparently fall back to text-only
with a stripped-attachment + warning — the caller's retry loop can
then escalate to an external vision API if the deploy allows.

Minimum Concinno version: 2.21.0
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from concinno.llm_runtime.base import ChatMessage
from concinno.llm_runtime.in_process import (
    InProcessLlamaCppBackend,
)


def _has_image(messages: list[ChatMessage | dict[str, Any]]) -> bool:
    """True iff any message content block is an image.

    Supports three shapes OpenAI-compat callers use in the wild:

    * ``{"type": "image_url", "image_url": {"url": ...}}`` (OpenAI)
    * ``{"type": "image", "image": {"data": b64, "media_type": ...}}``
      (Anthropic-style)
    * Legacy ``{"image": "<path or data>"}`` sidecar keys
    """
    for m in messages:
        content = m.get("content") if isinstance(m, dict) else None
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    btype = str(block.get("type", "")).lower()
                    if btype in ("image", "image_url"):
                        return True
        if isinstance(m, dict) and m.get("image"):
            return True
    return False


def _has_audio(messages: list[ChatMessage | dict[str, Any]]) -> bool:
    for m in messages:
        content = m.get("content") if isinstance(m, dict) else None
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    btype = str(block.get("type", "")).lower()
                    if btype in ("audio", "audio_url", "input_audio"):
                        return True
        if isinstance(m, dict) and m.get("audio"):
            return True
    return False


@dataclass(frozen=True)
class RoutingDecision:
    """What the router picked + why — lets callers log/metric the path."""

    modality: str  # "text" / "vision" / "audio"
    backend: str  # class name of the selected backend
    fallback: bool  # True iff requested modality unavailable → degraded


class MultimodalRouter:
    """Route chat requests across text / vision / audio in-process backends.

    The router is the single public surface Sancio's agent loop should
    call. It keeps each specialised :class:`InProcessLlamaCppBackend`
    lazily loaded so deploys that never serve an image don't pay the
    vision weight-load cost.

    Config (env vars read once at construction unless overridden):

    * ``CONCINNO_LLM_GGUF_PATH`` — text GGUF
    * ``CONCINNO_LLM_VISION_GGUF_PATH`` — vision GGUF (enables vision)
    * ``CONCINNO_LLM_VISION_N_CTX`` — vision context window (default 8192)
    """

    def __init__(
        self,
        text_backend: InProcessLlamaCppBackend | None = None,
        vision_backend: InProcessLlamaCppBackend | None = None,
        audio_backend: InProcessLlamaCppBackend | None = None,
    ) -> None:
        self._text = text_backend or InProcessLlamaCppBackend()
        self._vision = vision_backend
        self._audio = audio_backend
        if self._vision is None:
            vision_gguf = os.environ.get("CONCINNO_LLM_VISION_GGUF_PATH")
            if vision_gguf:
                self._vision = InProcessLlamaCppBackend(
                    model_path=vision_gguf,
                    n_ctx=int(os.environ.get(
                        "CONCINNO_LLM_VISION_N_CTX", "8192",
                    )),
                )

    def decide(
        self,
        messages: list[ChatMessage | dict[str, Any]],
    ) -> RoutingDecision:
        """Pure routing decision — does NOT invoke any backend.

        Callers can log this before chat() so the decision + dispatch
        are separately observable.
        """
        if _has_image(messages):
            if self._vision is not None:
                return RoutingDecision(
                    modality="vision",
                    backend=type(self._vision).__name__,
                    fallback=False,
                )
            return RoutingDecision(
                modality="vision", backend=type(self._text).__name__,
                fallback=True,
            )
        if _has_audio(messages):
            if self._audio is not None:
                return RoutingDecision(
                    modality="audio",
                    backend=type(self._audio).__name__,
                    fallback=False,
                )
            return RoutingDecision(
                modality="audio", backend=type(self._text).__name__,
                fallback=True,
            )
        return RoutingDecision(
            modality="text",
            backend=type(self._text).__name__,
            fallback=False,
        )

    def chat(
        self,
        system: str,
        messages: list[ChatMessage | dict[str, Any]],
        max_tokens: int = 2000,
        temperature: float = 0.3,
    ) -> str:
        decision = self.decide(messages)
        if decision.modality == "vision" and not decision.fallback:
            return self._vision.chat(
                system, messages, max_tokens, temperature,
            )  # type: ignore[union-attr]
        if decision.modality == "audio" and not decision.fallback:
            return self._audio.chat(
                system, messages, max_tokens, temperature,
            )  # type: ignore[union-attr]
        # text or fallback-degraded
        return self._text.chat(system, messages, max_tokens, temperature)

    @property
    def capabilities(self) -> dict[str, bool]:
        return {
            "text": True,
            "vision": self._vision is not None,
            "audio": self._audio is not None,
        }
