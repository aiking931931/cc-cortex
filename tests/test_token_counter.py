"""Tests for concinno.token_counter."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from concinno.token_counter import TokenCounter, _estimate_fast

# ── fast mode ─────────────────────────────────────────────


def test_fast_mode_basic():
    tc = TokenCounter(mode="fast")
    # 100 ASCII chars → ~25 tokens
    text = "a" * 100
    assert tc.count(text) == 25
    assert tc.last_mode_used == "fast"


def test_fast_mode_cjk():
    tc = TokenCounter(mode="fast")
    text = "字" * 200  # 200 CJK chars → 200 * 1.5 = 300
    assert tc.count(text) == 300
    assert tc.last_mode_used == "fast"


def test_fast_mode_mixed():
    tc = TokenCounter(mode="fast")
    text = "中文" * 50 + "a" * 100  # 100 CJK (150t) + 100 ASCII (25t) = 175
    assert tc.count(text) == 175


def test_fast_mode_empty():
    tc = TokenCounter(mode="fast")
    assert tc.count("") == 0
    assert _estimate_fast("") == 0


# ── tokenizer profile contract ─────────────────────────────
# 2.3.0 removed the ``opus47`` profile because its ratio constants
# could not be anchored to a published Anthropic source. These tests
# lock in the replacement contract: only ``legacy`` is accepted, and
# unknown profile names raise at the fast-mode entry point so silent
# substitution cannot regress.


def test_fast_mode_unknown_tokenizer_rejected():
    """Unknown tokenizer profile raises — no silent fallback."""
    import pytest
    with pytest.raises(ValueError, match="unknown tokenizer profile"):
        _estimate_fast("hello", tokenizer="opus47")
    with pytest.raises(ValueError, match="unknown tokenizer profile"):
        _estimate_fast("hello", tokenizer="future-model")


def test_fast_mode_legacy_is_the_only_profile():
    """``legacy`` ratio is the supported fast-mode profile."""
    text = "a" * 300
    assert _estimate_fast(text) == _estimate_fast(text, tokenizer="legacy")
    # ASCII ratio: len / 4
    assert _estimate_fast(text) == 75
    # CJK ratio: len * 1.5
    assert _estimate_fast("字" * 200) == 300


def test_tokencounter_opus_47_still_works_via_accurate_path():
    """Opus 4.7 callers must use accurate/hybrid, not a fast-mode profile.

    The constructor accepts any model name; fast mode falls back to
    ``legacy`` (via ``_tokenizer_for``) and only accurate/hybrid calls
    the real ``count_tokens`` API that knows the true tokenizer.
    """
    tc47 = TokenCounter(mode="fast", model="claude-opus-4-7")
    tc46 = TokenCounter(mode="fast", model="claude-opus-4-6")
    # Same fast-mode ratio regardless of model — by design.
    assert tc47.count("hello world") == tc46.count("hello world")


# ── hybrid mode ───────────────────────────────────────────


def test_hybrid_under_threshold():
    """Small input stays in fast path, never touches the client."""
    fake_client = MagicMock()
    tc = TokenCounter(mode="hybrid", budget=200_000, anthropic_client=fake_client)
    result = tc.count("hello world")
    assert result == _estimate_fast("hello world")
    assert tc.last_mode_used == "fast"
    fake_client.beta.messages.count_tokens.assert_not_called()


def test_hybrid_over_threshold_mock_accurate():
    """Crossing 80% cliff triggers an accurate API call."""
    fake_client = MagicMock()
    fake_client.beta.messages.count_tokens.return_value = MagicMock(input_tokens=170_000)

    tc = TokenCounter(mode="hybrid", budget=200_000, anthropic_client=fake_client)
    # ASCII length s.t. _estimate_fast >= 160_000 (= 0.8 * 200_000)
    big_text = "a" * 700_000  # 700k ASCII / 4 = 175k estimated
    result = tc.count(big_text)
    assert result == 170_000
    assert tc.last_mode_used == "accurate"
    fake_client.beta.messages.count_tokens.assert_called_once()


def test_hybrid_over_threshold_api_fail_returns_upper_bound():
    """If accurate fails inside hybrid, return fast * 1.15 (upper bound)."""
    fake_client = MagicMock()
    fake_client.beta.messages.count_tokens.side_effect = RuntimeError("network down")

    tc = TokenCounter(mode="hybrid", budget=200_000, anthropic_client=fake_client)
    big_text = "a" * 700_000
    fast = _estimate_fast(big_text)
    expected = int(fast * 1.15)

    result = tc.count(big_text)
    assert result == expected
    assert tc.last_mode_used == "fallback_fast_upper"


# ── accurate mode ─────────────────────────────────────────


def test_accurate_mode_with_client():
    fake_client = MagicMock()
    fake_client.beta.messages.count_tokens.return_value = MagicMock(input_tokens=42)
    tc = TokenCounter(mode="accurate", anthropic_client=fake_client)
    assert tc.count("anything") == 42
    assert tc.last_mode_used == "accurate"


def test_accurate_mode_no_client_fallback(caplog, monkeypatch):
    """No client + no env key → fallback to fast + warn."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    tc = TokenCounter(mode="accurate", anthropic_client=None)
    with caplog.at_level("WARNING"):
        result = tc.count("hello")
    assert result == _estimate_fast("hello")
    assert tc.last_mode_used == "fallback_fast"
    assert any("ANTHROPIC_API_KEY" in r.message or "anthropic SDK" in r.message
               for r in caplog.records)


