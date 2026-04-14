"""Tests for cc_cortex.escalation — multi-tier LLM gateway.

All network I/O is mocked at the httpx.Client / anthropic.Anthropic level.
No real API calls, no real sockets.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from cc_cortex.escalation import (
    DEFAULT_CHAIN,
    CircuitOpen,
    EscalationExhausted,
    EscalationResult,
    LLMEscalator,
    TierResult,
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
