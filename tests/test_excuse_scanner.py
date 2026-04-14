"""Tests for cc_cortex.excuse_scanner — Butterfly Effect excuse detector."""

from __future__ import annotations

from cc_cortex.excuse_scanner import (
    ExcuseHit,
    _build_block_reason,
    _collect_hits,
    _extract_message_text,
    _extract_snippet,
    _extract_tool_names,
    _has_fix_after,
    on_stop,
    scan_excuses,
)

# ── _extract_message_text ──────────────────────────────────


class TestExtractMessageText:
    def test_string_content(self):
        msg = {"content": "hello world"}
        assert _extract_message_text(msg) == "hello world"

    def test_list_content(self):
        msg = {"content": [
            {"type": "text", "text": "line 1"},
            {"type": "tool_use", "name": "Edit"},
            {"type": "text", "text": "line 2"},
        ]}
        assert "line 1" in _extract_message_text(msg)
        assert "line 2" in _extract_message_text(msg)

    def test_empty(self):
        assert _extract_message_text({}) == ""

    def test_non_string_content(self):
        msg = {"content": 42}
        assert _extract_message_text(msg) == ""


# ── _extract_snippet ───────────────────────────────────────


class TestExtractSnippet:
    def test_basic(self):
        import re
        text = "This is a test of the snippet extraction function here"
        match = re.search(r"snippet", text)
        result = _extract_snippet(text, match, radius=10)
        assert "snippet" in result

    def test_start_ellipsis(self):
        import re
        text = "x" * 100 + "TARGET" + "y" * 100
        match = re.search(r"TARGET", text)
        result = _extract_snippet(text, match, radius=5)
        assert result.startswith("...")

    def test_end_ellipsis(self):
        import re
        text = "TARGET" + "y" * 100
        match = re.search(r"TARGET", text)
        result = _extract_snippet(text, match, radius=5)
        assert result.endswith("...")


# ── _extract_tool_names ───────────────────────────────────


class TestExtractToolNames:
    def test_with_name_field(self):
        content = [{"name": "Edit"}, {"name": "Read"}]
        assert _extract_tool_names(content) == ["Edit", "Read"]

    def test_with_tool_name_field(self):
        content = [{"tool_name": "Write"}]
        assert _extract_tool_names(content) == ["Write"]

    def test_skips_non_dict(self):
        content = ["string", {"name": "Edit"}, 42]
        assert _extract_tool_names(content) == ["Edit"]


# ── _has_fix_after ─────────────────────────────────────────


class TestHasFixAfter:
    def test_edit_after(self):
        messages = [
            {"role": "assistant", "content": "excuse"},
            {"role": "assistant", "content": [{"name": "Edit"}]},
        ]
        assert _has_fix_after(messages, 0) is True

    def test_write_after(self):
        messages = [
            {"role": "assistant", "content": "excuse"},
            {"role": "assistant", "content": [{"name": "Write"}]},
        ]
        assert _has_fix_after(messages, 0) is True

    def test_no_fix_after(self):
        messages = [
            {"role": "assistant", "content": "excuse"},
            {"role": "assistant", "content": [{"name": "Read"}]},
        ]
        assert _has_fix_after(messages, 0) is False

    def test_fix_before_not_counted(self):
        messages = [
            {"role": "assistant", "content": [{"name": "Edit"}]},
            {"role": "assistant", "content": "excuse"},
        ]
        assert _has_fix_after(messages, 1) is False

    def test_string_content_with_tool_name(self):
        messages = [
            {"role": "assistant", "content": "excuse"},
            {"role": "assistant", "content": "I used Edit to fix it"},
        ]
        assert _has_fix_after(messages, 0) is True

    def test_skips_user_messages(self):
        messages = [
            {"role": "assistant", "content": "excuse"},
            {"role": "user", "content": [{"name": "Edit"}]},
        ]
        assert _has_fix_after(messages, 0) is False


# ── _collect_hits ──────────────────────────────────────────


