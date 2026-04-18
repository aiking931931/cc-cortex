"""Tests for ``concinno.cache.anthropic_helpers``.

Before 2.7.0 every Concinno-internal consumer that called Anthropic
(escalation chain, llm-guard, a2a agent, GAIA agent, eval runner)
rolled its own cache logic or had none. This module is the single
blessed path; the tests pin:

* breakpoint shape is exactly what Anthropic's API expects
* cache position is stable across identical inputs (= real cache
  hits, not just "a block with cache_control")
* all five caching strategies round-trip cleanly
* non-mutating on input (caller keeps a clean copy for retries)
"""

from __future__ import annotations

import pytest

from concinno.cache.anthropic_helpers import (
    DEFAULT_STRATEGY,
    STRATEGIES,
    cache_breakpoint,
    system_with_cache,
    with_cache_control,
)


# ── cache_breakpoint shape ────────────────────────────────────


def test_cache_breakpoint_default_ttl():
    block = cache_breakpoint()
    assert block == {"type": "ephemeral"}


def test_cache_breakpoint_5m_is_alias_of_default():
    assert cache_breakpoint("5m") == cache_breakpoint()


def test_cache_breakpoint_1h_adds_ttl():
    block = cache_breakpoint("1h")
    assert block == {"type": "ephemeral", "ttl": "1h"}


def test_cache_breakpoint_rejects_unknown_ttl():
    with pytest.raises(ValueError, match="unsupported cache_ttl"):
        cache_breakpoint("30m")


# ── with_cache_control — legacy ───────────────────────────────


def test_legacy_skips_single_turn():
    msgs = [{"role": "user", "content": "solo"}]
    out = with_cache_control(msgs, strategy="legacy")
    # Must NOT attach cache to a single-turn conversation.
    assert out == [{"role": "user", "content": "solo"}]


def test_legacy_caches_first_user_with_multiple_turns():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "q2"},
    ]
    out = with_cache_control(msgs, strategy="legacy")
    # First user turn wrapped in list-of-blocks with cache_control.
    assert isinstance(out[0]["content"], list)
    assert out[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    # Other turns untouched.
    assert out[1] == {"role": "assistant", "content": "hello"}
    assert out[2] == {"role": "user", "content": "q2"}


def test_legacy_stable_across_identical_inputs():
    """Cache position MUST be identical for identical input — this is
    the whole point of a cache. Drift here = silent cache misses."""
    msgs = [
        {"role": "user", "content": "same"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "still same"},
    ]
    out_a = with_cache_control(msgs, strategy="legacy")
    out_b = with_cache_control(msgs, strategy="legacy")
    assert out_a == out_b


# ── disabled ──────────────────────────────────────────────────


def test_disabled_returns_shallow_copy_no_cache():
    msgs = [
        {"role": "user", "content": "x"},
        {"role": "user", "content": "y"},
    ]
    out = with_cache_control(msgs, strategy="disabled")
    assert out == msgs
    # Shallow copy, not the same list object — otherwise callers
    # who mutate the return will also mutate their own input.
    assert out is not msgs


# ── explicit ──────────────────────────────────────────────────


def test_explicit_applies_to_each_index():
    msgs = [
        {"role": "user", "content": "0"},
        {"role": "assistant", "content": "1"},
        {"role": "user", "content": "2"},
    ]
    out = with_cache_control(
        msgs, strategy="explicit", breakpoints=[0, 2],
    )
    assert isinstance(out[0]["content"], list)
    assert out[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert isinstance(out[2]["content"], list)
    assert out[2]["content"][0]["cache_control"] == {"type": "ephemeral"}
    # Middle assistant message untouched.
    assert out[1] == {"role": "assistant", "content": "1"}


def test_explicit_negative_index_counts_from_end():
    msgs = [
        {"role": "user", "content": "0"},
        {"role": "user", "content": "1"},
        {"role": "user", "content": "2"},
    ]
    out = with_cache_control(
        msgs, strategy="explicit", breakpoints=[-1],
    )
    # -1 → index 2 (len 3 - 1)
    assert isinstance(out[2]["content"], list)
    assert out[0] == {"role": "user", "content": "0"}


def test_explicit_requires_breakpoints():
    msgs = [{"role": "user", "content": "x"}]
    with pytest.raises(ValueError, match="requires cache_breakpoints"):
        with_cache_control(msgs, strategy="explicit")


def test_explicit_out_of_range_raises():
    msgs = [{"role": "user", "content": "x"}]
    with pytest.raises(ValueError, match="out of range"):
        with_cache_control(
            msgs, strategy="explicit", breakpoints=[99],
        )


# ── multiturn ─────────────────────────────────────────────────


def test_multiturn_caches_first_and_third_from_last():
    # Five user turns: indices 0, 2, 4, 6, 8 are user; the caller's
    # own dict also alternates roles so assistant turns land at odd
    # indices. Third-from-last user is user_positions[-3] = index 4.
    msgs = []
    for i in range(5):
        msgs.append({"role": "user", "content": f"u{i}"})
        msgs.append({"role": "assistant", "content": f"a{i}"})
    out = with_cache_control(msgs, strategy="multiturn")
    # First user (index 0) cached.
    assert isinstance(out[0]["content"], list)
    # user_positions = [0,2,4,6,8]; positions[-3] = 4.
    assert isinstance(out[4]["content"], list)
    # Assistant in between untouched.
    assert out[1] == {"role": "assistant", "content": "a0"}


def test_multiturn_short_conv_caches_only_first():
    msgs = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
    ]
    out = with_cache_control(msgs, strategy="multiturn")
    assert isinstance(out[0]["content"], list)
    # Only 1 user turn total → no third-from-last to mark.
    assert out[1] == {"role": "assistant", "content": "b"}


# ── non-mutation ──────────────────────────────────────────────


def test_caller_input_not_mutated():
    msgs = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"},
    ]
    snap = [dict(m) for m in msgs]
    with_cache_control(msgs, strategy="legacy")
    # Every field in the caller's input must be preserved byte-for-byte.
    assert msgs == snap


