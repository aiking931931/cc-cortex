"""Tests for concinno.signal_patterns."""

from __future__ import annotations

from concinno.signal_patterns import (
    CHARACTER_CHALLENGE_PATTERN,
    GREETING_PATTERN,
    IDENTITY_PROBE_PATTERN,
    detect_signals,
    is_character_challenge,
    is_greeting,
    is_identity_probe,
)

# ── Greeting detection ─────────────────────────────────────────


def test_greeting_english_basic() -> None:
    assert is_greeting("hi")
    assert is_greeting("hello there")
    assert is_greeting("hey friend")


def test_greeting_english_case_insensitive() -> None:
    assert is_greeting("HELLO")
    assert is_greeting("Hi")


def test_greeting_english_phrases() -> None:
    assert is_greeting("good morning")
    assert is_greeting("how are you doing today")
    assert is_greeting("what's up")
    assert is_greeting("whats up")
    assert is_greeting("thanks a lot")


def test_greeting_cjk() -> None:
    assert is_greeting("\u4f60\u597d")  # 你好
    assert is_greeting("\u563f")  # 嗨
    assert is_greeting("\u55e8")  # 嘿
    assert is_greeting("\u8b1d\u8b1d")  # 謝謝
    assert is_greeting("\u611f\u8b1d")  # 感謝


def test_greeting_negative() -> None:
    assert not is_greeting("can you help me debug this code")
    assert not is_greeting("what is the weather")
    assert not is_greeting("")


def test_greeting_pattern_exported() -> None:
    # Module-level compiled object is usable directly.
    assert GREETING_PATTERN.search("hi") is not None
    assert GREETING_PATTERN.search("random") is None


# ── Identity probe detection ───────────────────────────────────


def test_identity_probe_english() -> None:
    assert is_identity_probe("why do you act that way")
    assert is_identity_probe("tell me about your past")
    assert is_identity_probe("how do you feel about this")
    assert is_identity_probe("describe your personality please")
    assert is_identity_probe("your background sounds interesting")
    assert is_identity_probe("please stay in character")


def test_identity_probe_cjk() -> None:
    # 你的過去 — your past
    assert is_identity_probe("\u4f60\u7684\u904e\u53bb")
    # 為什麼你 — why do you
    assert is_identity_probe("\u70ba\u4ec0\u9ebc\u4f60")
    # 保持角色 — stay in character
    assert is_identity_probe("\u4fdd\u6301\u89d2\u8272")


def test_identity_probe_negative() -> None:
    assert not is_identity_probe("hi")
    assert not is_identity_probe("what time is it")
    assert not is_identity_probe("")


def test_identity_probe_pattern_exported() -> None:
    assert IDENTITY_PROBE_PATTERN.search("your personality") is not None


# ── Character challenge detection ──────────────────────────────


def test_challenge_english() -> None:
    assert is_character_challenge("break character now")
    assert is_character_challenge("stop pretending")
    assert is_character_challenge("you're just an ai")
    assert is_character_challenge("youre just a ai")
    assert is_character_challenge("drop the act")


def test_challenge_cjk() -> None:
    # 你是人工智能
    assert is_character_challenge(
        "\u4f60\u662f\u4eba\u5de5\u667a\u80fd"
    )
    # 別裝了
    assert is_character_challenge("\u5225\u88dd\u4e86")
    # 你不是真的
    assert is_character_challenge(
        "\u4f60\u4e0d\u662f\u771f\u7684"
    )


def test_challenge_negative() -> None:
    assert not is_character_challenge("nice to meet you")
    assert not is_character_challenge("tell me a story")
    assert not is_character_challenge("")


def test_challenge_pattern_exported() -> None:
    assert CHARACTER_CHALLENGE_PATTERN.search("break character") is not None


# ── detect_signals aggregator ──────────────────────────────────


def test_detect_signals_shape() -> None:
    sig = detect_signals("hi")
    assert set(sig.keys()) == {"fast_match", "identity_probe", "challenge"}
    assert all(isinstance(v, bool) for v in sig.values())


def test_detect_signals_greeting_only() -> None:
    sig = detect_signals("hello there")
    assert sig["fast_match"] is True
    assert sig["identity_probe"] is False
    assert sig["challenge"] is False


def test_detect_signals_probe_only() -> None:
    sig = detect_signals("tell me about your background")
    assert sig["fast_match"] is False
    assert sig["identity_probe"] is True
    assert sig["challenge"] is False


def test_detect_signals_challenge_only() -> None:
    sig = detect_signals("break character immediately")
    assert sig["fast_match"] is False
    assert sig["identity_probe"] is False
    assert sig["challenge"] is True


def test_detect_signals_all_false_on_neutral() -> None:
    sig = detect_signals("what is 2 plus 2")
    assert sig == {
        "fast_match": False,
        "identity_probe": False,
        "challenge": False,
    }


def test_detect_signals_empty_string() -> None:
    sig = detect_signals("")
    assert sig == {
        "fast_match": False,
        "identity_probe": False,
        "challenge": False,
    }
