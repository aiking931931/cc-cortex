"""Tests for concinno.stop_guard — premature stop detection."""

from __future__ import annotations

from concinno.stop_guard import (
    _already_blocked_this_session,
    _extract_last_assistant_text,
    _has_pending_tool_use,
    _is_declaration,
    _match_any,
    _record_block,
    classify_stop,
    on_stop,
)

# ---------------------------------------------------------------------------
# _match_any
# ---------------------------------------------------------------------------


class TestMatchAny:
    def test_case_insensitive(self):
        assert _match_any("Task DONE and finished", ["done", "finished"]) == ["done", "finished"]

    def test_no_match(self):
        assert _match_any("hello world", ["goodbye"]) == []

    def test_empty_patterns(self):
        assert _match_any("anything", []) == []

    def test_empty_text(self):
        assert _match_any("", ["done"]) == []


# ---------------------------------------------------------------------------
# _extract_last_assistant_text
# ---------------------------------------------------------------------------


class TestExtractLastAssistant:
    def test_string_content(self):
        data = {"messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "done"},
        ]}
        assert _extract_last_assistant_text(data) == "done"

    def test_list_content(self):
        data = {"messages": [
            {"role": "assistant", "content": [
                {"type": "text", "text": "part1"},
                {"type": "text", "text": "part2"},
            ]},
        ]}
        assert "part1" in _extract_last_assistant_text(data)

    def test_no_assistant(self):
        data = {"messages": [{"role": "user", "content": "hi"}]}
        assert _extract_last_assistant_text(data) == ""

    def test_empty_messages(self):
        assert _extract_last_assistant_text({}) == ""

    def test_picks_last_assistant(self):
        data = {"messages": [
            {"role": "assistant", "content": "first"},
            {"role": "user", "content": "ok"},
            {"role": "assistant", "content": "second"},
        ]}
        assert _extract_last_assistant_text(data) == "second"


# ---------------------------------------------------------------------------
# classify_stop
# ---------------------------------------------------------------------------


class TestClassifyStop:
    def test_none_returns_unknown(self):
        r = classify_stop(None)
        assert r.category == "unknown"
        assert r.premature is False

    def test_no_messages_returns_unknown(self):
        r = classify_stop({"messages": []})
        assert r.category == "unknown"

    def test_clean_with_completion(self):
        data = {"messages": [
            {"role": "assistant", "content": "全部完成，交接已寫好。"},
        ]}
        r = classify_stop(data)
        assert r.category == "clean"
        assert r.premature is False

    def test_continuation_is_premature(self):
        data = {"messages": [
            {"role": "assistant", "content": "接下來我要處理第二個任務"},
        ]}
        r = classify_stop(data)
        assert r.category == "continuation"
        assert r.premature is True
        assert len(r.signals) > 0

    def test_pending_is_premature(self):
        data = {"messages": [
            {"role": "assistant", "content": "還有 ⬜ 三個待辦沒做"},
        ]}
        r = classify_stop(data)
        assert r.category == "pending"
        assert r.premature is True

    def test_question_is_not_premature(self):
        data = {"messages": [
            {"role": "assistant", "content": "要繼續嗎？"},
        ]}
        r = classify_stop(data)
        assert r.category == "question"
        assert r.premature is False

    def test_completion_overrides_pending(self):
        """Completion keyword present alongside pending → clean."""
        data = {"messages": [
            {"role": "assistant", "content": "全部完成。之前的待辦已處理完畢。"},
        ]}
        r = classify_stop(data)
        assert r.category == "clean"

    def test_completion_overrides_continuation(self):
        data = {"messages": [
            {"role": "assistant", "content": "交接寫好了。接下來我建議新對話處理。"},
        ]}
        r = classify_stop(data)
        assert r.category == "clean"


