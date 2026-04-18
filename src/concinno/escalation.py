"""concinno.escalation — Multi-tier LLM gateway with auto-fallback.

@module escalation
@responsibility Route a chat request through a tier chain (cheapest first),
    auto-fall-through on transient/permanent failures, wrap each tier with a
    persistent circuit breaker, and return the first successful response.
@dependencies concinno.core.state_store
@exports EscalationTier, DEFAULT_CHAIN, TierResult, EscalationResult,
    EscalationExhausted, CircuitOpen, LLMEscalator, escalate

Design:
  Caller hands us ``messages`` (OpenAI-style list of dicts). We walk the
  configured chain (default gemma → haiku → sonnet → opus), call each tier,
  and return as soon as one succeeds. Intermediate failures are swallowed
  (recorded in ``attempts``). Only if every tier fails do we raise
  ``EscalationExhausted``.

  Per-tier circuit breaker state is persisted via ``StateStore.read_flat`` /
  ``write_flat`` under namespace ``escalation`` / file ``breakers.json``. A
  fresh ``LLMEscalator`` instance with the same ``cache_dir`` picks up the
  saved breaker state — this is how a long-dead tier stays dead for
  ``circuit_cooldown_s`` without re-probing on every call.

  httpx and anthropic are *lazy-imported* inside the methods that use them.
  ``import concinno.escalation`` stays cheap and does not hard-depend on
  either library at module load time — callers who only touch the Gemma
  tier need not have ``anthropic`` installed, and vice versa.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Sequence, cast

from concinno.core.state_store import StateStore

# ── Public constants ────────────────────────────────────────

EscalationTier = Literal["gemma", "haiku", "sonnet", "opus"]
DEFAULT_CHAIN: tuple[EscalationTier, ...] = ("gemma", "haiku", "sonnet", "opus")

_VALID_TIERS: frozenset[str] = frozenset(("gemma", "haiku", "sonnet", "opus"))
_CLAUDE_TIERS: frozenset[str] = frozenset(("haiku", "sonnet", "opus"))

_NS = "escalation"
_BREAKER_FILE = "breakers.json"

# Circuit breaker states
_CB_CLOSED = "closed"
_CB_OPEN = "open"
_CB_HALF_OPEN = "half_open"


# ── Dataclasses ─────────────────────────────────────────────


@dataclass
class TierResult:
    """Outcome of a single tier attempt (success or failure)."""

    tier: EscalationTier
    text: str
    tokens_in: int
    tokens_out: int
    latency_ms: int
    retries: int


@dataclass
class EscalationResult:
    """Final result returned by :meth:`LLMEscalator.escalate`."""

    final: TierResult
    attempts: list[TierResult] = field(default_factory=list)
    chain: tuple[EscalationTier, ...] = DEFAULT_CHAIN


# ── Exceptions ──────────────────────────────────────────────


class EscalationExhausted(RuntimeError):
    """Raised when every tier in the chain failed."""

    def __init__(self, failures: list[tuple[EscalationTier, str]]) -> None:
        self.failures = failures
        super().__init__(f"All tiers failed: {failures}")


class CircuitOpen(RuntimeError):
    """Raised internally when a tier's circuit breaker is open."""


# ── Helpers (lazy error classification) ─────────────────────


def _httpx_transient(exc: BaseException) -> bool:
    """Return True if an httpx exception should be retried once."""
    try:
        import httpx  # local, lazy
    except ImportError:
        return False
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status == 429 or 500 <= status < 600
    return False


def _anthropic_transient(exc: BaseException) -> bool:
    """Return True if an anthropic SDK exception should be retried once."""
    try:
        import anthropic  # local, lazy
    except ImportError:
        return False
    if isinstance(exc, (anthropic.APITimeoutError, anthropic.APIConnectionError)):
        return True
    # RateLimitError / InternalServerError / APIStatusError with 5xx-ish
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and (status == 429 or 500 <= status < 600):
        return True
    return False


