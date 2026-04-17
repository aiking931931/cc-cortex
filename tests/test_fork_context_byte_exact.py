"""Tests for the byte-exact prompt cache additions in ``fork_context``.

Covers the CC 1.16.0 port of Claude Code's ``forkSubagent.ts`` prompt
cache strategy (lines 60-171):

  - ``RenderedPromptCache`` immutability + shape
  - ``normalize_tool_result_for_cache`` scrubs non-deterministic fields
  - ``insert_cache_breakpoints`` respects the 4-marker ceiling and
    places markers at strategic boundaries
  - ``render_for_fork_byte_exact`` emits a valid Anthropic params dict
    whose only per-sibling delta is the final directive text block
  - 10-sibling stress test: all siblings share a blake2b-identical
    prefix hash
  - ``estimate_cache_savings`` math for the 10 × 50K prefix scenario
  - Regression: existing ``SubagentContext.clone_for_child`` still
    works alongside the new rendering layer

Target: >=18 tests (actual: 20).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from concinno.agent.fork_context import (
    ANTHROPIC_MAX_CACHE_BREAKPOINTS,
    FORK_PLACEHOLDER_RESULT,
    CacheSafeParams,
    FileStateCache,
    RenderedPromptCache,
    SubagentContext,
    create_cache_safe_params,
    create_subagent_context,
    estimate_cache_savings,
    insert_cache_breakpoints,
    normalize_tool_result_for_cache,
    render_for_fork_byte_exact,
)

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _make_params(**overrides: Any) -> CacheSafeParams:
    defaults: dict[str, Any] = dict(
        system_prompt="you are a helpful assistant",
        tool_defs=[
            {"name": "Read", "input_schema": {"type": "object"}},
            {"name": "Edit", "input_schema": {"type": "object"}},
        ],
        betas=("context-1m-2025-08-07",),
        effort="medium",
        parent_session_id="sess-root",
        fork_depth=0,
    )
    defaults.update(overrides)
    return create_cache_safe_params(**defaults)


def _make_context(**overrides: Any) -> SubagentContext:
    params = _make_params(**overrides)
    return create_subagent_context(
        parent_params=params,
        parent_file_state=FileStateCache(),
        parent_transcript_ref="transcript://parent",
    )


def _make_rendered(
    *,
    system: str = "SYSTEM PROMPT BYTES",
    tools: str = "TOOL POOL BYTES",
    n_tool_results: int = 3,
    breakpoints: list[int] | None = None,
) -> RenderedPromptCache:
    placeholders = [
        FORK_PLACEHOLDER_RESULT.encode("utf-8") for _ in range(n_tool_results)
    ]
    return RenderedPromptCache(
        system_bytes=system.encode("utf-8"),
        tools_pool_bytes=tools.encode("utf-8"),
        placeholder_tool_results=placeholders,
        cache_breakpoints=breakpoints or [],
    )


def _prefix_hash(params: dict[str, Any]) -> str:
    """blake2b hash of everything in a fork request EXCEPT the final
    per-child directive text. Used to prove all siblings share bytes.
    """
    system_serialized = json.dumps(params["system"], sort_keys=True, ensure_ascii=False)
    # Drop the trailing text block (the directive) from the first user
    # message's content so only the shared prefix is hashed.
    msg = params["messages"][0]
    prefix_content = [
        block for block in msg["content"] if block.get("type") != "text"
    ]
    msg_serialized = json.dumps(
        {"role": msg["role"], "content": prefix_content},
        sort_keys=True,
        ensure_ascii=False,
    )
    h = hashlib.blake2b(digest_size=32)
    h.update(system_serialized.encode("utf-8"))
    h.update(b"\x00")
    h.update(msg_serialized.encode("utf-8"))
    h.update(b"\x00")
    h.update(str(params.get("model", "")).encode("utf-8"))
    h.update(b"\x00")
    h.update(str(params.get("max_tokens", "")).encode("utf-8"))
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# RenderedPromptCache — immutability
# --------------------------------------------------------------------------- #


def test_rendered_prompt_cache_is_frozen() -> None:
    rc = _make_rendered()
    with pytest.raises(FrozenInstanceError):
        rc.system_bytes = b"mutated"  # type: ignore[misc]


def test_rendered_prompt_cache_holds_exact_bytes() -> None:
    rc = _make_rendered(system="SYSTEM", tools="TOOLS", n_tool_results=2)
    assert rc.system_bytes == b"SYSTEM"
    assert rc.tools_pool_bytes == b"TOOLS"
    assert len(rc.placeholder_tool_results) == 2
    for entry in rc.placeholder_tool_results:
        assert entry == FORK_PLACEHOLDER_RESULT.encode("utf-8")


# --------------------------------------------------------------------------- #
# normalize_tool_result_for_cache
# --------------------------------------------------------------------------- #


def test_normalize_tool_result_strips_call_id_and_timestamp() -> None:
    raw = {
        "type": "tool_result",
        "tool_use_id": "toolu_01ABCXYZ",
        "call_id": "call_9zzz",
        "timestamp": 1_700_000_000,
        "created_at": 1_700_000_001,
        "content": [{"type": "text", "text": "hello from parent"}],
    }
    out = normalize_tool_result_for_cache(raw)
    assert out["tool_use_id"] == "tool_use_fork_placeholder"
    assert out["call_id"] == "call_fork_placeholder"
    assert out["timestamp"] == 0
    assert out["created_at"] == 0
    assert out["content"] == [
        {"type": "text", "text": FORK_PLACEHOLDER_RESULT},
    ]


def test_normalize_tool_result_does_not_mutate_input() -> None:
    raw = {
        "type": "tool_result",
        "tool_use_id": "toolu_01ABCXYZ",
        "timestamp": 42,
        "content": [{"type": "text", "text": "parent text"}],
    }
    snapshot = json.dumps(raw, sort_keys=True)
    _ = normalize_tool_result_for_cache(raw)
    assert json.dumps(raw, sort_keys=True) == snapshot


def test_normalize_tool_result_deterministic_across_calls() -> None:
    raw_a = {
        "tool_use_id": "toolu_random_a",
        "timestamp": 111,
        "content": [{"type": "text", "text": "A"}],
    }
    raw_b = {
        "tool_use_id": "toolu_random_b",
        "timestamp": 222,
        "content": [{"type": "text", "text": "B"}],
    }
    a = normalize_tool_result_for_cache(raw_a)
    b = normalize_tool_result_for_cache(raw_b)
    # After scrub + content rewrite, two different raw inputs collapse
    # to the same deterministic placeholder dict.
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


# --------------------------------------------------------------------------- #
# insert_cache_breakpoints
# --------------------------------------------------------------------------- #


def test_insert_cache_breakpoints_respects_max_limit() -> None:
    blocks: list[dict[str, Any]] = [
        {"type": "system", "text": "sys"},
        {"type": "tool", "name": "Read"},
        {"type": "tool", "name": "Edit"},
        {"type": "tool_result", "text": "result-a"},
        {"type": "tool_result", "text": "result-b"},
        {"type": "text", "text": "final"},
    ]
    out = insert_cache_breakpoints(blocks, max_breakpoints=2)
    marked = [b for b in out if "cache_control" in b]
    assert len(marked) == 2


def test_insert_cache_breakpoints_caps_at_anthropic_limit() -> None:
    # Asking for 99 markers still caps at 4.
    blocks: list[dict[str, Any]] = [
        {"type": "system", "text": "sys"},
        {"type": "tool", "name": "Read"},
        {"type": "tool_result", "text": "result"},
        {"type": "text", "text": "final"},
    ]
    out = insert_cache_breakpoints(blocks, max_breakpoints=99)
    marked = [b for b in out if "cache_control" in b]
    assert len(marked) <= ANTHROPIC_MAX_CACHE_BREAKPOINTS


def test_insert_cache_breakpoints_places_at_strategic_boundaries() -> None:
    blocks: list[dict[str, Any]] = [
        {"type": "text", "text": "intro"},
        {"type": "system", "text": "sys end"},  # idx 1 — system boundary
        {"type": "tool", "name": "Read"},
        {"type": "tool", "name": "Edit"},  # idx 3 — tools boundary
        {"type": "tool_result", "text": "result-a"},
        {"type": "tool_result", "text": "result-b"},  # idx 5 — history boundary
        {"type": "text", "text": "directive"},
    ]
    out = insert_cache_breakpoints(blocks, max_breakpoints=4)
    marked_indices = [i for i, b in enumerate(out) if "cache_control" in b]
    # Expect markers at system end (1), tools end (3), history end (5),
    # fallback final block (6).
    assert 1 in marked_indices
    assert 3 in marked_indices
    assert 5 in marked_indices


def test_insert_cache_breakpoints_zero_budget_noop() -> None:
    blocks: list[dict[str, Any]] = [{"type": "system", "text": "sys"}]
    out = insert_cache_breakpoints(blocks, max_breakpoints=0)
    assert all("cache_control" not in b for b in out)


def test_insert_cache_breakpoints_does_not_mutate_input() -> None:
    blocks: list[dict[str, Any]] = [
        {"type": "system", "text": "sys"},
        {"type": "text", "text": "final"},
    ]
    snapshot = json.dumps(blocks, sort_keys=True)
    _ = insert_cache_breakpoints(blocks, max_breakpoints=4)
    assert json.dumps(blocks, sort_keys=True) == snapshot


# --------------------------------------------------------------------------- #
# render_for_fork_byte_exact — shape + byte equality
# --------------------------------------------------------------------------- #


def test_render_for_fork_emits_anthropic_params_shape() -> None:
    ctx = _make_context()
    rendered = _make_rendered()
    params = render_for_fork_byte_exact(
        ctx, rendered, "do the thing", model="claude-opus-4-6", max_tokens=4096
    )
    assert params["model"] == "claude-opus-4-6"
    assert params["max_tokens"] == 4096
    assert isinstance(params["system"], list)
    assert params["system"][0]["type"] == "text"
    assert params["system"][0]["text"] == "SYSTEM PROMPT BYTES"
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert isinstance(params["messages"], list)
    assert len(params["messages"]) == 1
    assert params["messages"][0]["role"] == "user"


def test_render_for_fork_forwards_betas_and_thinking() -> None:
    ctx = _make_context(
        betas=("context-1m-2025-08-07", "extended-thinking-2025"),
        effort="high",
    )
    params = render_for_fork_byte_exact(ctx, _make_rendered(), "directive")
    assert params["betas"] == [
        "context-1m-2025-08-07",
        "extended-thinking-2025",
    ]
    assert params["thinking"] == {"type": "enabled", "effort": "high"}


def test_render_for_fork_directive_is_last_text_block() -> None:
    ctx = _make_context()
    params = render_for_fork_byte_exact(
        ctx, _make_rendered(n_tool_results=2), "unique per-sibling scope"
    )
    content = params["messages"][0]["content"]
    # Last block must be the directive text; all preceding blocks are
    # tool_results.
    assert content[-1] == {"type": "text", "text": "unique per-sibling scope"}
    for block in content[:-1]:
        assert block["type"] == "tool_result"


def test_two_siblings_share_system_bytes() -> None:
    ctx = _make_context()
    rendered = _make_rendered()
    a = render_for_fork_byte_exact(ctx, rendered, "directive A")
    b = render_for_fork_byte_exact(ctx, rendered, "directive B")
    assert a["system"] == b["system"]


def test_two_siblings_share_tool_result_bytes() -> None:
    ctx = _make_context()
    rendered = _make_rendered(n_tool_results=4)
    a = render_for_fork_byte_exact(ctx, rendered, "directive A")
    b = render_for_fork_byte_exact(ctx, rendered, "directive B")
    a_tool_results = [
        blk for blk in a["messages"][0]["content"] if blk["type"] == "tool_result"
    ]
    b_tool_results = [
        blk for blk in b["messages"][0]["content"] if blk["type"] == "tool_result"
    ]
    assert a_tool_results == b_tool_results
    # And they carry the exact placeholder text.
    for blk in a_tool_results:
        assert blk["content"] == [
            {"type": "text", "text": FORK_PLACEHOLDER_RESULT},
        ]


def test_two_siblings_differ_only_in_directive() -> None:
    ctx = _make_context()
    rendered = _make_rendered()
    a = render_for_fork_byte_exact(ctx, rendered, "do X")
    b = render_for_fork_byte_exact(ctx, rendered, "do Y")
    # Same system, same tool_results, only the last text block differs.
    assert a["system"] == b["system"]
    a_content = a["messages"][0]["content"]
    b_content = b["messages"][0]["content"]
    assert a_content[:-1] == b_content[:-1]
    assert a_content[-1] != b_content[-1]


def test_ten_siblings_hash_to_same_prefix() -> None:
    ctx = _make_context()
    rendered = _make_rendered(n_tool_results=5)
    hashes = {
        _prefix_hash(
            render_for_fork_byte_exact(ctx, rendered, f"directive {i}")
        )
        for i in range(10)
    }
    # All 10 siblings collapse to exactly ONE prefix hash.
    assert len(hashes) == 1


# --------------------------------------------------------------------------- #
# estimate_cache_savings — math
# --------------------------------------------------------------------------- #


def test_estimate_cache_savings_single_sibling_is_zero() -> None:
    result = estimate_cache_savings(1, 50_000, 500)
    assert result["saved_usd"] == pytest.approx(0.0)
    assert result["hit_ratio"] == pytest.approx(0.0)


def test_estimate_cache_savings_ten_siblings_50k_prefix() -> None:
    # 10 siblings × 50K shared prefix = 500K baseline prefix tokens.
    # Fork path: 1 × 50K write + 9 × 50K × 10% read = 50K + 45K = 95K
    # Saved: 500K - 95K = 405K prefix tokens equivalent (not all are
    # "avoided" — 45K are still billed at the discount). The hit_ratio
    # measures the fraction of siblings that hit the cache, which is
    # 9/10 = 0.9.
    result = estimate_cache_savings(10, 50_000, 500)
    assert result["hit_ratio"] == pytest.approx(0.9)
    assert result["baseline_usd"] > result["fork_usd"]
    assert result["saved_usd"] > 0.0
    # Savings for 10 × 50K shared prefix at $3/Mtok should be
    # meaningful (>$1.00).
    assert result["saved_usd"] > 1.0


def test_estimate_cache_savings_rejects_negative_inputs() -> None:
    with pytest.raises(ValueError):
        estimate_cache_savings(0, 1000, 100)
    with pytest.raises(ValueError):
        estimate_cache_savings(5, -1, 100)
    with pytest.raises(ValueError):
        estimate_cache_savings(5, 1000, -1)


# --------------------------------------------------------------------------- #
# Regression — existing clone_for_child still works alongside new layer
# --------------------------------------------------------------------------- #


def test_clone_for_child_still_works_with_byte_exact_render() -> None:
    parent = _make_context()
    child = parent.clone_for_child()
    assert child.params.fork_depth == parent.params.fork_depth + 1
    # Both parent and child can render byte-exact fork requests off
    # the same RenderedPromptCache without interference.
    rendered = _make_rendered()
    parent_params = render_for_fork_byte_exact(parent, rendered, "parent scope")
    child_params = render_for_fork_byte_exact(child, rendered, "child scope")
    assert parent_params["system"] == child_params["system"]
    # File state isolation preserved (empty but independent).
    assert parent.file_state is not child.file_state