def test_list_content_preserves_earlier_blocks():
    """List-content messages: cache attaches to the LAST block, earlier
    blocks (tool_use / image / text preambles) stay exactly as-is."""
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64"}},
                {"type": "text", "text": "describe"},
            ],
        },
        {"role": "user", "content": "again"},
    ]
    out = with_cache_control(msgs, strategy="legacy")
    # Image block untouched.
    assert out[0]["content"][0] == {"type": "image", "source": {"type": "base64"}}
    # Text block got cache_control.
    assert out[0]["content"][1]["cache_control"] == {"type": "ephemeral"}


# ── system_with_cache ─────────────────────────────────────────


def test_system_with_cache_returns_list_of_blocks():
    out = system_with_cache("you are helpful")
    assert out == [{
        "type": "text",
        "text": "you are helpful",
        "cache_control": {"type": "ephemeral"},
    }]


def test_system_with_cache_empty_falls_back_to_placeholder():
    out = system_with_cache("   ")
    assert out[0]["text"] == "You are a helpful assistant."
    assert out[0]["cache_control"] == {"type": "ephemeral"}


def test_system_with_cache_1h_ttl():
    out = system_with_cache("x", ttl="1h")
    assert out[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


# ── 5-mode matrix ─────────────────────────────────────────────


def test_default_strategy_is_legacy():
    assert DEFAULT_STRATEGY == "legacy"


@pytest.mark.parametrize("strategy", STRATEGIES)
def test_all_strategies_handle_empty_messages(strategy):
    if strategy == "explicit":
        out = with_cache_control(
            [], strategy=strategy, breakpoints=[],
        )
    else:
        out = with_cache_control([], strategy=strategy)
    assert out == []


# ── type errors surface clearly ───────────────────────────────


def test_non_list_messages_type_error():
    with pytest.raises(TypeError, match="list of dicts"):
        with_cache_control("not a list", strategy="legacy")  # type: ignore[arg-type]


def test_unknown_strategy_raises():
    with pytest.raises(ValueError, match="unknown cache strategy"):
        with_cache_control([{"role": "user", "content": "x"}], strategy="bogus")
