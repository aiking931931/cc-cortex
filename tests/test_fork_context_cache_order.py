"""Regression test for F4 (2.7.1): ``insert_cache_breakpoints`` tools-first.

Previously the priority order was [system, tools, history, fallback].
Anthropic's prompt caching best practices recommend caching the most
stable prefix first — tools change less often than a rendered system
prompt. With cap=2 the markers should land on tools + system.
"""

from __future__ import annotations

from concinno.agent.fork_context import insert_cache_breakpoints


def _block(type_: str, text: str = "") -> dict:
    return {"type": type_, "text": text}


def test_cap_2_picks_tools_and_system() -> None:
    """cap=2 → markers on tools + system, NOT history."""
    blocks = [
        _block("system", "sys"),
        _block("tool_definition", "td"),
        _block("tool_result", "tr"),
    ]
    out = insert_cache_breakpoints(blocks, max_breakpoints=2)

    # Exactly 2 cache_control markers present
    marked = [i for i, b in enumerate(out) if b.get("cache_control")]
    assert len(marked) == 2

    # Must include tools (index 1) and system (index 0)
    types_marked = {out[i].get("type") for i in marked}
    assert "tool_definition" in types_marked
    assert "system" in types_marked
    assert "tool_result" not in types_marked


def test_cap_1_picks_tools_only() -> None:
    """cap=1 → tools wins over system (tools first)."""
    blocks = [_block("system", "s"), _block("tool_definition", "t")]
    out = insert_cache_breakpoints(blocks, max_breakpoints=1)
    marked = [i for i, b in enumerate(out) if b.get("cache_control")]
    assert len(marked) == 1
    assert out[marked[0]].get("type") == "tool_definition"


def test_input_not_mutated() -> None:
    """Input list / dicts must remain untouched."""
    blocks = [_block("system", "s"), _block("tool_definition", "t")]
    snapshot = [dict(b) for b in blocks]
    insert_cache_breakpoints(blocks, max_breakpoints=2)
    assert blocks == snapshot