class TestCollectHits:
    def test_zh_excuse(self):
        messages = [
            {"role": "assistant", "content": "這個問題不是我造成的，先跳過"},
        ]
        hits = _collect_hits(messages)
        assert len(hits) == 1
        assert not hits[0].resolved

    def test_en_excuse(self):
        messages = [
            {"role": "assistant", "content": "This is a pre-existing issue, skipping"},
        ]
        hits = _collect_hits(messages)
        assert len(hits) == 1

    def test_resolved_excuse(self):
        messages = [
            {"role": "assistant", "content": "不是我造成的問題"},
            {"role": "assistant", "content": [{"name": "Edit"}]},
        ]
        hits = _collect_hits(messages)
        assert len(hits) == 1
        assert hits[0].resolved is True

    def test_no_excuse(self):
        messages = [
            {"role": "assistant", "content": "Fixed the bug successfully"},
        ]
        hits = _collect_hits(messages)
        assert len(hits) == 0

    def test_skips_user_messages(self):
        messages = [
            {"role": "user", "content": "不是我造成的"},
        ]
        hits = _collect_hits(messages)
        assert len(hits) == 0

    def test_one_hit_per_message(self):
        messages = [
            {"role": "assistant", "content": "不是我造成的，先跳過，暫時忽略"},
        ]
        hits = _collect_hits(messages)
        assert len(hits) == 1  # Only first match per message


# ── _build_block_reason ────────────────────────────────────


class TestBuildBlockReason:
    def test_empty(self):
        assert _build_block_reason([]) == ""

    def test_single(self):
        hits = [ExcuseHit(0, "pattern", "snippet context")]
        reason = _build_block_reason(hits)
        assert "Butterfly Effect" in reason
        assert "snippet context" in reason
        assert "1 unresolved" in reason

    def test_max_three_examples(self):
        hits = [
            ExcuseHit(i, "p", f"snippet_{i}")
            for i in range(5)
        ]
        reason = _build_block_reason(hits)
        assert "snippet_0" in reason
        assert "snippet_2" in reason
        assert "snippet_4" not in reason  # Only top 3


# ── scan_excuses ───────────────────────────────────────────


class TestScanExcuses:
    def test_none_input(self):
        result = scan_excuses(None)
        assert not result.should_block
        assert result.unresolved_count == 0

    def test_empty_messages(self):
        result = scan_excuses({"messages": []})
        assert not result.should_block

    def test_unresolved_blocks(self):
        result = scan_excuses({"messages": [
            {"role": "assistant", "content": "這個 lint error 不是我造成的"},
        ]})
        assert result.should_block
        assert result.unresolved_count == 1
        assert "Butterfly Effect" in result.block_reason

    def test_resolved_passes(self):
        result = scan_excuses({"messages": [
            {"role": "assistant", "content": "不是我造成的問題，但我來修"},
            {"role": "assistant", "content": [{"name": "Edit"}]},
        ]})
        assert not result.should_block
        assert result.unresolved_count == 0

    def test_mixed_resolved_and_unresolved(self):
        result = scan_excuses({"messages": [
            {"role": "assistant", "content": "pre-existing issue here"},
            {"role": "assistant", "content": [{"name": "Edit"}]},
            {"role": "assistant", "content": "also out of scope for now"},
        ]})
        assert result.should_block
        assert result.unresolved_count == 1
        assert len(result.hits) == 2

    def test_clean_conversation(self):
        result = scan_excuses({"messages": [
            {"role": "user", "content": "fix the bug"},
            {"role": "assistant", "content": "Done, fixed the bug."},
        ]})
        assert not result.should_block


# ── on_stop ────────────────────────────────────────────────


class TestOnStop:
    def test_returns_block_string(self):
        result = on_stop({"messages": [
            {"role": "assistant", "content": "先不處理這個 lint error"},
        ]})
        assert result is not None
        assert result.startswith("EXCUSE_BLOCK:")

    def test_returns_none_when_clean(self):
        result = on_stop({"messages": [
            {"role": "assistant", "content": "All done!"},
        ]})
        assert result is None

    def test_returns_none_when_resolved(self):
        result = on_stop({"messages": [
            {"role": "assistant", "content": "not caused by me but fixing anyway"},
            {"role": "assistant", "content": [{"name": "Edit"}]},
        ]})
        assert result is None
