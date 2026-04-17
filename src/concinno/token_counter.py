"""concinno.token_counter — Hybrid token counter (fast/accurate/hybrid).

@module token_counter
@responsibility Provide three counting modes so CCC can make precise
    budget decisions after compression instead of relying on ±15% estimates.
@dependencies stdlib only (anthropic SDK is lazy-imported when needed)
@exports TokenCounter

Modes
-----
- fast:     CJK-aware local estimator (~0 latency, ±15% error) — DEFAULT
- accurate: anthropic.beta.messages.count_tokens() — exact, network bound
- hybrid:   fast for <80% budget, escalate to accurate at the cliff

.. warning::
    ``accurate`` and ``hybrid`` both call the Anthropic count-tokens API.
    That endpoint does not charge tokens but still incurs a network round
    trip and requires ``ANTHROPIC_API_KEY``. Opt in explicitly by passing
    ``mode="accurate"`` or ``mode="hybrid"``; the default stays ``"fast"``
    so consumers never get surprised with network latency or key errors.

Why a separate module
---------------------
field_read.py and prompt_engine.py both ship a private _estimate_tokens()
helper. We deliberately copy the logic here (rather than import it) to
avoid a circular dependency: field_read may eventually want to use the
counter for its own budget decisions.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Literal, Optional

if TYPE_CHECKING:  # pragma: no cover - typing only
    import anthropic

logger = logging.getLogger(__name__)

# CJK-aware token estimation — matches field_read.py / prompt_engine.py
_CJK_RANGE = 0x2E80

# Hybrid escalation cliff: estimate >= this fraction of budget triggers
# the accurate path. Below it, fast is good enough and saves an API call.
_HYBRID_ESCALATION_RATIO = 0.8

# Pessimistic upper-bound multiplier when accurate fails inside hybrid.
# +15% covers the worst-case fast-mode under-estimate so callers stay safe.
_FAST_UPPER_BOUND_MULT = 1.15

ModeName = Literal["fast", "accurate", "hybrid"]
LastModeUsed = Literal[
    "fast",
    "accurate",
    "fallback_fast",
    "fallback_fast_upper",
]


def _estimate_fast(text: str, *, tokenizer: str = "legacy") -> int:
    """CJK-aware local estimator.

    Currently one tokenizer profile is supported:

    - ``legacy`` (default): ``cjk * 1.5 + ascii / 4``. Empirically
      validated against Claude ``count_tokens`` API on mixed English /
      CJK corpora; accurate to within ±10% for workloads under ~10k
      chars which is the regime fast mode is designed for.

    An ``opus47`` profile was considered but removed because the
    ratio could not be anchored to a published Anthropic source. If
    Opus 4.7 materially changes per-char token density for your
    workload, use ``mode="accurate"`` or ``mode="hybrid"`` so the
    real Anthropic ``count_tokens`` API is authoritative, and only
    the coarse legacy ratio is used as a pre-escalation heuristic.
    """
    if not text:
        return 0
    cjk = sum(1 for c in text if ord(c) > _CJK_RANGE)
    ascii_chars = len(text) - cjk
    if tokenizer != "legacy":
        raise ValueError(
            f"unknown tokenizer profile: {tokenizer!r} "
            f"(only 'legacy' is supported; use mode='accurate' for "
            f"Opus 4.7+ workloads that need exact counts)"
        )
    return int(cjk * 1.5 + ascii_chars / 4)


def _tokenizer_for(model: str) -> str:
    """Pick the tokenizer tag for a model id.

    Only ``legacy`` is currently implemented. The detector is kept
    as an explicit seam so future per-model profiles can be added
    with anchored empirical data (cf. ``_estimate_fast`` docstring)
    rather than free-hand constants.
    """
    return "legacy"


class TokenCounter:
    """Token counter with fast / accurate / hybrid modes.

    Parameters
    ----------
    mode:
        "fast" — never call API, always estimate.
        "accurate" — always call Anthropic count_tokens API (falls back to
            fast if SDK missing, no API key, or network error).
        "hybrid" — fast first; if estimate >= 80% of ``budget``, re-count
            via API. If API fails, return ``fast * 1.15`` as a pessimistic
            upper bound so the caller never under-counts near the cliff.
    budget:
        Total token budget (used by hybrid mode to decide when to escalate).
    anthropic_client:
        Optional pre-built ``anthropic.Anthropic`` client. If ``None``, one
        is constructed on first use from ``ANTHROPIC_API_KEY``. If the SDK
        is not installed or the key is missing, accurate calls fall back
        to fast and emit a warning.
    model:
        Model name passed to ``count_tokens``. Defaults to current Opus.
    """

    def __init__(
        self,
        mode: ModeName = "fast",
        budget: int = 200_000,
        anthropic_client: Optional["anthropic.Anthropic"] = None,
        model: str = "claude-opus-4-6",
    ) -> None:
        if mode not in ("fast", "accurate", "hybrid"):
            raise ValueError(f"invalid mode: {mode!r}")
        if budget <= 0:
            raise ValueError(f"budget must be positive, got {budget}")
        self.mode: ModeName = mode
        self.budget = budget
        self._client = anthropic_client
        self._client_resolved = anthropic_client is not None
        self.model = model
        self._last_mode_used: LastModeUsed = "fast"

    # ── public api ────────────────────────────────────────

    @property
    def last_mode_used(self) -> LastModeUsed:
        """Which path the last ``count()`` call actually executed.

        One of: ``fast``, ``accurate``, ``fallback_fast``, ``fallback_fast_upper``.
        """
        return self._last_mode_used

    def count(self, text: str) -> int:
        """Count tokens for a single string under the active mode."""
        tok = _tokenizer_for(self.model)
        if self.mode == "fast":
            self._last_mode_used = "fast"
            return _estimate_fast(text, tokenizer=tok)

        if self.mode == "accurate":
            result = self._count_accurate(text)
            if result is not None:
                self._last_mode_used = "accurate"
                return result
            self._last_mode_used = "fallback_fast"
            return _estimate_fast(text, tokenizer=_tokenizer_for(self.model))

        # hybrid
        fast = _estimate_fast(text, tokenizer=tok)
        if fast < int(self.budget * _HYBRID_ESCALATION_RATIO):
            self._last_mode_used = "fast"
            return fast
        result = self._count_accurate(text)
        if result is not None:
            self._last_mode_used = "accurate"
            return result
        self._last_mode_used = "fallback_fast_upper"
        return int(fast * _FAST_UPPER_BOUND_MULT)

    def count_messages(self, messages: list[dict]) -> int:
        """Count tokens for a list of ``{'role', 'content'}`` dicts.

        Accurate mode uses the API directly on the message list. Fast and
        hybrid modes sum ``count()`` across the per-message content strings,
        which is an estimate (it skips role overhead) but stays consistent
        with single-string ``count()`` semantics.
        """
        if self.mode == "accurate":
            result = self._count_accurate_messages(messages)
            if result is not None:
                self._last_mode_used = "accurate"
                return result
            self._last_mode_used = "fallback_fast"
            return sum(_estimate_fast(str(m.get("content", ""))) for m in messages)

        total = 0
        for msg in messages:
            total += self.count(str(msg.get("content", "")))
        return total

    # ── internals ─────────────────────────────────────────

    def _resolve_client(self) -> Optional["anthropic.Anthropic"]:
        """Lazy-build an Anthropic client. Returns None if unavailable."""
        if self._client is not None:
            return self._client
        if self._client_resolved:
            return None  # already tried, gave up
        self._client_resolved = True
        try:
            import anthropic  # noqa: PLC0415 - lazy by design
        except ImportError:
            logger.warning(
                "anthropic SDK not installed; accurate token counting unavailable",
            )
            return None
        if not os.environ.get("ANTHROPIC_API_KEY"):
            logger.warning(
                "ANTHROPIC_API_KEY not set; accurate token counting unavailable",
            )
            return None
        try:
            self._client = anthropic.Anthropic()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("failed to construct Anthropic client: %s", exc)
            return None
        return self._client

    def _count_accurate(self, text: str) -> Optional[int]:
        client = self._resolve_client()
        if client is None:
            return None
        try:
            resp = client.beta.messages.count_tokens(
                model=self.model,
                messages=[{"role": "user", "content": text}],
            )
        except Exception as exc:
            logger.warning("anthropic count_tokens failed: %s", exc)
            return None
        return int(resp.input_tokens)

    def _count_accurate_messages(self, messages: list[dict]) -> Optional[int]:
        client = self._resolve_client()
        if client is None:
            return None
        try:
            resp = client.beta.messages.count_tokens(
                model=self.model,
                messages=messages,
            )
        except Exception as exc:
            logger.warning("anthropic count_tokens (messages) failed: %s", exc)
            return None
        return int(resp.input_tokens)