# ---------------------------------------------------------------------------
# on_stop (hook entry)
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    def test_record_and_check(self, tmp_path, monkeypatch):
        state_file = str(tmp_path / "block.json")
        monkeypatch.setattr("concinno.stop_guard._BLOCK_STATE_PATH", state_file)
        assert not _already_blocked_this_session("s1")
        _record_block("s1")
        assert _already_blocked_this_session("s1")

    def test_different_session_not_blocked(self, tmp_path, monkeypatch):
        state_file = str(tmp_path / "block.json")
        monkeypatch.setattr("concinno.stop_guard._BLOCK_STATE_PATH", state_file)
        _record_block("s1")
        assert not _already_blocked_this_session("s2")

    def test_expired_not_blocked(self, tmp_path, monkeypatch):
        state_file = str(tmp_path / "block.json")
        monkeypatch.setattr("concinno.stop_guard._BLOCK_STATE_PATH", state_file)
        monkeypatch.setattr("concinno.stop_guard._BLOCK_COOLDOWN_S", 0.01)
        _record_block("s1")
        import time
        time.sleep(0.02)
        assert not _already_blocked_this_session("s1")

    def test_missing_file_not_blocked(self):
        assert not _already_blocked_this_session("any")

    def test_empty_session_id_not_blocked(self, tmp_path, monkeypatch):
        state_file = str(tmp_path / "block.json")
        monkeypatch.setattr("concinno.stop_guard._BLOCK_STATE_PATH", state_file)
        _record_block("s1")
        assert not _already_blocked_this_session("")


class TestOnStop:
    def test_clean_returns_none(self):
        data = {"messages": [
            {"role": "assistant", "content": "All tasks done and finished."},
        ]}
        assert on_stop(data) is None

    def test_continuation_returns_block(self, tmp_path, monkeypatch):
        state_file = str(tmp_path / "block.json")
        monkeypatch.setattr("concinno.stop_guard._BLOCK_STATE_PATH", state_file)
        data = {
            "session_id": "test-cont",
            "messages": [
                {"role": "assistant", "content": "let me continue with the next step"},
            ],
        }
        result = on_stop(data)
        assert result is not None
        assert result.startswith("STOP_BLOCK:")

    def test_continuation_second_time_downgrades_to_warn(self, tmp_path, monkeypatch):
        state_file = str(tmp_path / "block.json")
        monkeypatch.setattr("concinno.stop_guard._BLOCK_STATE_PATH", state_file)
        data = {
            "session_id": "test-2nd",
            "messages": [
                {"role": "assistant", "content": "let me continue with the next step"},
            ],
        }
        first = on_stop(data)
        assert first.startswith("STOP_BLOCK:")
        second = on_stop(data)
        assert not second.startswith("STOP_BLOCK:")  # Downgraded to warn

    def test_pending_returns_warn_not_block(self):
        data = {"messages": [
            {"role": "assistant", "content": "還有 ⬜ 三個待辦沒做"},
        ]}
        result = on_stop(data)
        assert result is not None
        assert not result.startswith("STOP_BLOCK:")

    def test_declaration_with_tool_use_is_premature(
        self, tmp_path, monkeypatch,
    ):
        """Short declarative output + recent tool calls = premature."""
        state_file = str(tmp_path / "block.json")
        monkeypatch.setattr(
            "concinno.stop_guard._BLOCK_STATE_PATH", state_file,
        )
        data = {
            "session_id": "test-decl",
            "messages": [
                {"role": "assistant", "content": [
                    {"type": "tool_use", "name": "Edit", "id": "x"},
                ]},
                {"role": "tool", "content": "ok"},
                {"role": "assistant", "content": "Nesting 是 JSX 巢狀。"},
            ],
        }
        result = on_stop(data)
        assert result is not None
        assert result.startswith("STOP_BLOCK:")

    def test_declaration_without_tool_use_is_not_premature(self):
        """Short output but no tool calls → not premature."""
        data = {"messages": [
            {"role": "assistant", "content": "了解。"},
        ]}
        result = on_stop(data)
        assert result is None

    def test_declaration_question_is_not_premature(self):
        """Short output ending with ? → question, not declaration."""
        data = {"messages": [
            {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Read", "id": "y"},
            ]},
            {"role": "tool", "content": "ok"},
            {"role": "assistant", "content": "要繼續嗎？"},
        ]}
        r = classify_stop(data)
        assert r.category == "question"


class TestDeclarationHelpers:
    def test_short_statement_is_declaration(self):
        assert _is_declaration("Hook 報的問題是 pre-existing。")

    def test_question_is_not_declaration(self):
        assert not _is_declaration("要繼續嗎？")

    def test_long_text_is_not_declaration(self):
        assert not _is_declaration("x" * 301)

    def test_interactive_is_not_declaration(self):
        assert not _is_declaration("需要我幫你處理這個嗎")

    def test_empty_is_not_declaration(self):
        assert not _is_declaration("")

    def test_has_tool_use_true(self):
        data = {"messages": [
            {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Edit", "id": "a"},
            ]},
        ]}
        assert _has_pending_tool_use(data)

    def test_has_tool_use_false(self):
        data = {"messages": [
            {"role": "assistant", "content": "just text"},
        ]}
        assert not _has_pending_tool_use(data)