def test_accurate_mode_network_fail_fallback(caplog):
    fake_client = MagicMock()
    fake_client.beta.messages.count_tokens.side_effect = ConnectionError("boom")
    tc = TokenCounter(mode="accurate", anthropic_client=fake_client)
    with caplog.at_level("WARNING"):
        result = tc.count("hello")
    assert result == _estimate_fast("hello")
    assert tc.last_mode_used == "fallback_fast"
    assert any("count_tokens failed" in r.message for r in caplog.records)


def test_accurate_mode_sdk_missing_fallback(caplog, monkeypatch):
    """Simulate ImportError on lazy import."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    tc = TokenCounter(mode="accurate", anthropic_client=None)

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) \
        else __builtins__.__import__

    def fake_import(name, *args, **kwargs):
        if name == "anthropic":
            raise ImportError("no module named anthropic")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=fake_import):
        with caplog.at_level("WARNING"):
            result = tc.count("hello")
    assert result == _estimate_fast("hello")
    assert tc.last_mode_used == "fallback_fast"


# ── telemetry ─────────────────────────────────────────────


def test_last_mode_used_telemetry():
    """Telemetry should reflect the *most recent* call only."""
    fake_client = MagicMock()
    fake_client.beta.messages.count_tokens.return_value = MagicMock(input_tokens=170_000)

    tc = TokenCounter(mode="hybrid", budget=200_000, anthropic_client=fake_client)
    tc.count("small")
    assert tc.last_mode_used == "fast"
    tc.count("a" * 700_000)
    assert tc.last_mode_used == "accurate"
    tc.count("small again")
    assert tc.last_mode_used == "fast"


# ── messages ──────────────────────────────────────────────


def test_count_messages_fast_mode():
    tc = TokenCounter(mode="fast")
    msgs = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
    ]
    expected = _estimate_fast("hello") + _estimate_fast("world")
    assert tc.count_messages(msgs) == expected


def test_count_messages_accurate_mode():
    fake_client = MagicMock()
    fake_client.beta.messages.count_tokens.return_value = MagicMock(input_tokens=99)
    tc = TokenCounter(mode="accurate", anthropic_client=fake_client)
    msgs = [{"role": "user", "content": "hi"}]
    assert tc.count_messages(msgs) == 99
    assert tc.last_mode_used == "accurate"
    fake_client.beta.messages.count_tokens.assert_called_once()


def test_count_messages_accurate_fallback():
    fake_client = MagicMock()
    fake_client.beta.messages.count_tokens.side_effect = RuntimeError("nope")
    tc = TokenCounter(mode="accurate", anthropic_client=fake_client)
    msgs = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
    ]
    expected = _estimate_fast("hello") + _estimate_fast("world")
    assert tc.count_messages(msgs) == expected
    assert tc.last_mode_used == "fallback_fast"


# ── validation ────────────────────────────────────────────


def test_invalid_mode_raises():
    with pytest.raises(ValueError, match="invalid mode"):
        TokenCounter(mode="bogus")  # type: ignore[arg-type]


def test_invalid_budget_raises():
    with pytest.raises(ValueError, match="budget must be positive"):
        TokenCounter(budget=0)
