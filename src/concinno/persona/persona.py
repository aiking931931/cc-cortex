"""Persona — high-level facade orchestrating schema + state + RAG + LLM backend.

This is the public entry point most consumers will use. It glues
together the smaller pieces in the persona package and keeps the
backend pluggable so Track 2 (Sancio HTTP endpoint) and Track 3
(local fine-tuned model) can swap implementations without changing
caller code.

Backends:

* :class:`InProcessBackend` — Track 1, ships now. Uses the user's
  installed Anthropic / OpenAI SDK keyed by ``provider``.
* :class:`HTTPBackend` — Track 2 stub. Raises NotImplementedError.
* :class:`LocalModelBackend` — Track 3 stub. Raises NotImplementedError.

The stubs exist so the public API surface is fixed at Track 1 ship
time; Track 2/3 land as drop-in implementations.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from concinno.persona.loader import load_persona_file
from concinno.persona.pinned_memories import PinnedMemoryStore
from concinno.persona.prompt import render_recall_context, render_system_prompt
from concinno.persona.rag import PersonaRAG, RAGHit
from concinno.persona.schema import EmotionalState, PersonaSchema
from concinno.persona.state import (
    PersonaState,
    make_consolidate,
    make_pin,
    make_turn,
    make_unpin,
)

# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


class PersonaBackend(ABC):
    """Abstract chat / consolidation interface.

    All Persona LLM calls funnel through one of these so the upgrade
    path from in-process -> HTTP endpoint -> local model is transparent
    to callers.
    """

    @abstractmethod
    def chat(
        self,
        system_prompt: str,
        history: list[dict[str, str]],
        user: str,
        *,
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ) -> str:
        """Return assistant text. Empty string on failure (never raise)."""

    def consolidate(
        self,
        system_prompt: str,
        history: list[dict[str, str]],
        target_chars: int = 600,
    ) -> str:
        """Default consolidation: ask the chat backend for a summary.

        Subclasses with cheaper / specialised consolidation paths can
        override this. Default implementation routes through chat with
        an instruction prepended.
        """
        instruction = (
            "Summarise the conversation so far for long-term memory."
            f" Keep it under {target_chars} characters."
            " Preserve any pinned facts already in the system prompt."
        )
        return self.chat(system_prompt, history, instruction, max_tokens=512, temperature=0.3)


class InProcessBackend(PersonaBackend):
    """Default backend for Track 1.

    Resolves a provider client lazily so consumers without
    ``anthropic`` / ``openai`` installed can still construct a
    Persona for offline / dry-run use (e.g. CI testing of the
    schema + state machine).
    """

    def __init__(
        self,
        provider: str = "anthropic",
        model: str = "",
        api_key: str | None = None,
    ) -> None:
        self.provider = provider.lower().strip()
        self.model = model or self._default_model(self.provider)
        self.api_key = api_key

    @staticmethod
    def _default_model(provider: str) -> str:
        return {
            "anthropic": "claude-haiku-4-5",
            "openai": "gpt-4o-mini",
            "ollama": "gemma4:latest",
            "echo": "echo",
        }.get(provider, "claude-haiku-4-5")

    def chat(
        self,
        system_prompt: str,
        history: list[dict[str, str]],
        user: str,
        *,
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ) -> str:
        if self.provider == "echo":
            # Deterministic backend for tests + offline smoke runs.
            return f"[echo:{self.model}] {user}"
        if self.provider == "anthropic":
            return self._chat_anthropic(system_prompt, history, user, max_tokens, temperature)
        if self.provider == "openai":
            return self._chat_openai(system_prompt, history, user, max_tokens, temperature)
        # Unknown provider — fail soft (per LLMBackend Protocol contract).
        return ""

    def _chat_anthropic(
        self,
        system_prompt: str,
        history: list[dict[str, str]],
        user: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        try:
            import anthropic  # type: ignore
        except ImportError:
            return ""
        api_key = self.api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            return ""
        try:
            client = anthropic.Anthropic(api_key=api_key)
            messages: list[dict[str, str]] = []
            for h in history:
                role = h.get("role", "user")
                if role not in ("user", "assistant"):
                    continue
                messages.append({"role": role, "content": h.get("content", "")})
            messages.append({"role": "user", "content": user})
            resp = client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system_prompt,
                messages=messages,
            )
            blocks = getattr(resp, "content", []) or []
            for blk in blocks:
                t = getattr(blk, "text", None)
                if t:
                    return str(t)
            return ""
        except Exception:
            return ""

    def _chat_openai(
        self,
        system_prompt: str,
        history: list[dict[str, str]],
        user: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        try:
            import openai  # type: ignore
        except ImportError:
            return ""
        api_key = self.api_key or os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            return ""
        try:
            client = openai.OpenAI(api_key=api_key)
            messages = [{"role": "system", "content": system_prompt}]
            for h in history:
                role = h.get("role", "user")
                if role not in ("user", "assistant", "system"):
                    continue
                messages.append({"role": role, "content": h.get("content", "")})
            messages.append({"role": "user", "content": user})
            resp = client.chat.completions.create(
                model=self.model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            choice = (getattr(resp, "choices", []) or [None])[0]
            if choice is None:
                return ""
            content = getattr(getattr(choice, "message", None), "content", "") or ""
            return str(content)
        except Exception:
            return ""


class HTTPBackend(PersonaBackend):
    """Track 2 stub — Sancio paid endpoint.

    Reserved for a future Concinno release. Construction succeeds so
    consumer code can import the symbol; calling :meth:`chat` raises
    so accidental use surfaces clearly.
    """

    def __init__(self, endpoint: str, api_key: str = "") -> None:
        self.endpoint = endpoint
        self.api_key = api_key

    def chat(
        self,
        system_prompt: str,
        history: list[dict[str, str]],
        user: str,
        *,
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ) -> str:
        raise NotImplementedError(
            "HTTPBackend ships in Track 2 (Sancio paid endpoint)."
            " Use InProcessBackend for now."
        )


class LocalModelBackend(PersonaBackend):
    """Track 3 stub — local fine-tuned model.

    Reserved for a future Concinno release shipping the persona-tuned
    model on Hugging Face. Calling :meth:`chat` raises until then.
    """

    def __init__(self, model_id: str) -> None:
        self.model_id = model_id

    def chat(
        self,
        system_prompt: str,
        history: list[dict[str, str]],
        user: str,
        *,
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ) -> str:
        raise NotImplementedError(
            "LocalModelBackend ships in Track 3 (local fine-tuned model)."
            " Use InProcessBackend for now."
        )


# ---------------------------------------------------------------------------
# Persona facade
# ---------------------------------------------------------------------------


class Persona:
    """High-level wrapper combining schema, state, pins, RAG, and a backend."""

    def __init__(
        self,
        schema: PersonaSchema,
        state: PersonaState | None = None,
        backend: PersonaBackend | None = None,
        rag: PersonaRAG | None = None,
    ) -> None:
        self.schema = schema
        self.state = state if state is not None else PersonaState.empty()
        self.backend: PersonaBackend = backend if backend is not None else InProcessBackend()
        self.rag = rag if rag is not None else PersonaRAG(schema.name)
        self.pins = PinnedMemoryStore(schema.pinned_memories)
        self._replay_pins_from_state()
        self._replay_rag_from_state()

    # ---- alternate constructors ----

    @classmethod
    def load(
        cls,
        persona_path: str | Path,
        state: str | Path | None = None,
        backend: PersonaBackend | None = None,
    ) -> Persona:
        """Load from a persona MD file. Optionally attach an existing state log."""
        schema = load_persona_file(persona_path)
        if state is not None:
            persona_state = PersonaState.load(state)
        else:
            persona_state = PersonaState.empty()
        return cls(schema=schema, state=persona_state, backend=backend)

    # ---- replay helpers ----

    def _replay_pins_from_state(self) -> None:
        for r in self.state.records:
            if r.kind == "pin":
                content = (r.state_delta or {}).get("content")
                reason = (r.state_delta or {}).get("reason")
                if content:
                    self.pins.pin(content, reason=reason)
            elif r.kind == "unpin":
                content = (r.state_delta or {}).get("content")
                if content:
                    self.pins.unpin(content)

    def _replay_rag_from_state(self) -> None:
        # Seed RAG from memory_seed + recorded turns.
        for s in self.schema.memory_seed:
            self.rag.add(s)
        for r in self.state.turns():
            self.rag.add_turn(r.user, r.assistant)

    # ---- chat / state ops ----

    def chat(
        self,
        message: str,
        *,
        recall_top_k: int = 3,
        max_tokens: int = 1024,
        temperature: float = 0.7,
        record: bool = True,
    ) -> str:
        """Send one user message; return assistant reply.

        ``record=True`` also appends a turn record to the state log.
        Pinned memories + RAG hits are folded into the system prompt.
        """
        hits = self.recall(message, top_k=recall_top_k)
        extra = render_recall_context([h.text for h in hits])
        system = render_system_prompt(self.schema, self.pins, extra_context=extra)
        history = [
            {"role": "user", "content": r.user}
            for r in self.state.turns()[-6:]
        ]
        # Interleave assistant turns into the history.
        history = []
        for r in self.state.turns()[-6:]:
            if r.user:
                history.append({"role": "user", "content": r.user})
            if r.assistant:
                history.append({"role": "assistant", "content": r.assistant})
        reply = self.backend.chat(
            system_prompt=system,
            history=history,
            user=message,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        if record:
            self.state.append(make_turn(message, reply))
            self.rag.add_turn(message, reply)
        return reply

    def consolidate(self, turn: tuple[str, str] | None = None) -> str:
        """Append an explicit consolidation marker; optionally record a turn first.

        ``turn`` is ``(user_message, assistant_reply)`` if you want to
        record a turn that bypassed :meth:`chat` (e.g. you used a
        non-Concinno LLM call directly). The consolidation marker is
        what tells future replays "memory before this point may have
        been summarised".
        """
        if turn is not None:
            user_msg, reply = turn
            self.state.append(make_turn(user_msg, reply))
            self.rag.add_turn(user_msg, reply)
        # Soft-consolidate: just emit a marker. Heavy summarisation is
        # left to the consumer / Track 2 server-side path. Pinned
        # memories survive untouched.
        summary = f"checkpoint: {len(self.state.turns())} turns recorded"
        self.state.append(make_consolidate(summary))
        return summary

    # ---- pinned memory ops ----

    def pin_memory(self, content: str, *, reason: str | None = None) -> None:
        self.pins.pin(content, reason=reason)
        self.state.append(make_pin(content, reason))

    def unpin_memory(self, content: str) -> bool:
        removed = self.pins.unpin(content)
        if removed:
            self.state.append(make_unpin(content))
        return removed

    def pinned(self) -> list[Any]:
        return self.pins.all()

    # ---- recall ----

    def recall(self, query: str, top_k: int = 3) -> list[RAGHit]:
        """Return top-k recall results, pinned matches first.

        Pinned memories are appended at the top with a synthetic high
        score so consumers can show them above general RAG hits.
        """
        results: list[RAGHit] = []
        seen: set[str] = set()
        # Pins take priority — synthetic score so they sort to the top.
        for m in self.pins.search(query, top_k=top_k):
            results.append(RAGHit(score=1e6, text=m.content, idx=-1))
            seen.add(m.content)
        for h in self.rag.search(query, top_k=top_k):
            if h.text in seen:
                continue
            results.append(h)
            seen.add(h.text)
        # Truncate to caller's top_k after merging — pinned facts
        # always stay at the head.
        return results[: max(top_k, len(self.pins))]

    # ---- backend swap ----

    def use_endpoint(self, url: str, api_key: str = "") -> None:
        """Swap to Track 2 HTTP backend (raises until Track 2 ships)."""
        self.backend = HTTPBackend(url, api_key)

    def use_local_model(self, model_id: str) -> None:
        """Swap to Track 3 local fine-tuned model (raises until Track 3 ships)."""
        self.backend = LocalModelBackend(model_id)

    # ---- save ----

    def save(self, path: str | Path) -> None:
        """Persist the JSONL state log to ``path``."""
        self.state.attach(path)
        self.state.save_snapshot(path)

    # ---- emotional state helpers ----

    def decay_emotion(self) -> EmotionalState:
        """Apply one decay step to the emotional state. Returns the new state."""
        em = self.schema.emotional_state
        new_intensity = max(0.0, min(1.0, em.intensity * em.decay_rate))
        self.schema.emotional_state = EmotionalState(
            default=em.default,
            intensity=new_intensity,
            decay_rate=em.decay_rate,
        )
        return self.schema.emotional_state


__all__ = [
    "HTTPBackend",
    "InProcessBackend",
    "LocalModelBackend",
    "Persona",
    "PersonaBackend",
]
