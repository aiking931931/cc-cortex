"""Tests for intent_anchor_guard — Preserve original task intent."""

from concinno.core.state_store import StateStore
from concinno.guards.base import GuardAction, GuardContext
from concinno.intent_anchor_guard import (
    IntentAnchorGuard,
    _extract_intent,
)


def _ctx(
    tmp_path,
    tool_name="Edit",
    tool_input=None,
    *,
    hook_event="PreToolUse",
    tool_result="",
    session_id="test-session",
):
    return GuardContext(
        tool_name=tool_name,
        tool_input=tool_input or {"file_path": "test.md", "new_string": "content"},
        session_id=session_id,
        cache_dir=str(tmp_path),
        hook_event=hook_event,
        tool_result=tool_result,
        workspace="",
    )


# ── _extract_intent ────────────────────────────────────────────────────────────


class TestExtractIntent:
    def test_truncates_at_sentence_boundary(self):
        text = "Build a REST API. Add authentication. Deploy to cloud."
        result = _extract_intent(text, max_len=30)
        assert result.endswith(".")
        assert len(result) <= 30

    def test_returns_empty_for_empty_input(self):
        assert _extract_intent("") == ""

    def test_short_text_returned_as_is(self):
        text = "Build it."
        assert _extract_intent(text) == "Build it."

    def test_truncates_chinese_at_sentence(self):
        text = "實作一個 REST API。加入驗證功能。部署到雲端。"
        result = _extract_intent(text, max_len=20)
        assert "。" in result or len(result) <= 20


# ── IntentAnchorGuard.check() ─────────────────────────────────────────────────


class TestIntentAnchorGuardCheck:
    def test_returns_none_when_no_intent_stored(self, tmp_path):
        guard = IntentAnchorGuard()
        ctx = _ctx(tmp_path)
        assert guard.check(ctx) is None

    def test_returns_none_when_write_count_not_at_interval(self, tmp_path):
        guard = IntentAnchorGuard()
        store = StateStore(str(tmp_path))
        store.write("intent_anchor", "test-session", {
            "intent": "Build a REST API.",
            "write_count": 0,
        })
        ctx = _ctx(tmp_path)
        result = guard.check(ctx)
        # write_count goes 0+1=1, not at interval 15
        assert result is None

    def test_injects_at_interval(self, tmp_path):
        guard = IntentAnchorGuard()
        store = StateStore(str(tmp_path))
        # write_count=14 so next call (14+1=15) hits interval
        store.write("intent_anchor", "test-session", {
            "intent": "Build a REST API.",
            "write_count": 14,
        })
        ctx = _ctx(tmp_path)
        result = guard.check(ctx)
        assert result is not None
        assert result.action == GuardAction.ALLOW
        assert "原始意圖" in result.context

    def test_no_cache_dir_returns_none(self):
        guard = IntentAnchorGuard()
        ctx = GuardContext(
            tool_name="Edit",
            tool_input={"file_path": "f.md", "new_string": "x"},
            session_id="s",
            cache_dir="",
            hook_event="PreToolUse",
        )
        assert guard.check(ctx) is None

    def test_force_inject_triggers_regardless_of_count(self, tmp_path):
        guard = IntentAnchorGuard()
        store = StateStore(str(tmp_path))
        store.write("intent_anchor", "test-session", {
            "intent": "Original goal.",
            "write_count": 3,
            "force_inject": True,
            "force_reason": "紅隊完成",
        })
        ctx = _ctx(tmp_path)
        result = guard.check(ctx)
        assert result is not None
        assert result.action == GuardAction.ALLOW


# ── IntentAnchorGuard.on_post_tool() ──────────────────────────────────────────


class TestIntentAnchorGuardOnPostTool:
    def test_captures_intent_from_user_prompt_namespace(self, tmp_path):
        guard = IntentAnchorGuard()
        store = StateStore(str(tmp_path))
        # Simulate prompt_submit hook writing user prompt to StateStore
        store.write("user_prompt", "test-session", {
            "last_prompt": "Build a complete REST API with auth and rate limiting.",
        })
        ctx = _ctx(
            tmp_path,
            tool_name="Read",
            tool_input={"file_path": "prompt.md"},
            hook_event="PostToolUse",
            tool_result="file content here",
        )
        guard.on_post_tool(ctx)
        state = store.read("intent_anchor", "test-session", default={})
        assert state.get("intent")
        assert "REST API" in state["intent"]

    def test_detects_direction_change_from_user_prompt(self, tmp_path):
        guard = IntentAnchorGuard()
        store = StateStore(str(tmp_path))
        store.write("intent_anchor", "test-session", {
            "intent": "Old goal.",
            "write_count": 10,
        })
        # Simulate user changing direction via new prompt
        store.write("user_prompt", "test-session", {
            "last_prompt": "actually, let's do something different. Build a GraphQL API instead.",
        })
        ctx = _ctx(
            tmp_path,
            tool_name="Read",
            tool_input={"file_path": "x.md"},
            hook_event="PostToolUse",
            tool_result="file content",
        )
        guard.on_post_tool(ctx)
        state = store.read("intent_anchor", "test-session", default={})
        assert state.get("recaptured") is True
        assert state.get("write_count") == 0

    def test_sets_force_inject_on_redteam_signal(self, tmp_path):
        guard = IntentAnchorGuard()
        store = StateStore(str(tmp_path))
        store.write("intent_anchor", "test-session", {"intent": "Goal."})
        ctx = _ctx(
            tmp_path,
            tool_name="Agent",
            tool_input={"prompt": "run red team"},
            hook_event="PostToolUse",
            tool_result="紅隊分析完成，發現 3 個弱點",
        )
        guard.on_post_tool(ctx)
        state = store.read("intent_anchor", "test-session", default={})
        assert state.get("force_inject") is True

    def test_no_cache_dir_returns_none(self):
        guard = IntentAnchorGuard()
        ctx = GuardContext(
            tool_name="Read",
            tool_input={"file_path": "x.md"},
            session_id="s",
            cache_dir="",
            hook_event="PostToolUse",
            tool_result="some text",
        )
        assert guard.on_post_tool(ctx) is None