def _now_ms() -> int:
    return int(time.perf_counter_ns() // 1_000_000)


# ── Main class ──────────────────────────────────────────────


class LLMEscalator:
    """Multi-tier LLM gateway with auto-fallback and circuit breaker.

    Args:
        chain: Ordered tuple of tiers to try. First success wins.
        cache_dir: Directory for persistent circuit-breaker state. If
            ``None`` a default under ``~/.concinno/escalation`` is used.
        http_client: Optional pre-built ``httpx.Client``. Tests pass a
            ``MagicMock`` here to avoid network I/O. When ``None`` a new
            client is built lazily inside ``_call_gemma``.
        anthropic_client: Optional pre-built ``anthropic.Anthropic``
            client. Same rationale as ``http_client``.
        max_retries_per_tier: Max retries on transient errors (1 retry
            means 2 total attempts).
        per_tier_timeout_s: Soft per-tier timeout in seconds.
        circuit_threshold: Consecutive failures that flip CLOSED → OPEN.
        circuit_cooldown_s: OPEN → HALF_OPEN cooldown in seconds.
    """

    def __init__(
        self,
        chain: Sequence[EscalationTier] = DEFAULT_CHAIN,
        *,
        cache_dir: str | None = None,
        http_client: Any = None,
        anthropic_client: Any = None,
        max_retries_per_tier: int = 1,
        per_tier_timeout_s: float = 60.0,
        circuit_threshold: int = 5,
        circuit_cooldown_s: float = 120.0,
    ) -> None:
        for t in chain:
            if t not in _VALID_TIERS:
                raise ValueError(f"Unknown tier in chain: {t!r}")
        self._chain: tuple[EscalationTier, ...] = tuple(chain)
        self._cache_dir = cache_dir or os.path.join(
            os.path.expanduser("~"), ".concinno", "escalation"
        )
        self._store = StateStore(self._cache_dir)
        self._http_client = http_client
        self._anthropic_client = anthropic_client
        self._max_retries = max(0, int(max_retries_per_tier))
        self._timeout_s = float(per_tier_timeout_s)
        self._circuit_threshold = max(1, int(circuit_threshold))
        self._circuit_cooldown_s = float(circuit_cooldown_s)
        self._run_stats: dict[str, dict[str, int]] = {
            t: {"calls": 0, "successes": 0, "failures": 0} for t in _VALID_TIERS
        }

    # ── Circuit breaker ────────────────────────────────────

    def _load_breakers(self) -> dict[str, dict[str, Any]]:
        raw = self._store.read_flat(_NS, _BREAKER_FILE, default={})
        if not isinstance(raw, dict):
            return {}
        return cast(dict[str, dict[str, Any]], raw)

    def _save_breakers(self, data: dict[str, dict[str, Any]]) -> None:
        self._store.write_flat(_NS, _BREAKER_FILE, data)

    def _breaker_state(self, tier: str) -> str:
        data = self._load_breakers()
        entry = data.get(tier, {})
        state = entry.get("state", _CB_CLOSED)
        if state == _CB_OPEN:
            opened_at = float(entry.get("opened_at", 0.0))
            if time.time() - opened_at >= self._circuit_cooldown_s:
                # Transition to half-open on next access
                entry["state"] = _CB_HALF_OPEN
                data[tier] = entry
                self._save_breakers(data)
                return _CB_HALF_OPEN
        return str(state)

    def _breaker_record_failure(self, tier: str) -> None:
        data = self._load_breakers()
        entry = data.get(tier, {})
        if not isinstance(entry, dict):
            entry = {}
        prev_state = entry.get("state", _CB_CLOSED)
        if prev_state == _CB_HALF_OPEN:
            # Probe failed → back to OPEN with fresh cooldown
            entry["state"] = _CB_OPEN
            entry["opened_at"] = time.time()
            entry["consecutive_failures"] = (
                int(entry.get("consecutive_failures", 0)) + 1
            )
        else:
            fails = int(entry.get("consecutive_failures", 0)) + 1
            entry["consecutive_failures"] = fails
            if fails >= self._circuit_threshold:
                entry["state"] = _CB_OPEN
                entry["opened_at"] = time.time()
            else:
                entry["state"] = _CB_CLOSED
        data[tier] = entry
        self._save_breakers(data)

    def _breaker_record_success(self, tier: str) -> None:
        data = self._load_breakers()
        entry = data.get(tier, {})
        if not isinstance(entry, dict):
            entry = {}
        entry["state"] = _CB_CLOSED
        entry["consecutive_failures"] = 0
        entry["opened_at"] = 0.0
        data[tier] = entry
        self._save_breakers(data)

    # ── Public API ─────────────────────────────────────────

    def escalate(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 4096,
        temperature: float = 0.3,
        force_tier: EscalationTier | None = None,
        stop_at: EscalationTier | None = None,
    ) -> EscalationResult:
        """Run ``messages`` through the tier chain, return first success.

        Args:
            messages: OpenAI-style chat messages (``[{role, content}, ...]``).
            max_tokens: Max output tokens per tier call.
            temperature: Sampling temperature.
            force_tier: If set, only this tier is tried.
            stop_at: Truncate the chain after this tier (inclusive).

        Returns:
            :class:`EscalationResult` with ``final`` = successful tier.

        Raises:
            ValueError: ``messages`` empty, or unknown ``force_tier`` /
                ``stop_at``.
            EscalationExhausted: every tier failed.
        """
        if not messages:
            raise ValueError("messages cannot be empty")

        if force_tier is not None:
            if force_tier not in _VALID_TIERS:
                raise ValueError(f"Unknown force_tier: {force_tier!r}")
            chain: tuple[EscalationTier, ...] = (force_tier,)
        else:
            chain = self._chain
            if stop_at is not None:
                if stop_at not in _VALID_TIERS:
                    raise ValueError(f"Unknown stop_at: {stop_at!r}")
                if stop_at in chain:
                    idx = chain.index(stop_at)
                    chain = chain[: idx + 1]

        attempts: list[TierResult] = []
        failures: list[tuple[EscalationTier, str]] = []

        for tier in chain:
            # Skip Claude tiers if API key is not configured
            if tier in _CLAUDE_TIERS and not os.environ.get("ANTHROPIC_API_KEY"):
                failures.append((tier, "ANTHROPIC_API_KEY not set"))
                continue

            # Check circuit breaker
            cb_state = self._breaker_state(tier)
            if cb_state == _CB_OPEN:
                failures.append((tier, "circuit_open"))
                continue

            try:
                result = self._call_tier(
                    tier,
                    messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
            except Exception as exc:
                self._run_stats[tier]["calls"] += 1
                self._run_stats[tier]["failures"] += 1
                self._breaker_record_failure(tier)
                failed = TierResult(
                    tier=tier,
                    text="",
                    tokens_in=0,
                    tokens_out=0,
                    latency_ms=0,
                    retries=self._max_retries,
                )
                attempts.append(failed)
                failures.append((tier, f"{type(exc).__name__}: {exc}"))
                continue

            self._run_stats[tier]["calls"] += 1
            self._run_stats[tier]["successes"] += 1
            self._breaker_record_success(tier)
            attempts.append(result)
            return EscalationResult(final=result, attempts=attempts, chain=chain)

        raise EscalationExhausted(failures)

    def stats(self) -> dict[str, dict[str, Any]]:
        """Return per-tier counters plus circuit-breaker state.

        The returned mapping holds ``{calls, successes, failures}`` as
        integers and ``circuit_state`` as a string
        (``closed`` / ``open`` / ``half_open``).
        """
        out: dict[str, dict[str, Any]] = {}
        breakers = self._load_breakers()
        for tier in _VALID_TIERS:
            run = self._run_stats.get(
                tier, {"calls": 0, "successes": 0, "failures": 0}
            )
            entry = breakers.get(tier, {}) if isinstance(breakers, dict) else {}
            cb_state = (
                entry.get("state", _CB_CLOSED)
                if isinstance(entry, dict)
                else _CB_CLOSED
            )
            out[tier] = {
                "calls": int(run.get("calls", 0)),
                "successes": int(run.get("successes", 0)),
                "failures": int(run.get("failures", 0)),
                "circuit_state": str(cb_state),
            }
        return out

    # ── Tier dispatch ──────────────────────────────────────

    def _call_tier(
        self,
        tier: EscalationTier,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        temperature: float,
    ) -> TierResult:
        retries = 0
        last_exc: BaseException | None = None
        for attempt in range(self._max_retries + 1):
            try:
                if tier == "gemma":
                    return self._call_gemma(
                        messages,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        retries=retries,
                    )
                model_id = _anthropic_model_for(tier)
                return self._call_anthropic(
                    tier,
                    model_id,
                    messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    retries=retries,
                )
            except Exception as exc:
                last_exc = exc
                transient = (
                    _httpx_transient(exc)
                    if tier == "gemma"
                    else _anthropic_transient(exc)
                )
                if not transient or attempt >= self._max_retries:
                    raise
                retries += 1
                continue
        # Unreachable: loop either returns or raises.
        assert last_exc is not None
        raise last_exc

    def _call_gemma(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        temperature: float,
        retries: int,
    ) -> TierResult:
        import httpx  # lazy

        base_url = os.environ.get(
            "CONCINNO_GEMMA_URL", "http://localhost:8400/v1"
        )
        api_key = os.environ.get("CONCINNO_GEMMA_KEY", "")
        model = os.environ.get("CONCINNO_GEMMA_MODEL", "gemma-4-27b")
        url = base_url.rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        client = self._http_client or httpx.Client(timeout=self._timeout_s)
        t0 = _now_ms()
        resp = client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        latency = _now_ms() - t0
        body = resp.json()

        choices = body.get("choices", [])
        text = ""
        if choices:
            msg = choices[0].get("message", {}) or {}
            text = (msg.get("content") or "").strip()
            if not text:
                # cybergym quirk: reasoning-only response
                text = (msg.get("reasoning") or "").strip()

        usage = body.get("usage", {}) or {}
        tokens_in = int(usage.get("prompt_tokens", 0) or 0)
        tokens_out = int(usage.get("completion_tokens", 0) or 0)
        if tokens_in == 0:
            tokens_in = sum(len(m.get("content", "")) // 3 for m in messages)
        if tokens_out == 0:
            tokens_out = max(1, len(text) // 3)

        return TierResult(
            tier="gemma",
            text=text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency,
            retries=retries,
        )

    def _call_anthropic(
        self,
        tier: EscalationTier,
        model_id: str,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        temperature: float,
        retries: int,
    ) -> TierResult:
        import anthropic  # lazy

        client = self._anthropic_client
        if client is None:
            client = anthropic.Anthropic(
                api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
                timeout=self._timeout_s,
            )

        # Split system message if present
        system_txt = ""
        chat: list[dict[str, str]] = []
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            if role == "system":
                system_txt = (system_txt + "\n" + content).strip() if system_txt else content
                continue
            chat.append({"role": role, "content": content})

        # Mark the escalation prompt cacheable so repeat steps in a
        # chain (Gemma→Haiku→Sonnet→Opus) re-use the prefix instead of
        # paying full input cost each tier. Legacy strategy caches the
        # first user turn; system block goes through a cache wrapper
        # so the static instructions stay stable across escalations.
        from concinno.cache.anthropic_helpers import (
            system_with_cache,
            with_cache_control,
        )

        cached_chat = with_cache_control(chat, strategy="legacy")

        t0 = _now_ms()
        kwargs: dict[str, Any] = {
            "model": model_id,
            "max_tokens": max_tokens,
            "messages": cached_chat,
        }
        if not _is_opus_4_7_plus(model_id):
            kwargs["temperature"] = temperature
        if system_txt:
            kwargs["system"] = system_with_cache(system_txt)
        resp = client.messages.create(**kwargs)
        latency = _now_ms() - t0

        text = ""
        try:
            blocks = getattr(resp, "content", []) or []
            for blk in blocks:
                t = getattr(blk, "text", None)
                if t:
                    text += t
        except Exception:
            text = ""

        usage = getattr(resp, "usage", None)
        tokens_in = int(getattr(usage, "input_tokens", 0) or 0) if usage else 0
        tokens_out = int(getattr(usage, "output_tokens", 0) or 0) if usage else 0
        if tokens_in == 0:
            tokens_in = sum(len(m.get("content", "")) // 3 for m in messages)
        if tokens_out == 0:
            tokens_out = max(1, len(text) // 3)

        return TierResult(
            tier=tier,
            text=text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency,
            retries=retries,
        )


def _anthropic_model_for(tier: EscalationTier) -> str:
    if tier == "haiku":
        return os.environ.get("CONCINNO_HAIKU_MODEL", "claude-haiku-4-5")
    if tier == "sonnet":
        return os.environ.get("CONCINNO_SONNET_MODEL", "claude-sonnet-4-6")
    if tier == "opus":
        return os.environ.get("CONCINNO_OPUS_MODEL", "claude-opus-4-7")
    raise ValueError(f"Not an anthropic tier: {tier!r}")


def _is_opus_4_7_plus(model_id: str) -> bool:
    """True when ``model_id`` is Opus 4.7 or later.

    Opus 4.7 returns a 400 error if ``temperature`` / ``top_p`` / ``top_k``
    are set to any non-default value. Older Opus (4.6, 4.5, 4.1, 4.0, 3.x)
    and all Sonnet / Haiku variants still accept them.
    """
    if not model_id.startswith("claude-opus-"):
        return False
    rest = model_id.removeprefix("claude-opus-")
    parts = rest.split("-")
    if len(parts) < 2:
        return False
    try:
        major = int(parts[0])
        minor = int(parts[1])
    except ValueError:
        return False
    return (major, minor) >= (4, 7)


# ── Module-level convenience ────────────────────────────────


def escalate(messages: list[dict[str, str]], **kw: Any) -> EscalationResult:
    """Module-level convenience: builds a default LLMEscalator and calls it.

    Separate ``LLMEscalator`` kwargs (``chain``, ``cache_dir``,
    ``http_client``, etc.) from per-call kwargs (``max_tokens``,
    ``temperature``, ``force_tier``, ``stop_at``).
    """
    ctor_keys = {
        "chain",
        "cache_dir",
        "http_client",
        "anthropic_client",
        "max_retries_per_tier",
        "per_tier_timeout_s",
        "circuit_threshold",
        "circuit_cooldown_s",
    }
    ctor_kw = {k: kw.pop(k) for k in list(kw.keys()) if k in ctor_keys}
    esc = LLMEscalator(**ctor_kw)
    return esc.escalate(messages, **kw)
