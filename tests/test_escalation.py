"""Tests for concinno.escalation — multi-tier LLM gateway.

All network I/O is mocked at the httpx.Client / anthropic.Anthropic level.
No real API calls, no real sockets.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from concinno.escalation import (
    DEFAULT_CHAIN,
    CircuitOpen,
    EscalationExhausted,
    EscalationResult,
    LLMEscalator,
    TierResult,
    _anthropic_model_for,
    _is_opus_4_7_plus,
    escalate,
)

# ── Fixtures ────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _anthropic_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default: Anthropic key present so Claude tiers run."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")


@pytest.fixture
def messages() -> list[dict[str, str]]:
    return [{"role": "user", "content": "hello"}]


def _mk_httpx_response(
    content: str = "gemma says hi",
    reasoning: str = "",
    status: int = 200,
    usage: dict[str, int] | None = None,
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.raise_for_status = MagicMock()
    if status >= 400:
        err = httpx.HTTPStatusError(
            "boom",
            request=MagicMock(),
            response=MagicMock(status_code=status),
        )
        resp.raise_for_status.side_effect = err
    body: dict[str, Any] = {
        "choices": [
            {
                "message": {
                    "content": content,
                    "reasoning": reasoning,
                }
            }
        ],
        "usage": usage or {"prompt_tokens": 3, "completion_tokens": 4},
    }
    resp.json.return_value = body
    return resp


def _mk_http_client(response: Any = None) -> MagicMock:
    client = MagicMock()
    client.post.return_value = response or _mk_httpx_response()
    return client


def _mk_anth_response(text: str = "claude says hi") -> MagicMock:
    block = MagicMock()
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    usage = MagicMock()
    usage.input_tokens = 5
    usage.output_tokens = 6
    resp.usage = usage
    return resp


def _mk_anth_client(response: Any = None) -> MagicMock:
    client = MagicMock()
    client.messages.create.return_value = response or _mk_anth_response()
    return client


@pytest.fixture
def escalator(tmp_path: Any) -> LLMEscalator:
    return LLMEscalator(
        cache_dir=str(tmp_path),
        http_client=_mk_http_client(),
        anthropic_client=_mk_anth_client(),
    )


# ── Tests ───────────────────────────────────────────────────


def test_default_chain_constant_order() -> None:
    assert DEFAULT_CHAIN == ("gemma", "haiku", "sonnet", "opus")


def test_gemma_success_returns_immediately(
    tmp_path: Any, messages: list[dict[str, str]]
) -> None:
    http = _mk_http_client(_mk_httpx_response(content="ok from gemma"))
    anth = _mk_anth_client()
    esc = LLMEscalator(
        cache_dir=str(tmp_path), http_client=http, anthropic_client=anth
    )
    result = esc.escalate(messages)
    assert result.final.tier == "gemma"
    assert result.final.text == "ok from gemma"
    assert len(result.attempts) == 1
    # Claude tiers never touched
    anth.messages.create.assert_not_called()


def test_gemma_fail_falls_through_to_haiku(
    tmp_path: Any, messages: list[dict[str, str]]
) -> None:
    http = MagicMock()
    http.post.side_effect = httpx.ConnectError("down")
    anth = _mk_anth_client(_mk_anth_response("haiku saved the day"))
    esc = LLMEscalator(
        cache_dir=str(tmp_path),
        http_client=http,
        anthropic_client=anth,
        max_retries_per_tier=0,
    )
    result = esc.escalate(messages)
    assert result.final.tier == "haiku"
    assert result.final.text == "haiku saved the day"
    assert result.attempts[0].tier == "gemma"
    assert result.attempts[0].text == ""  # failed tier
    assert result.attempts[1].tier == "haiku"


def test_all_tiers_fail_raises_exhausted(
    tmp_path: Any, messages: list[dict[str, str]]
) -> None:
    http = MagicMock()
    http.post.side_effect = httpx.ConnectError("down")
    anth = MagicMock()
    anth.messages.create.side_effect = RuntimeError("api down")
    esc = LLMEscalator(
        cache_dir=str(tmp_path),
        http_client=http,
        anthropic_client=anth,
        max_retries_per_tier=0,
    )
    with pytest.raises(EscalationExhausted) as excinfo:
        esc.escalate(messages)
    assert len(excinfo.value.failures) == 4


def test_force_tier_sonnet_only(
    tmp_path: Any, messages: list[dict[str, str]]
) -> None:
    anth = _mk_anth_client(_mk_anth_response("sonnet resp"))
    http = _mk_http_client()
    esc = LLMEscalator(
        cache_dir=str(tmp_path), http_client=http, anthropic_client=anth
    )
    result = esc.escalate(messages, force_tier="sonnet")
    assert result.final.tier == "sonnet"
    # Gemma never called
    http.post.assert_not_called()
    assert len(result.attempts) == 1


def test_force_tier_success_skips_others(
    tmp_path: Any, messages: list[dict[str, str]]
) -> None:
    anth = _mk_anth_client(_mk_anth_response("opus"))
    esc = LLMEscalator(
        cache_dir=str(tmp_path),
        http_client=_mk_http_client(),
        anthropic_client=anth,
    )
    result = esc.escalate(messages, force_tier="opus")
    assert result.final.tier == "opus"
    assert len(result.attempts) == 1
    assert result.chain == ("opus",)


def test_stop_at_haiku_caps_chain(
    tmp_path: Any, messages: list[dict[str, str]]
) -> None:
    http = MagicMock()
    http.post.side_effect = httpx.ConnectError("down")
    anth = MagicMock()
    anth.messages.create.side_effect = RuntimeError("claude down")
    esc = LLMEscalator(
        cache_dir=str(tmp_path),
        http_client=http,
        anthropic_client=anth,
        max_retries_per_tier=0,
    )
    with pytest.raises(EscalationExhausted) as excinfo:
        esc.escalate(messages, stop_at="haiku")
    tiers = [t for t, _ in excinfo.value.failures]
    assert tiers == ["gemma", "haiku"]


def test_transient_error_retried_once(
    tmp_path: Any, messages: list[dict[str, str]]
) -> None:
    http = MagicMock()
    # First call times out, second succeeds
    http.post.side_effect = [
        httpx.TimeoutException("slow"),
        _mk_httpx_response(content="retry win"),
    ]
    esc = LLMEscalator(
        cache_dir=str(tmp_path),
        http_client=http,
        anthropic_client=_mk_anth_client(),
        max_retries_per_tier=1,
    )
    result = esc.escalate(messages, force_tier="gemma")
    assert result.final.text == "retry win"
    assert result.final.retries == 1
    assert http.post.call_count == 2


def test_permanent_error_no_retry(
    tmp_path: Any, messages: list[dict[str, str]]
) -> None:
    http = MagicMock()
    http.post.side_effect = [
        httpx.HTTPStatusError(
            "bad",
            request=MagicMock(),
            response=MagicMock(status_code=400),
        ),
        _mk_httpx_response(content="should not see"),
    ]
    esc = LLMEscalator(
        cache_dir=str(tmp_path),
        http_client=http,
        anthropic_client=_mk_anth_client(),
        max_retries_per_tier=1,
    )
    with pytest.raises(EscalationExhausted):
        esc.escalate(messages, force_tier="gemma")
    assert http.post.call_count == 1  # no retry


def test_rate_limit_429_treated_transient(
    tmp_path: Any, messages: list[dict[str, str]]
) -> None:
    http = MagicMock()
    http.post.side_effect = [
        httpx.HTTPStatusError(
            "rate",
            request=MagicMock(),
            response=MagicMock(status_code=429),
        ),
        _mk_httpx_response(content="after rate"),
    ]
    esc = LLMEscalator(
        cache_dir=str(tmp_path),
        http_client=http,
        anthropic_client=_mk_anth_client(),
        max_retries_per_tier=1,
    )
    result = esc.escalate(messages, force_tier="gemma")
    assert result.final.text == "after rate"
    assert http.post.call_count == 2


def test_circuit_breaker_opens_after_threshold(
    tmp_path: Any, messages: list[dict[str, str]]
) -> None:
    http = MagicMock()
    http.post.side_effect = httpx.ConnectError("down")
    esc = LLMEscalator(
        cache_dir=str(tmp_path),
        http_client=http,
        anthropic_client=_mk_anth_client(),
        max_retries_per_tier=0,
        circuit_threshold=3,
        circuit_cooldown_s=120.0,
    )
    for _ in range(3):
        with pytest.raises(EscalationExhausted):
            esc.escalate(messages, force_tier="gemma")
    # 4th try: circuit open, gemma skipped without calling http
    http.post.reset_mock()
    with pytest.raises(EscalationExhausted):
        esc.escalate(messages, force_tier="gemma")
    http.post.assert_not_called()


def test_circuit_breaker_cooldown_elapses_half_open(
    tmp_path: Any, messages: list[dict[str, str]]
) -> None:
    http = MagicMock()
    http.post.side_effect = httpx.ConnectError("down")
    esc = LLMEscalator(
        cache_dir=str(tmp_path),
        http_client=http,
        anthropic_client=_mk_anth_client(),
        max_retries_per_tier=0,
        circuit_threshold=2,
        circuit_cooldown_s=0.01,
    )
    for _ in range(2):
        with pytest.raises(EscalationExhausted):
            esc.escalate(messages, force_tier="gemma")
    time.sleep(0.05)
    # cooldown elapsed → half-open → probe runs (fails again)
    http.post.reset_mock()
    with pytest.raises(EscalationExhausted):
        esc.escalate(messages, force_tier="gemma")
    assert http.post.call_count == 1


def test_circuit_breaker_half_open_success_closes(
    tmp_path: Any, messages: list[dict[str, str]]
) -> None:
    http = MagicMock()
    http.post.side_effect = [
        httpx.ConnectError("down"),
        httpx.ConnectError("down"),
        _mk_httpx_response(content="back!"),
        _mk_httpx_response(content="still good"),
    ]
    esc = LLMEscalator(
        cache_dir=str(tmp_path),
        http_client=http,
        anthropic_client=_mk_anth_client(),
        max_retries_per_tier=0,
        circuit_threshold=2,
        circuit_cooldown_s=0.01,
    )
    for _ in range(2):
        with pytest.raises(EscalationExhausted):
            esc.escalate(messages, force_tier="gemma")
    time.sleep(0.05)
    # Probe success → closed
    r1 = esc.escalate(messages, force_tier="gemma")
    assert r1.final.text == "back!"
    # Next call still works
    r2 = esc.escalate(messages, force_tier="gemma")
    assert r2.final.text == "still good"


def test_circuit_breaker_state_persists_across_instances(
    tmp_path: Any, messages: list[dict[str, str]]
) -> None:
    http1 = MagicMock()
    http1.post.side_effect = httpx.ConnectError("down")
    esc1 = LLMEscalator(
        cache_dir=str(tmp_path),
        http_client=http1,
        anthropic_client=_mk_anth_client(),
        max_retries_per_tier=0,
        circuit_threshold=2,
        circuit_cooldown_s=600.0,
    )
    for _ in range(2):
        with pytest.raises(EscalationExhausted):
            esc1.escalate(messages, force_tier="gemma")

    # Fresh instance, same cache dir
    http2 = _mk_http_client(_mk_httpx_response("should never run"))
    esc2 = LLMEscalator(
        cache_dir=str(tmp_path),
        http_client=http2,
        anthropic_client=_mk_anth_client(),
        max_retries_per_tier=0,
        circuit_threshold=2,
        circuit_cooldown_s=600.0,
    )
    with pytest.raises(EscalationExhausted):
        esc2.escalate(messages, force_tier="gemma")
    http2.post.assert_not_called()


def test_gemma_empty_content_fallback_to_reasoning(
    tmp_path: Any, messages: list[dict[str, str]]
) -> None:
    resp = _mk_httpx_response(content="", reasoning="thinking out loud")
    http = _mk_http_client(resp)
    esc = LLMEscalator(
        cache_dir=str(tmp_path),
        http_client=http,
        anthropic_client=_mk_anth_client(),
    )
    result = esc.escalate(messages, force_tier="gemma")
    assert result.final.text == "thinking out loud"


def test_token_counts_populated(
    tmp_path: Any, messages: list[dict[str, str]]
) -> None:
    esc = LLMEscalator(
        cache_dir=str(tmp_path),
        http_client=_mk_http_client(),
        anthropic_client=_mk_anth_client(),
    )
    result = esc.escalate(messages, force_tier="gemma")
    assert result.final.tokens_in == 3
    assert result.final.tokens_out == 4

    result2 = esc.escalate(messages, force_tier="haiku")
    assert result2.final.tokens_in == 5
    assert result2.final.tokens_out == 6


# ── Opus 4.7 compatibility ─────────────────────────────────


@pytest.mark.parametrize(
    "model_id,expected",
    [
        ("claude-opus-4-7", True),
        ("claude-opus-4-7-20260416", True),
        ("claude-opus-4-8", True),
        ("claude-opus-5-0", True),
        ("claude-opus-4-6", False),
        ("claude-opus-4-5", False),
        ("claude-opus-4-1", False),
        ("claude-opus-3-5", False),
        ("claude-sonnet-4-6", False),
        ("claude-haiku-4-5", False),
        ("gpt-4o", False),
        ("claude-opus", False),
        ("claude-opus-vnext", False),
    ],
)
def test_is_opus_4_7_plus_detector(model_id: str, expected: bool) -> None:
    assert _is_opus_4_7_plus(model_id) is expected


def test_default_opus_model_is_4_7(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CONCINNO_OPUS_MODEL", raising=False)
    assert _anthropic_model_for("opus") == "claude-opus-4-7"


def test_opus_env_override_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONCINNO_OPUS_MODEL", "claude-opus-4-6")
    assert _anthropic_model_for("opus") == "claude-opus-4-6"


def test_opus_4_7_strips_temperature(
    tmp_path: Any, messages: list[dict[str, str]]
) -> None:
    """Opus 4.7 rejects non-default temperature with 400 — must not be sent."""
    anth = _mk_anth_client(_mk_anth_response("opus 4.7 reply"))
    esc = LLMEscalator(
        cache_dir=str(tmp_path),
        http_client=_mk_http_client(),
        anthropic_client=anth,
    )
    esc.escalate(messages, force_tier="opus")
    anth.messages.create.assert_called_once()
    kwargs = anth.messages.create.call_args.kwargs
    assert kwargs["model"] == "claude-opus-4-7"
    assert "temperature" not in kwargs
    assert "top_p" not in kwargs
    assert "top_k" not in kwargs


def test_opus_4_6_keeps_temperature(
    tmp_path: Any,
    messages: list[dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-4.7 Opus (and all Sonnet/Haiku) still accept temperature."""
    monkeypatch.setenv("CONCINNO_OPUS_MODEL", "claude-opus-4-6")
    anth = _mk_anth_client(_mk_anth_response("opus 4.6 reply"))
    esc = LLMEscalator(
        cache_dir=str(tmp_path),
        http_client=_mk_http_client(),
        anthropic_client=anth,
    )
    esc.escalate(messages, force_tier="opus", temperature=0.5)
    kwargs = anth.messages.create.call_args.kwargs
    assert kwargs["model"] == "claude-opus-4-6"
    assert kwargs["temperature"] == 0.5


