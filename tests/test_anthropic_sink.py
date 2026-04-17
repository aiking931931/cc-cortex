"""Tests for concinno.cache.anthropic_sink — AnthropicCacheEditSink.

Covers: Protocol compliance, tool-result deletion, section edit/replace,
dry-run semantics, queue management, deep-copy safety, stats tracking.
"""

from __future__ import annotations

import copy

from concinno.cache.anthropic_sink import AnthropicCacheEditSink
from concinno.cache.microcompact import CacheEdit, SectionEdit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tool_messages(call_id: str, tool_content: str) -> list[dict]:
    """Build a minimal assistant→user tool_use/tool_result pair."""
    return [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": call_id,
                    "name": "Read",
                    "input": {"file_path": "/tmp/test.py"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": tool_content,
                }
            ],
        },
    ]


def _make_section_message(start: str, body: str, end: str) -> dict:
    """Build a user message with section markers in string content."""
    return {
        "role": "user",
        "content": f"preamble\n{start}\n{body}\n{end}\npostamble",
    }


def _make_section_block_message(start: str, body: str, end: str) -> dict:
    """Build a user message with section markers in a text block."""
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": f"preamble\n{start}\n{body}\n{end}\npostamble"}
        ],
    }


SECTION_START = "<!-- cp-section:abcd1234 title=test updated=100.0 -->"
SECTION_END = "<!-- /cp-section -->"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSubmitQueues:
    def test_submit_queues_edits(self) -> None:
        sink = AnthropicCacheEditSink()
        edit = CacheEdit(call_id="c1", action="delete_tool_result", reason="test")
        count = sink.submit([edit])
        assert count == 1
        assert sink.pending_count() == (1, 0)

    def test_submit_sections_queues(self) -> None:
        sink = AnthropicCacheEditSink()
        se = SectionEdit(
            section_id="abcd1234",
            action="delete_section",
            start_marker=SECTION_START,
            end_marker=SECTION_END,
        )
        count = sink.submit_sections([se])
        assert count == 1
        assert sink.pending_count() == (0, 1)


class TestToolEdits:
    def test_apply_tool_edits_replaces_content(self) -> None:
        sink = AnthropicCacheEditSink()
        msgs = _make_tool_messages("c1", "big result content here")
        edit = CacheEdit(call_id="c1")
        result = sink.apply_tool_edits(msgs, [edit])
        assert result.edits_applied == 1
        # The user message tool_result block should have replacement text
        user_msg = result.messages[1]
        block = user_msg["content"][0]
        assert block["content"] == "[tool result removed by microcompact]"

    def test_apply_tool_edits_missing_call_id_skipped(self) -> None:
        sink = AnthropicCacheEditSink()
        msgs = _make_tool_messages("c1", "content")
        edit = CacheEdit(call_id="nonexistent")
        result = sink.apply_tool_edits(msgs, [edit])
        assert result.edits_applied == 0
        # Original content unchanged
        block = result.messages[1]["content"][0]
        assert block["content"] == "content"

    def test_apply_tool_edits_counts_tokens_saved(self) -> None:
        sink = AnthropicCacheEditSink()
        content = "a" * 400  # 400 chars -> ~100 tokens
        msgs = _make_tool_messages("c1", content)
        edit = CacheEdit(call_id="c1")
        result = sink.apply_tool_edits(msgs, [edit])
        assert result.tokens_estimated_saved == 100

    def test_replacement_text_customizable(self) -> None:
        sink = AnthropicCacheEditSink(replacement_text="[GONE]")
        msgs = _make_tool_messages("c1", "data")
        result = sink.apply_tool_edits(msgs, [CacheEdit(call_id="c1")])
        block = result.messages[1]["content"][0]
        assert block["content"] == "[GONE]"


class TestSectionEdits:
    def test_apply_section_edits_delete(self) -> None:
        sink = AnthropicCacheEditSink()
        msg = _make_section_message(SECTION_START, "body text", SECTION_END)
        se = SectionEdit(
            section_id="abcd1234",
            action="delete_section",
            start_marker=SECTION_START,
            end_marker=SECTION_END,
        )
        result = sink.apply_section_edits([msg], [se])
        assert result.sections_applied == 1
        # Section should be gone, preamble and postamble remain
        content = result.messages[0]["content"]
        assert SECTION_START not in content
        assert SECTION_END not in content
        assert "preamble" in content
        assert "postamble" in content

    def test_apply_section_edits_replace(self) -> None:
        sink = AnthropicCacheEditSink()
        msg = _make_section_message(SECTION_START, "old body", SECTION_END)
        se = SectionEdit(
            section_id="abcd1234",
            action="replace_section",
            start_marker=SECTION_START,
            end_marker=SECTION_END,
            new_body="new body",
        )
        result = sink.apply_section_edits([msg], [se])
        assert result.sections_applied == 1
        content = result.messages[0]["content"]
        assert "new body" in content
        assert "old body" not in content
        # Markers should still be present
        assert SECTION_START in content
        assert SECTION_END in content

    def test_apply_section_edits_in_string_content(self) -> None:
        """Section markers in plain string content (not list-of-blocks)."""
        sink = AnthropicCacheEditSink()
        msg = _make_section_message(SECTION_START, "body", SECTION_END)
        se = SectionEdit(
            section_id="abcd1234",
            action="delete_section",
            start_marker=SECTION_START,
            end_marker=SECTION_END,
        )
        result = sink.apply_section_edits([msg], [se])
        assert result.sections_applied == 1
        assert isinstance(result.messages[0]["content"], str)

    def test_apply_section_edits_in_block_content(self) -> None:
        """Section markers in a text block within list content."""
        sink = AnthropicCacheEditSink()
        msg = _make_section_block_message(SECTION_START, "body", SECTION_END)
        se = SectionEdit(
            section_id="abcd1234",
            action="delete_section",
            start_marker=SECTION_START,
            end_marker=SECTION_END,
        )
        result = sink.apply_section_edits([msg], [se])
        assert result.sections_applied == 1


