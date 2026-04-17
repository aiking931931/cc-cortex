"""concinno.cache.anthropic_sink — Concrete CacheEditSink implementation.

@module cache.anthropic_sink
@responsibility First concrete implementation of the CacheEditSink Protocol.
    Transforms Anthropic-format message lists by removing tool_result blocks
    and replacing/deleting cognitive-pool sections. Does NOT make API calls —
    it produces a transformed message list that the caller sends to the
    Anthropic API with proper cache_control markers.

    This closes the red-team FATAL finding that CacheEditSink was a Protocol
    with zero implementations anywhere in the codebase.

@dependencies concinno.cache.microcompact (CacheEdit, CacheEditSink, SectionEdit)
@exports AnthropicCacheEditSink, MessageTransform
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

from concinno.cache.microcompact import CacheEdit, SectionEdit

logger = logging.getLogger("concinno.cache.anthropic_sink")


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------


@dataclass
class MessageTransform:
    """The result of applying cache edits to a message list.

    Attributes:
        messages: Transformed message list (deep copy of the original,
            with edits applied). When ``dry_run=True``, this is a
            reference to the original list (unchanged).
        edits_applied: Number of tool-result edits that matched and
            were applied (or would have been, in dry-run mode).
        sections_applied: Number of section edits that matched and
            were applied (or would have been).
        tokens_estimated_saved: Rough token savings estimate. Uses
            ``len(content) // 4`` for removed content — simple but
            sufficient for budget decisions.
        details: Human-readable log of what changed, one entry per
            applied edit.
    """

    messages: list[dict[str, Any]]
    edits_applied: int = 0
    sections_applied: int = 0
    tokens_estimated_saved: int = 0
    details: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Concrete sink
# ---------------------------------------------------------------------------


class AnthropicCacheEditSink:
    """Concrete CacheEditSink that transforms message lists.

    Removes tool_result blocks and replaces/deletes cognitive-pool
    sections in Anthropic-format message lists. This is a message-list
    transformer, not an API caller — the caller owns the
    ``anthropic.Anthropic`` client and sends the transformed messages.

    Implements the ``CacheEditSink`` Protocol from
    ``concinno.cache.microcompact``.

    Usage::

        sink = AnthropicCacheEditSink()
        transform = sink.apply_all(messages, tool_edits, section_edits)
        # caller sends transform.messages to Anthropic API
    """

    def __init__(
        self,
        *,
        replacement_text: str = "[tool result removed by microcompact]",
        dry_run: bool = False,
    ) -> None:
        self._replacement_text = replacement_text
        self._dry_run = dry_run

        # Internal queues for Protocol submit/submit_sections
        self._pending_tool_edits: list[CacheEdit] = []
        self._pending_section_edits: list[SectionEdit] = []

        # Lifetime stats
        self._total_tool_edits = 0
        self._total_section_edits = 0
        self._total_tokens_saved = 0
        self._dry_runs = 0

    # ── CacheEditSink Protocol compliance ─────────────────────

    def submit(self, edits: Sequence[CacheEdit]) -> int:
        """Protocol method. Queue edits for the next apply call.

        Returns count queued.
        """
        added = list(edits)
        self._pending_tool_edits.extend(added)
        return len(added)

    def submit_sections(self, edits: Sequence[SectionEdit]) -> int:
        """Protocol method for section edits. Queue for next apply call.

        Returns count queued.
        """
        added = list(edits)
        self._pending_section_edits.extend(added)
        return len(added)

    # ── Transform API (the real work) ─────────────────────────

    def apply_tool_edits(
        self,
        messages: list[dict[str, Any]],
        edits: Sequence[CacheEdit] | None = None,
    ) -> MessageTransform:
        """Apply tool-result deletions to a message list.

        For each CacheEdit with action="delete_tool_result":
        - Find user-role messages containing a content block with
          ``type=="tool_result"`` and matching ``tool_use_id``.
        - Replace that block's ``content`` with ``replacement_text``.
        - Estimate tokens saved: ``len(original_content) // 4``.

        If ``edits`` is None, consumes and clears the pending queue.
        If ``dry_run``, computes the transform but returns the original
        messages unchanged.
        """
        consume_queue = edits is None
        work_edits = list(self._pending_tool_edits) if consume_queue else list(edits)  # type: ignore[arg-type]

        # Build the working copy
        work_messages = messages if self._dry_run else copy.deepcopy(messages)

        result = MessageTransform(messages=work_messages)

        # Index edits by call_id for O(n) scan
        target_ids: dict[str, CacheEdit] = {}
        for edit in work_edits:
            if edit.action == "delete_tool_result":
                target_ids[edit.call_id] = edit

        if not target_ids:
            if consume_queue:
                self._pending_tool_edits.clear()
            return result

        for msg in work_messages:
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "tool_result":
                    continue
                tool_use_id = block.get("tool_use_id", "")
                if tool_use_id not in target_ids:
                    continue

                # Found a match — measure and replace
                original = block.get("content", "")
                original_str = original if isinstance(original, str) else str(original)
                tokens_saved = len(original_str) // 4

                if not self._dry_run:
                    block["content"] = self._replacement_text

                result.edits_applied += 1
                result.tokens_estimated_saved += tokens_saved
                result.details.append(
                    f"tool_result {tool_use_id}: "
                    f"replaced {len(original_str)} chars "
                    f"(~{tokens_saved} tokens)"
                )

                # Remove from targets so we don't double-match
                del target_ids[tool_use_id]

        # Update lifetime stats
        self._total_tool_edits += result.edits_applied
        self._total_tokens_saved += result.tokens_estimated_saved
        if self._dry_run:
            self._dry_runs += 1

        # Consume queue if applicable
        if consume_queue:
            self._pending_tool_edits.clear()

        return result

    def apply_section_edits(
        self,
        messages: list[dict[str, Any]],
        edits: Sequence[SectionEdit] | None = None,
    ) -> MessageTransform:
        """Apply section deletions/replacements to message content.

        For each SectionEdit, scans all message content (both string
        and list-of-blocks) for ``start_marker...end_marker`` ranges
        and deletes or replaces the section body.
        """
        consume_queue = edits is None
        work_edits = list(self._pending_section_edits) if consume_queue else list(edits)  # type: ignore[arg-type]

        work_messages = messages if self._dry_run else copy.deepcopy(messages)
        result = MessageTransform(messages=work_messages)

        if not work_edits:
            if consume_queue:
                self._pending_section_edits.clear()
            return result

        for edit in work_edits:
            applied = self._apply_one_section_edit(work_messages, edit, result)
            if applied:
                result.sections_applied += 1

        # Update lifetime stats
        self._total_section_edits += result.sections_applied
        self._total_tokens_saved += result.tokens_estimated_saved
        if self._dry_run:
            self._dry_runs += 1

        if consume_queue:
            self._pending_section_edits.clear()

        return result

    def _apply_one_section_edit(
        self,
        messages: list[dict[str, Any]],
        edit: SectionEdit,
        result: MessageTransform,
    ) -> bool:
        """Apply a single section edit across all messages. Returns True if matched."""
        found = False
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, str):
                new_content, did_edit, tokens = self._edit_section_in_string(
                    content, edit
                )
                if did_edit:
                    found = True
                    result.tokens_estimated_saved += tokens
                    if not self._dry_run:
                        msg["content"] = new_content
                    result.details.append(
                        f"section {edit.section_id} "
                        f"({edit.action}): ~{tokens} tokens"
                    )
            elif isinstance(content, list):
                hit = self._edit_section_in_blocks(content, edit, result)
                found = found or hit
        return found

    def _edit_section_in_blocks(
        self,
        blocks: list[Any],
        edit: SectionEdit,
        result: MessageTransform,
    ) -> bool:
        """Scan list-of-block content for section markers. Returns True if matched."""
        found = False
        for block in blocks:
            if not isinstance(block, dict):
                continue
            text = block.get("text", "")
            if not isinstance(text, str):
                continue
            new_text, did_edit, tokens = self._edit_section_in_string(text, edit)
            if not did_edit:
                continue
            found = True
            result.tokens_estimated_saved += tokens
            if not self._dry_run:
                block["text"] = new_text
            result.details.append(
                f"section {edit.section_id} "
                f"({edit.action}): ~{tokens} tokens"
            )
        return found

    def _edit_section_in_string(
        self,
        text: str,
        edit: SectionEdit,
    ) -> tuple[str, bool, int]:
        """Find start_marker...end_marker in text and apply the edit.

        Returns (new_text, did_edit, tokens_saved).
        """
        start_idx = text.find(edit.start_marker)
        if start_idx == -1:
            return text, False, 0

        end_idx = text.find(edit.end_marker, start_idx + len(edit.start_marker))
        if end_idx == -1:
            return text, False, 0

        # The full range including both markers
        full_end = end_idx + len(edit.end_marker)
        original_section = text[start_idx:full_end]
        tokens_saved = len(original_section) // 4

        if edit.action == "delete_section":
            # Remove the entire range (markers + body)
            new_text = text[:start_idx] + text[full_end:]
        elif edit.action == "replace_section":
            # Keep markers, replace body between them
            new_text = (
                text[:start_idx]
                + edit.start_marker
                + "\n"
                + edit.new_body
                + "\n"
                + edit.end_marker
                + text[full_end:]
            )
            # Adjust tokens saved: we saved the old but added the new
            new_section_len = (
                len(edit.start_marker) + 1 + len(edit.new_body) + 1 + len(edit.end_marker)
            )
            tokens_saved = max(0, (len(original_section) - new_section_len) // 4)
        else:
            return text, False, 0

        return new_text, True, tokens_saved

    def apply_all(
        self,
        messages: list[dict[str, Any]],
        tool_edits: Sequence[CacheEdit] | None = None,
        section_edits: Sequence[SectionEdit] | None = None,
    ) -> MessageTransform:
        """Apply both tool and section edits in one pass.

        Tool edits are applied first, then section edits on the
        already-transformed messages.
        """
        # Deep copy once, then apply both in sequence
        work_messages = messages if self._dry_run else copy.deepcopy(messages)

        # Apply tool edits (pass the work copy, force non-dry-run
        # deep-copy off since we already copied)
        saved_dry = self._dry_run
        self._dry_run = True  # prevent double-copy inside sub-calls

        tool_result = self.apply_tool_edits(work_messages, tool_edits)
        section_result = self.apply_section_edits(
            tool_result.messages, section_edits
        )

        self._dry_run = saved_dry

        # If not dry_run, the mutations happened on work_messages
        # (since we set dry_run=True, sub-calls skipped deep-copy
        # and mutated in place). If actual dry_run, we return original.
        final_messages = messages if saved_dry else work_messages

        return MessageTransform(
            messages=final_messages,
            edits_applied=tool_result.edits_applied,
            sections_applied=section_result.sections_applied,
            tokens_estimated_saved=(
                tool_result.tokens_estimated_saved
                + section_result.tokens_estimated_saved
            ),
            details=tool_result.details + section_result.details,
        )

    # ── Queue management ──────────────────────────────────────

    def pending_count(self) -> tuple[int, int]:
        """Return (pending_tool_edits, pending_section_edits)."""
        return len(self._pending_tool_edits), len(self._pending_section_edits)

    def clear_pending(self) -> None:
        """Clear both pending queues."""
        self._pending_tool_edits.clear()
        self._pending_section_edits.clear()

    def stats(self) -> dict[str, int]:
        """Return lifetime stats.

        Keys: total_tool_edits, total_section_edits,
        total_tokens_saved, dry_runs.
        """
        return {
            "total_tool_edits": self._total_tool_edits,
            "total_section_edits": self._total_section_edits,
            "total_tokens_saved": self._total_tokens_saved,
            "dry_runs": self._dry_runs,
        }