def test_sonnet_keeps_temperature(
    tmp_path: Any, messages: list[dict[str, str]]
) -> None:
    anth = _mk_anth_client(_mk_anth_response("sonnet reply"))
    esc = LLMEscalator(
        cache_dir=str(tmp_path),
        http_client=_mk_http_client(),
        anthropic_client=anth,
    )
    esc.escalate(messages, force_tier="sonnet", temperature=0.4)
    kwargs = anth.messages.create.call_args.kwargs
    assert kwargs["temperature"] == 0.4


def test_escalate_default_max_tokens_4096(
    tmp_path: Any, messages: list[dict[str, str]]
) -> None:
    """Default raised from 2048 → 4096 for Opus 4.7 new tokenizer."""
    anth = _mk_anth_client(_mk_anth_response("ok"))
    esc = LLMEscalator(
        cache_dir=str(tmp_path),
        http_client=_mk_http_client(),
        anthropic_client=anth,
    )
    esc.escalate(messages, force_tier="opus")
    kwargs = anth.messages.create.call_args.kwargs
    assert kwargs["max_tokens"] == 4096


def test_latency_recorded_monotonic(
    tmp_path: Any, messages: list[dict[str, str]]
) -> None:
    esc = LLMEscalator(
        cache_dir=str(tmp_path),
        http_client=_mk_http_client(),
        anthropic_client=_mk_anth_client(),
    )
    result = esc.escalate(messages, force_tier="gemma")
    assert isinstance(result.final.latency_ms, int)
    assert result.final.latency_ms >= 0