class TestApplyAll:
    def test_apply_all_combines_both(self) -> None:
        sink = AnthropicCacheEditSink()
        msgs = _make_tool_messages("c1", "tool data")
        section_msg = _make_section_message(SECTION_START, "sec body", SECTION_END)
        msgs.append(section_msg)

        te = CacheEdit(call_id="c1")
        se = SectionEdit(
            section_id="abcd1234",
            action="delete_section",
            start_marker=SECTION_START,
            end_marker=SECTION_END,
        )
        result = sink.apply_all(msgs, [te], [se])
        assert result.edits_applied == 1
        assert result.sections_applied == 1
        assert result.tokens_estimated_saved > 0


class TestDryRun:
    def test_dry_run_returns_original_messages(self) -> None:
        sink = AnthropicCacheEditSink(dry_run=True)
        msgs = _make_tool_messages("c1", "important data")
        original_msgs = msgs  # same reference
        result = sink.apply_tool_edits(msgs, [CacheEdit(call_id="c1")])
        # Messages should be the SAME object (not a copy)
        assert result.messages is original_msgs
        # Content should be UNCHANGED
        block = result.messages[1]["content"][0]
        assert block["content"] == "important data"

    def test_dry_run_still_counts_edits(self) -> None:
        sink = AnthropicCacheEditSink(dry_run=True)
        msgs = _make_tool_messages("c1", "x" * 200)
        result = sink.apply_tool_edits(msgs, [CacheEdit(call_id="c1")])
        assert result.edits_applied == 1
        assert result.tokens_estimated_saved == 50


class TestQueueManagement:
    def test_pending_count_accurate(self) -> None:
        sink = AnthropicCacheEditSink()
        sink.submit([CacheEdit(call_id="a"), CacheEdit(call_id="b")])
        sink.submit_sections([
            SectionEdit(
                section_id="x", action="delete_section",
                start_marker="<s>", end_marker="</s>",
            )
        ])
        assert sink.pending_count() == (2, 1)

    def test_clear_pending_empties_queues(self) -> None:
        sink = AnthropicCacheEditSink()
        sink.submit([CacheEdit(call_id="a")])
        sink.submit_sections([
            SectionEdit(
                section_id="x", action="delete_section",
                start_marker="<s>", end_marker="</s>",
            )
        ])
        sink.clear_pending()
        assert sink.pending_count() == (0, 0)

    def test_apply_consumes_pending_queue(self) -> None:
        sink = AnthropicCacheEditSink()
        sink.submit([CacheEdit(call_id="c1")])
        msgs = _make_tool_messages("c1", "data")
        # apply with edits=None -> consume queue
        result = sink.apply_tool_edits(msgs)
        assert result.edits_applied == 1
        assert sink.pending_count() == (0, 0)


class TestStats:
    def test_stats_tracks_totals(self) -> None:
        sink = AnthropicCacheEditSink()
        msgs = _make_tool_messages("c1", "x" * 80)
        sink.apply_tool_edits(msgs, [CacheEdit(call_id="c1")])
        s = sink.stats()
        assert s["total_tool_edits"] == 1
        assert s["total_tokens_saved"] == 20
        assert s["dry_runs"] == 0

    def test_stats_dry_run_counted(self) -> None:
        sink = AnthropicCacheEditSink(dry_run=True)
        msgs = _make_tool_messages("c1", "data")
        sink.apply_tool_edits(msgs, [CacheEdit(call_id="c1")])
        assert sink.stats()["dry_runs"] == 1


class TestDeepCopy:
    def test_deep_copy_does_not_mutate_input(self) -> None:
        sink = AnthropicCacheEditSink()
        msgs = _make_tool_messages("c1", "original content")
        original_copy = copy.deepcopy(msgs)
        sink.apply_tool_edits(msgs, [CacheEdit(call_id="c1")])
        # The INPUT messages should be UNCHANGED
        assert msgs == original_copy
