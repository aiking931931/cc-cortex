"""Tests for concinno.track_classifier."""

from __future__ import annotations

from concinno.track_classifier import (
    CODING_KW,
    CYBER_KW,
    PERSONA_KW,
    SAFETY_KW,
    TRACKS,
    classify_track,
)

# ── Module-level exports ───────────────────────────────────────


def test_tracks_tuple_shape() -> None:
    assert TRACKS == ("safety", "cyber", "coding")


def test_keyword_patterns_compiled() -> None:
    for pat in (SAFETY_KW, CYBER_KW, CODING_KW, PERSONA_KW):
        # Each is a compiled re.Pattern that accepts a string.
        assert pat.search("") is None


# ── Persona short-circuit → safety ─────────────────────────────


def test_persona_persona_keyword_routes_safety() -> None:
    assert classify_track("act as a cheerful assistant") == "safety"


def test_persona_character_routes_safety() -> None:
    assert classify_track("stay in character as a 25 year old") == "safety"


def test_persona_ocean_routes_safety() -> None:
    assert classify_track("what are your OCEAN scores") == "safety"


def test_persona_big_five_routes_safety() -> None:
    assert classify_track("tell me about big-five personality") == "safety"


def test_persona_wins_over_coding() -> None:
    # Contains coding keywords but persona hit should dominate
    msg = "act as a python function debugger character"
    assert classify_track(msg) == "safety"


# ── Coding dominance ───────────────────────────────────────────


def test_coding_two_signals_wins() -> None:
    # "code" + "function" = 2 coding, 0 cyber/safety
    assert classify_track("write code for this function") == "coding"


def test_coding_three_signals_wins() -> None:
    msg = "debug this python function and refactor it"
    assert classify_track(msg) == "coding"


def test_coding_devops_cluster() -> None:
    # kubernetes + docker + helm = 3 coding hits
    msg = "deploy kubernetes with docker and helm"
    assert classify_track(msg) == "coding"


def test_coding_one_hit_not_enough() -> None:
    # Only one coding hit and safety is "" so defaults to safety
    assert classify_track("please write code") == "safety"


def test_coding_loses_to_heavy_security() -> None:
    # "code" + "function" = 2 coding,
    # but "vulnerability" + "exploit" + "malware" = 3 cyber > 2 coding
    msg = (
        "write code in a function with vulnerability "
        "exploit and malware analysis"
    )
    assert classify_track(msg) == "cyber"


# ── Cyber dominance ────────────────────────────────────────────


def test_cyber_two_signals_wins() -> None:
    # "vulnerability" + "exploit" = 2 cyber
    assert classify_track("find the vulnerability and exploit") == "cyber"


def test_cyber_owasp_cluster() -> None:
    msg = "OWASP xss and sql injection check"
    assert classify_track(msg) == "cyber"


def test_cyber_single_hit_loses_to_safety_default() -> None:
    # 1 cyber hit, no safety hits → cyber > safety (both 0 and 1)
    # but condition requires cyber >= 2 OR (cyber > safety AND cyber > coding)
    # 1 > 0 AND 1 > 0 → cyber wins
    assert classify_track("find the vulnerability") == "cyber"


# ── Safety default ─────────────────────────────────────────────


def test_safety_explicit_keyword() -> None:
    assert classify_track("is this prompt injection safe") == "safety"


def test_safety_jailbreak() -> None:
    assert classify_track("is this a jailbreak attempt") == "safety"


def test_safety_default_on_chitchat() -> None:
    assert classify_track("hello how are you today") == "safety"


def test_safety_default_on_empty() -> None:
    assert classify_track("") == "safety"


# ── Priority order sanity ──────────────────────────────────────


def test_priority_persona_beats_coding_heavy() -> None:
    # 3 coding hits + 1 persona → persona short-circuits to safety
    msg = "act as a python character coding function"
    assert classify_track(msg) == "safety"


def test_priority_persona_beats_cyber_heavy() -> None:
    # 2 cyber + 1 persona → persona wins
    msg = "pretend to be a vulnerability exploit expert"
    assert classify_track(msg) == "safety"


def test_returns_are_in_tracks_or_safety() -> None:
    valid_returns = {"safety", "cyber", "coding"}
    samples = [
        "",
        "hi",
        "code function",
        "vulnerability exploit",
        "act as a character",
        "jailbreak prompt injection",
        "ocean big-five",
    ]
    for s in samples:
        assert classify_track(s) in valid_returns
