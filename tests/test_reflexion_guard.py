"""Tests for concinno.guards.reflexion_guard module."""

from __future__ import annotations

from concinno.guards.base import GuardContext
from concinno.guards.reflexion_guard import (
    ReflexionGuard,
    build_failure_narrative,
)

# ── helpers ──────────────────────────────────────────────


def _ctx(
    *,
    tool_name: str = "Edit",
    tool_input: dict | None = None,
    tool_result: str = "",
    session_id: str = "reflexion-test-session",
    cache_dir: str = "",
    hook_event: str = "PostToolUse",
) -> GuardContext:
    return GuardContext(
        tool_name=tool_name,
        tool_input=tool_input or {},
        session_id=session_id,
        cache_dir=cache_dir,
        hook_event=hook_event,
        tool_result=tool_result,
    )


# ── build_failure_narrative ──────────────────────────────


def test_narrative_edit_old_string_not_found():
    n = build_failure_narrative(
        tool_name="Edit",
        error_sig="edit:old_string_not_found",
        file_path="src/foo.py",
    )
    assert "Reflexion" in n
    assert "Edit" in n
    assert "src/foo.py" in n
    assert "exact string" in n.lower() or "re-read" in n.lower()


def test_narrative_bash_tsc_with_code():
    n = build_failure_narrative(
        tool_name="Bash",
        error_sig="bash:tsc:TS2304",
    )
    assert "Reflexion" in n
    assert "TypeScript" in n


def test_narrative_unknown_signature_falls_back():
    n = build_failure_narrative(
        tool_name="Bash",
        error_sig="bash:weird:thing",
    )
    assert "Reflexion" in n
    # Unknown signature still yields a narrative (falls back generic).
    assert "blind-retry" in n or "new problem" in n


def test_narrative_empty_signature_returns_empty():
    n = build_failure_narrative(tool_name="Edit", error_sig="")
    assert n == ""


def test_narrative_word_cap_enforced():
    # Force a long fallback narrative and cap it tight.
    n = build_failure_narrative(
        tool_name="Bash",
        error_sig="bash:totally:novel:thing",
        max_words=5,
    )
    # 5 words capped + ellipsis suffix.
    assert n.endswith("...")
    # Word count of the prefix portion is ≤ 5.
    prefix = n.rstrip(".")
    assert len(prefix.split()) <= 5


# ── ReflexionGuard PostToolUse capture ───────────────────


def test_post_tool_captures_narrative_on_edit_failure(tmp_path):
    cache = str(tmp_path)
    g = ReflexionGuard()

    ctx = _ctx(
        tool_name="Edit",
        tool_input={"file_path": "src/x.py", "old_string": "bar"},
        tool_result="Error: old_string not found in file",
        cache_dir=cache,
    )
    res = g.on_post_tool(ctx)
    # PostToolUse should not return any context.
    assert res is None

    # State should now hold a narrative + ttl.
    from concinno.core.state_store import StateStore
    store = StateStore(cache)
    state = store.read("reflexion", ctx.session_id)
    assert state.get("why_failed", "")
    assert state.get("ttl_remaining", 0) >= 1
    assert state.get("error_sig", "").startswith("edit:")


def test_post_tool_no_action_on_clean_result(tmp_path):
    g = ReflexionGuard()
    ctx = _ctx(
        tool_name="Edit",
        tool_input={"file_path": "src/x.py"},
        tool_result="File edited successfully.",
        cache_dir=str(tmp_path),
    )
    assert g.on_post_tool(ctx) is None

    from concinno.core.state_store import StateStore
    state = StateStore(str(tmp_path)).read("reflexion", ctx.session_id)
    # No state written because no error signature.
    assert not state.get("why_failed", "")


def test_post_tool_skips_when_no_session_id(tmp_path):
    g = ReflexionGuard()
    ctx = _ctx(
        tool_name="Edit",
        tool_result="Error: old_string not found",
        cache_dir=str(tmp_path),
        session_id="",
    )
    # Should silently skip.
    assert g.on_post_tool(ctx) is None


# ── ReflexionGuard PreToolUse replay + TTL ───────────────


def test_pre_tool_replays_narrative_and_decrements_ttl(tmp_path):
    cache = str(tmp_path)
    g = ReflexionGuard(injection_ttl_calls=2)

    # First, capture a narrative via PostToolUse.
    fail_ctx = _ctx(
        tool_name="Edit",
        tool_input={"file_path": "src/y.py"},
        tool_result="old_string not found in file",
        cache_dir=cache,
    )
    g.on_post_tool(fail_ctx)

    # First PreToolUse → advisory injected, TTL decremented from 2 → 1.
    pre_ctx = _ctx(
        tool_name="Bash",
        cache_dir=cache,
        hook_event="PreToolUse",
    )
    res1 = g.check(pre_ctx)
    assert res1 is not None
    assert res1.advisory is True
    assert "Reflexion" in res1.context

    # Second PreToolUse → still injected, TTL decremented to 0 and cleared.
    res2 = g.check(pre_ctx)
    assert res2 is not None
    assert res2.advisory is True

    # Third PreToolUse → narrative expired.
    res3 = g.check(pre_ctx)
    assert res3 is None


def test_pre_tool_returns_none_with_no_state(tmp_path):
    g = ReflexionGuard()
    ctx = _ctx(
        tool_name="Bash",
        cache_dir=str(tmp_path),
        hook_event="PreToolUse",
    )
    assert g.check(ctx) is None


def test_init_clamps_out_of_range_params():
    # max_words below 30 → clamped to 30; ttl above 5 → clamped to 5.
    g = ReflexionGuard(max_words=5, injection_ttl_calls=99)
    # Internal members are private, exercise the clamp via behaviour.
    n = build_failure_narrative(
        tool_name="Bash",
        error_sig="bash:tsc:TS9999",
        max_words=g._max_words,  # noqa: SLF001 — private member access in test is fine
    )
    # max_words floor is 30, narrative is well under — should not be ellipsised.
    assert not n.endswith("...")
    assert g._ttl == 5  # noqa: SLF001
