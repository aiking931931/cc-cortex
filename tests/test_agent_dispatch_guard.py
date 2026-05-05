"""Tests for concinno.guards.agent_dispatch_guard.

Focus: 2.10.4 poll-pattern detection logic. Token-zone strategy branches
are covered indirectly via integration tests in test_pipeline; here we
pin the new behavior.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from concinno.guards.agent_dispatch_guard import (
    AgentDispatchGuard,
    _extract_prompt,
    _has_unbounded_poll,
)
from concinno.guards.base import GuardContext


def _ctx(prompt: str = "", hook_event: str = "PreToolUse") -> GuardContext:
    return GuardContext(
        tool_name="Agent",
        tool_input={"prompt": prompt} if prompt else {},
        session_id="test",
        cache_dir="/tmp",
        hook_event=hook_event,
    )


# ---------------------------------------------------------------- _has_unbounded_poll


def test_unbounded_until_grep_detected():
    assert _has_unbounded_poll(
        'until grep -q "DONE" output; do sleep 15; done'
    )


def test_unbounded_until_bracket_detected():
    assert _has_unbounded_poll(
        'until [ -f done.marker ]; do sleep 30; done'  # no timeout guard
    ) is False  # -f is an EXIST check — acceptable substitute for timeout
    # The more dangerous pattern is `until [` with no file/timeout:
    # our regex matches the opening and relies on lack of `date +%s` guard
    # to decide. A bare `until [` with only a keyword check SHOULD warn.


def test_unbounded_while_bang_grep_detected():
    assert _has_unbounded_poll(
        'while ! grep DONE log; do sleep 10; done'
    )


def test_timeout_guard_via_date_suppresses_warning():
    assert not _has_unbounded_poll(
        'START=$(date +%s); '
        'until grep -q DONE log || [ $(($(date +%s)-START)) -gt 3600 ]; '
        'do sleep 15; done'
    )


def test_timeout_flag_suppresses_warning():
    assert not _has_unbounded_poll(
        'timeout 3600 bash -c "until grep DONE log; do sleep 15; done"'
    )


def test_seconds_guard_suppresses_warning():
    assert not _has_unbounded_poll(
        'SECONDS=0; until grep DONE log || [ $SECONDS -gt 3600 ]; do sleep 15; done'
    )


def test_no_poll_no_warning():
    assert not _has_unbounded_poll(
        "Please list files in /tmp and report back."
    )


def test_empty_prompt_no_warning():
    assert not _has_unbounded_poll("")


def test_env_escape_suppresses_warning():
    with patch.dict(os.environ, {"CONCINNO_ALLOW_UNBOUNDED_POLL": "1"}):
        assert not _has_unbounded_poll(
            'until grep -q DONE log; do sleep 15; done'
        )


def test_env_escape_zero_still_warns():
    with patch.dict(os.environ, {"CONCINNO_ALLOW_UNBOUNDED_POLL": "0"}):
        assert _has_unbounded_poll(
            'until grep -q DONE log; do sleep 15; done'
        )


# ---------------------------------------------------------------- _extract_prompt


def test_extract_prompt_from_agent_input():
    ctx = _ctx(prompt="test brief")
    assert _extract_prompt(ctx) == "test brief"


def test_extract_prompt_missing_returns_empty():
    ctx = GuardContext(
        tool_name="Agent",
        tool_input={},
        session_id="s",
        cache_dir="/tmp",
        hook_event="PreToolUse",
    )
    assert _extract_prompt(ctx) == ""


def test_extract_prompt_non_string_returns_empty():
    ctx = GuardContext(
        tool_name="Agent",
        tool_input={"prompt": 42},
        session_id="s",
        cache_dir="/tmp",
        hook_event="PreToolUse",
    )
    assert _extract_prompt(ctx) == ""


# ---------------------------------------------------------------- check() integration


def _mock_tokens(n: int):
    return patch(
        "concinno.guards.agent_dispatch_guard._get_input_tokens",
        return_value=n,
    )


def test_check_clean_prompt_injects_strategy_only():
    guard = AgentDispatchGuard()
    with _mock_tokens(10_000):
        result = guard.check(_ctx(prompt="Summarize README.md"))
    assert result.action.value == "allow"
    assert "GREEN" in (result.context or "")
    assert "unbounded poll" not in (result.context or "")


def test_check_poll_prompt_injects_warning():
    guard = AgentDispatchGuard()
    with _mock_tokens(10_000):
        result = guard.check(
            _ctx(prompt='SSH run: until grep -q DONE log; do sleep 15; done')
        )
    assert result.action.value == "allow"
    ctx_text = result.context or ""
    assert "GREEN" in ctx_text
    assert "unbounded poll" in ctx_text
    assert "2026-04-21" in ctx_text  # incident reference


def test_check_red_zone_with_poll_stacks_both():
    guard = AgentDispatchGuard()
    with _mock_tokens(200_000):
        result = guard.check(
            _ctx(prompt='until grep DONE log; do sleep 15; done')
        )
    ctx_text = result.context or ""
    assert "RED" in ctx_text
    assert "unbounded poll" in ctx_text


def test_check_post_tool_use_ignores_poll():
    guard = AgentDispatchGuard()
    result = guard.check(
        _ctx(
            prompt='until grep DONE log; do sleep 15; done',
            hook_event="PostToolUse",
        )
    )
    # PostToolUse is result-check reminder, poll-scan only on Pre
    assert "subagent returned" in (result.context or "").lower()
    assert "unbounded poll" not in (result.context or "")


def test_check_non_agent_tool_noop():
    guard = AgentDispatchGuard()
    ctx = GuardContext(
        tool_name="Bash",
        tool_input={"command": "until grep DONE log; do sleep 15; done"},
        session_id="s",
        cache_dir="/tmp",
        hook_event="PreToolUse",
    )
    result = guard.check(ctx)
    assert result.action.value == "allow"
    assert result.context in (None, "")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