def test_stats_reports_per_tier_counts(
    tmp_path: Any, messages: list[dict[str, str]]
) -> None:
    http = MagicMock()
    http.post.side_effect = [
        httpx.ConnectError("down"),
        _mk_httpx_response(content="win"),
    ]
    esc = LLMEscalator(
        cache_dir=str(tmp_path),
        http_client=http,
        anthropic_client=_mk_anth_client(),
        max_retries_per_tier=0,
    )
    with pytest.raises(EscalationExhausted):
        esc.escalate(messages, force_tier="gemma")
    esc.escalate(messages, force_tier="gemma")
    stats = esc.stats()
    assert stats["gemma"]["calls"] == 2
    assert stats["gemma"]["successes"] == 1
    assert stats["gemma"]["failures"] == 1
    assert stats["gemma"]["circuit_state"] == "closed"


def test_missing_anthropic_key_skips_claude_tiers(
    tmp_path: Any,
    messages: list[dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    http = MagicMock()
    http.post.side_effect = httpx.ConnectError("down")
    anth = _mk_anth_client()
    esc = LLMEscalator(
        cache_dir=str(tmp_path),
        http_client=http,
        anthropic_client=anth,
        max_retries_per_tier=0,
    )
    with pytest.raises(EscalationExhausted) as excinfo:
        esc.escalate(messages)
    reasons = {t: r for t, r in excinfo.value.failures}
    assert "ANTHROPIC_API_KEY not set" in reasons.get("haiku", "")
    assert "ANTHROPIC_API_KEY not set" in reasons.get("sonnet", "")
    assert "ANTHROPIC_API_KEY not set" in reasons.get("opus", "")
    # Anthropic client was never called
    anth.messages.create.assert_not_called()


def test_force_tier_unknown_raises_valueerror(
    tmp_path: Any, messages: list[dict[str, str]]
) -> None:
    esc = LLMEscalator(
        cache_dir=str(tmp_path),
        http_client=_mk_http_client(),
        anthropic_client=_mk_anth_client(),
    )
    with pytest.raises(ValueError, match="Unknown force_tier"):
        esc.escalate(messages, force_tier="unknown")  # type: ignore[arg-type]


def test_convenience_escalate_module_function(
    tmp_path: Any, messages: list[dict[str, str]]
) -> None:
    http = _mk_http_client(_mk_httpx_response(content="via module"))
    anth = _mk_anth_client()
    result = escalate(
        messages,
        cache_dir=str(tmp_path),
        http_client=http,
        anthropic_client=anth,
    )
    assert isinstance(result, EscalationResult)
    assert result.final.tier == "gemma"
    assert result.final.text == "via module"


def test_attempts_include_failed_tiers_in_order(
    tmp_path: Any, messages: list[dict[str, str]]
) -> None:
    http = MagicMock()
    http.post.side_effect = httpx.ConnectError("down")

    anth = MagicMock()
    # haiku fails, sonnet succeeds
    calls = {"n": 0}

    def _create(**kw: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("haiku down")
        return _mk_anth_response("sonnet wins")

    anth.messages.create.side_effect = _create
    esc = LLMEscalator(
        cache_dir=str(tmp_path),
        http_client=http,
        anthropic_client=anth,
        max_retries_per_tier=0,
    )
    result = esc.escalate(messages)
    assert [a.tier for a in result.attempts] == ["gemma", "haiku", "sonnet"]
    assert result.final.tier == "sonnet"


def test_stop_at_unknown_tier_raises(
    tmp_path: Any, messages: list[dict[str, str]]
) -> None:
    esc = LLMEscalator(
        cache_dir=str(tmp_path),
        http_client=_mk_http_client(),
        anthropic_client=_mk_anth_client(),
    )
    with pytest.raises(ValueError, match="Unknown stop_at"):
        esc.escalate(messages, stop_at="nope")  # type: ignore[arg-type]


def test_empty_messages_raises_valueerror(
    tmp_path: Any,
) -> None:
    esc = LLMEscalator(
        cache_dir=str(tmp_path),
        http_client=_mk_http_client(),
        anthropic_client=_mk_anth_client(),
    )
    with pytest.raises(ValueError, match="messages cannot be empty"):
        esc.escalate([])


def test_tier_result_is_dataclass() -> None:
    r = TierResult(
        tier="gemma", text="x", tokens_in=1, tokens_out=2, latency_ms=3, retries=0
    )
    assert r.tier == "gemma"
    assert r.retries == 0


def test_circuit_open_exception_is_runtime_error() -> None:
    assert issubclass(CircuitOpen, RuntimeError)


def test_unknown_tier_in_chain_raises(tmp_path: Any) -> None:
    with pytest.raises(ValueError, match="Unknown tier in chain"):
        LLMEscalator(chain=("bogus",), cache_dir=str(tmp_path))  # type: ignore[arg-type]


def test_gemma_http_client_lazy_built(
    tmp_path: Any, messages: list[dict[str, str]]
) -> None:
    """When no http_client passed, _call_gemma builds one via lazy import."""
    fake_client = _mk_http_client(_mk_httpx_response("lazy built"))
    with patch("httpx.Client", return_value=fake_client) as ctor:
        esc = LLMEscalator(
            cache_dir=str(tmp_path),
            anthropic_client=_mk_anth_client(),
        )
        result = esc.escalate(messages, force_tier="gemma")
        assert result.final.text == "lazy built"
        ctor.assert_called_once()


# ══════════════════════════════════════════════════════════════
#  CircuitBreakerGuard integration (4.4.0 ship-fix FATAL-3)
# ══════════════════════════════════════════════════════════════
#
# These tests prove the opt-in Hystrix-grade guard:
#   * stays out of the way when no guard passed and the FEATURE_META
#     gate is OFF (default 4.0.0 baseline) — escalator behaviour is
#     bit-identical to the legacy file-backed breaker;
#   * trips the new guard on observed failures (mirrored from the
#     legacy breaker so both layers stay in sync);
#   * denies further attempts to that tier while the guard is OPEN —
#     the escalator falls through to the next tier exactly as if the
#     legacy breaker had tripped;
#   * shares state across LLMEscalator instances when the same guard
#     is passed in (the share_state_with contract for cross-tier /
#     cross-escalator state convergence);
#   * does not affect retry semantics on its own.


def _make_cb_guard(**kw: Any) -> Any:
    """Tiny helper — builds a CircuitBreakerGuard with defaults that
    keep the rate-limit window out of the way (we only want to
    exercise the breaker state machine in these tests)."""
    from concinno.security.circuit_breaker_guard import CircuitBreakerGuard

    kw.setdefault("max_calls", 0)  # disable rate limit
    kw.setdefault("failure_threshold", 2)
    kw.setdefault("cooldown_s", 60.0)
    return CircuitBreakerGuard(**kw)


def test_cb_guard_opt_in_does_not_change_happy_path(
    tmp_path: Any, messages: list[dict[str, str]]
) -> None:
    """Guard wired in but nothing fails → escalator behaves as before."""
    guard = _make_cb_guard()
    esc = LLMEscalator(
        cache_dir=str(tmp_path),
        http_client=_mk_http_client(_mk_httpx_response("ok")),
        anthropic_client=_mk_anth_client(),
        circuit_breaker_guard=guard,
    )
    result = esc.escalate(messages)
    assert result.final.tier == "gemma"
    assert result.final.text == "ok"
    # Guard saw one success report → consecutive_failures stays 0.
    snap = guard.snapshot("escalation.tier.gemma")
    assert snap.consecutive_failures == 0


def test_cb_guard_records_failure_when_tier_raises(
    tmp_path: Any, messages: list[dict[str, str]]
) -> None:
    """A raised exception from a tier mirrors into the guard."""
    guard = _make_cb_guard(failure_threshold=10)  # don't trip yet
    http = MagicMock()
    http.post.side_effect = httpx.ConnectError("down")
    esc = LLMEscalator(
        cache_dir=str(tmp_path),
        http_client=http,
        anthropic_client=_mk_anth_client(_mk_anth_response("haiku saved")),
        circuit_breaker_guard=guard,
        max_retries_per_tier=0,
    )
    result = esc.escalate(messages)
    assert result.final.tier == "haiku"  # gemma fell through
    snap = guard.snapshot("escalation.tier.gemma")
    assert snap.consecutive_failures == 1


def test_cb_guard_open_state_skips_tier(
    tmp_path: Any, messages: list[dict[str, str]]
) -> None:
    """Once the guard trips OPEN for a tier, escalator skips that tier."""
    guard = _make_cb_guard(failure_threshold=2, cooldown_s=3600.0)
    http = MagicMock()
    http.post.side_effect = httpx.ConnectError("down")
    esc = LLMEscalator(
        cache_dir=str(tmp_path),
        http_client=http,
        anthropic_client=_mk_anth_client(_mk_anth_response("haiku")),
        circuit_breaker_guard=guard,
        max_retries_per_tier=0,
        circuit_threshold=99,  # legacy breaker stays closed
    )
    # Two failures trip the guard for gemma.
    esc.escalate(messages)
    esc.escalate(messages)
    snap = guard.snapshot("escalation.tier.gemma")
    from concinno.security.circuit_breaker_guard import CircuitState
    assert snap.state == CircuitState.OPEN
    # Third call: gemma denied by guard pre-call probe → we never call
    # the http client this time.
    http.post.reset_mock()
    result = esc.escalate(messages)
    assert result.final.tier == "haiku"
    http.post.assert_not_called()


def test_cb_guard_share_state_across_escalators(
    tmp_path: Any, messages: list[dict[str, str]]
) -> None:
    """Two escalators sharing one guard converge on the same state."""
    guard = _make_cb_guard(failure_threshold=2, cooldown_s=3600.0)

    def _new(name: str) -> LLMEscalator:
        return LLMEscalator(
            cache_dir=str(tmp_path / name),
            http_client=_mk_http_client(),
            anthropic_client=_mk_anth_client(),
            circuit_breaker_guard=guard,
            max_retries_per_tier=0,
            circuit_threshold=99,
        )

    esc_a = _new("a")
    esc_b = _new("b")
    # Failure on esc_a should be visible to esc_b through the guard.
    http_fail = MagicMock()
    http_fail.post.side_effect = httpx.ConnectError("down")
    esc_a._http_client = http_fail
    esc_a.escalate(messages)
    esc_a.escalate(messages)
    snap = guard.snapshot("escalation.tier.gemma")
    from concinno.security.circuit_breaker_guard import CircuitState
    assert snap.state == CircuitState.OPEN
    # esc_b sees the same OPEN gemma → falls through to haiku without
    # touching its own http client.
    http_b_calls = MagicMock()
    esc_b._http_client = http_b_calls
    result = esc_b.escalate(messages)
    assert result.final.tier == "haiku"
    http_b_calls.post.assert_not_called()


def test_cb_guard_with_shared_circuit_breaker_factory(
    tmp_path: Any, messages: list[dict[str, str]]
) -> None:
    """The classmethod factory wires the guard the same way as the kwarg."""
    guard = _make_cb_guard()
    esc = LLMEscalator.with_shared_circuit_breaker(
        guard,
        cache_dir=str(tmp_path),
        http_client=_mk_http_client(),
        anthropic_client=_mk_anth_client(),
    )
    esc.escalate(messages)
    snap = guard.snapshot("escalation.tier.gemma")
    assert snap.consecutive_failures == 0
    assert esc._cb_guard is guard


def test_cb_guard_default_off_when_feature_meta_disabled(
    tmp_path: Any,
    messages: list[dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No guard passed + FEATURE_META off (default) → no guard built.

    The conftest autouse fixture ``_restore_default_on_for_legacy_tests``
    force-flips every ``DEFAULT_OFF_4_0_0`` feature to enabled=True for
    legacy tests; we override the gate predicate locally so this test
    can verify the *actual* default-off behaviour the 4.0.0 SEMVER
    baseline ships with.
    """
    monkeypatch.setattr(
        LLMEscalator, "_gate_enabled", staticmethod(lambda: False)
    )
    esc = LLMEscalator(
        cache_dir=str(tmp_path),
        http_client=_mk_http_client(),
        anthropic_client=_mk_anth_client(),
    )
    # circuit_breaker_guard gate forced False → no guard auto-constructed.
    assert esc._cb_guard is None


def test_cb_guard_auto_built_when_feature_meta_enabled(
    tmp_path: Any,
    messages: list[dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FEATURE_META enabled → escalator builds its own guard."""
    # Patch the gate predicate so we don't have to mutate the real
    # cc_config.json. The static method lives on LLMEscalator itself.
    monkeypatch.setattr(LLMEscalator, "_gate_enabled", staticmethod(lambda: True))
    esc = LLMEscalator(
        cache_dir=str(tmp_path),
        http_client=_mk_http_client(),
        anthropic_client=_mk_anth_client(),
        circuit_threshold=7,
        circuit_cooldown_s=42.0,
    )
    assert esc._cb_guard is not None
    # Auto-built guard inherits the escalator's failure_threshold and
    # cooldown so behaviour stays consistent across the two breaker
    # layers. cooldown is stored on the guard's _initial_cooldown_s
    # constant; the per-resource _BreakerState only adopts it after the
    # first trip (until then the dataclass DEFAULT_COOLDOWN_S is the
    # snapshot value).
    assert esc._cb_guard._initial_cooldown_s == 42.0
    assert esc._cb_guard._failure_threshold == 7
    # The auto-built guard uses our threshold — observe failures flip
    # the state to OPEN exactly at the configured threshold.
    from concinno.security.circuit_breaker_guard import CircuitState
    for _ in range(7):
        esc._cb_guard.evaluate({"resource": "probe", "outcome": "failure"})
    snap = esc._cb_guard.snapshot("probe")
    assert snap.state == CircuitState.OPEN
    # Once tripped the per-resource cooldown adopts the escalator's
    # configured value (42 s, not the 30 s dataclass default).
    assert snap.cooldown_s == 42.0


def test_cb_guard_record_helper_silently_swallows_errors(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_cb_guard_record`` must never raise — it's a best-effort mirror."""
    guard = _make_cb_guard()
    esc = LLMEscalator(
        cache_dir=str(tmp_path),
        http_client=_mk_http_client(),
        anthropic_client=_mk_anth_client(),
        circuit_breaker_guard=guard,
    )
    # Force the guard to raise — _cb_guard_record must not propagate.
    with patch.object(
        guard, "evaluate", side_effect=RuntimeError("guard exploded")
    ):
        esc._cb_guard_record("gemma", "failure")  # no raise expected

    # And when the guard is None the helper is a clean no-op. Pin the
    # gate to its 4.0.0 default-off baseline so the conftest legacy
    # auto-flip does not auto-build a guard for us.
    monkeypatch.setattr(
        LLMEscalator, "_gate_enabled", staticmethod(lambda: False)
    )
    esc2 = LLMEscalator(
        cache_dir=str(tmp_path / "no_guard"),
        http_client=_mk_http_client(),
        anthropic_client=_mk_anth_client(),
    )
    assert esc2._cb_guard is None
    esc2._cb_guard_record("gemma", "failure")  # no-op, no raise
