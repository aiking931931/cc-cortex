"""Tests for concinno.thinking_depth_guard — Read:Edit ratio monitor."""

from __future__ import annotations

import tempfile

from concinno.core.state_store import StateStore
from concinno.thinking_depth_guard import (
    MIN_EDITS,
    READ_EDIT_WARN,
    ThinkingDepthGuard,
    _record_tool,
    check_read_edit_ratio,
)


def _make_store():
    """Create a temp StateStore."""
    return StateStore(tempfile.mkdtemp())


class TestRecordTool:
    def test_records_tool(self):
        store = _make_store()
        _record_tool(store, "s1", "Read")
        state = store.read("thinking_depth", "s1", default={})
        assert len(state["calls"]) == 1
        assert state["calls"][0]["tool"] == "Read"

    def test_multiple_records(self):
        store = _make_store()
        for tool in ["Read", "Edit", "Grep", "Write"]:
            _record_tool(store, "s1", tool)
        state = store.read("thinking_depth", "s1", default={})
        assert len(state["calls"]) == 4


class TestCheckReadEditRatio:
    def test_no_calls(self):
        store = _make_store()
        ratio, reads, edits = check_read_edit_ratio(store, "s1")
        assert ratio == float("inf")
        assert reads == 0
        assert edits == 0

    def test_only_reads(self):
        store = _make_store()
        for _ in range(5):
            _record_tool(store, "s1", "Read")
        ratio, reads, edits = check_read_edit_ratio(store, "s1")
        assert ratio == float("inf")
        assert reads == 5
        assert edits == 0

    def test_only_edits(self):
        store = _make_store()
        for _ in range(3):
            _record_tool(store, "s1", "Edit")
        ratio, reads, edits = check_read_edit_ratio(store, "s1")
        assert ratio == 0.0
        assert reads == 0
        assert edits == 3

    def test_healthy_ratio(self):
        store = _make_store()
        # 6 reads, 1 edit = 6:1 (healthy)
        for _ in range(6):
            _record_tool(store, "s1", "Read")
        _record_tool(store, "s1", "Edit")
        ratio, reads, edits = check_read_edit_ratio(store, "s1")
        assert ratio >= 6.0

    def test_degraded_ratio(self):
        store = _make_store()
        # 2 reads, 3 edits = 0.67:1 (degraded)
        for _ in range(2):
            _record_tool(store, "s1", "Read")
        for _ in range(3):
            _record_tool(store, "s1", "Edit")
        ratio, reads, edits = check_read_edit_ratio(store, "s1")
        assert ratio < READ_EDIT_WARN

    def test_grep_counts_as_read(self):
        store = _make_store()
        for _ in range(4):
            _record_tool(store, "s1", "Grep")
        _record_tool(store, "s1", "Edit")
        ratio, reads, edits = check_read_edit_ratio(store, "s1")
        assert reads == 4
        assert edits == 1

    def test_write_counts_as_edit(self):
        store = _make_store()
        _record_tool(store, "s1", "Read")
        _record_tool(store, "s1", "Write")
        ratio, reads, edits = check_read_edit_ratio(store, "s1")
        assert edits == 1


class TestThinkingDepthGuard:
    def _make_ctx(self, tool_name, cache_dir, session_id="test"):
        """Create a minimal GuardContext-like object."""
        from concinno.guards.base import GuardContext

        return GuardContext(
            tool_name=tool_name,
            tool_input={},
            cache_dir=cache_dir,
            session_id=session_id,
            hook_event="PostToolUse",
        )

    def test_no_warning_on_read(self):
        guard = ThinkingDepthGuard()
        cache = tempfile.mkdtemp()
        ctx = self._make_ctx("Read", cache)
        result = guard.check(ctx)
        assert result is None

    def test_no_warning_few_edits(self):
        guard = ThinkingDepthGuard()
        cache = tempfile.mkdtemp()
        store = StateStore(cache)
        # Only 1 edit — below MIN_EDITS
        _record_tool(store, "test", "Edit")
        ctx = self._make_ctx("Edit", cache)
        result = guard.check(ctx)
        assert result is None

    def test_warning_on_low_ratio(self):
        guard = ThinkingDepthGuard()
        cache = tempfile.mkdtemp()
        store = StateStore(cache)
        # 1 read, then many edits
        _record_tool(store, "test", "Read")
        for _ in range(MIN_EDITS):
            _record_tool(store, "test", "Edit")
        ctx = self._make_ctx("Edit", cache)
        result = guard.check(ctx)
        assert result is not None
        assert "Read:Edit" in result.context

    def test_no_warning_healthy_ratio(self):
        guard = ThinkingDepthGuard()
        cache = tempfile.mkdtemp()
        store = StateStore(cache)
        # 20 reads, 3 edits = 6.67:1 (healthy)
        for _ in range(20):
            _record_tool(store, "test", "Read")
        for _ in range(MIN_EDITS - 1):
            _record_tool(store, "test", "Edit")
        ctx = self._make_ctx("Edit", cache)
        result = guard.check(ctx)
        assert result is None

    def test_warn_zero_reads(self):
        """0 reads + edits should still trigger WARN (no CRITICAL tier
        since 2026-04-13; WARN is the sole ratio signal)."""
        guard = ThinkingDepthGuard()
        cache = tempfile.mkdtemp()
        store = StateStore(cache)
        # 0 reads, many edits = 0:1
        for _ in range(MIN_EDITS + 2):
            _record_tool(store, "test", "Edit")
        ctx = self._make_ctx("Edit", cache)
        result = guard.check(ctx)
        assert result is not None
        assert "Read:Edit" in result.context

    def test_no_cache_dir_passes(self):
        guard = ThinkingDepthGuard()
        ctx = self._make_ctx("Edit", "")
        result = guard.check(ctx)
        assert result is None


class TestSidNormalization:
    """Regression test for the split-store-key bug.

    Two callers wrote tool records under different session_id formats:
      - guards/pipeline.py used ``ctx.session_id`` (full UUID, e.g.
        ``d5e6b534-0e8e-4f23-af8b-cbbdd6226fea``)
      - hooks/on_post_tool.py used ``_resolve_session_id()[:12]``
        (truncated to 12 chars, e.g. ``d5e6b534-0e8``)

    Because StateStore hashes the session_id with blake2b, the two
    forms hashed to two different filenames. Result: Read/Glob/Grep
    records (from pipeline.py) and Edit/Write records (from
    on_post_tool.py) lived in separate stores. The ratio check ran
    against one store and saw zero of the other — a permanent
    "Read:Edit = 0.0:1, reasoning shallow" false positive on every
    Edit, regardless of how many reads the session had performed.

    Fix: ``_normalize_sid`` collapses any session_id to the same
    12-char key inside _record_tool / check_read_edit_ratio, so all
    callers route to one store regardless of input format.
    """

    def test_full_uuid_and_truncated_share_window(self):
        store = _make_store()
        full = "d5e6b534-0e8e-4f23-af8b-cbbdd6226fea"
        trunc = "d5e6b534-0e8"  # what _resolve_session_id()[:12] returns

        # pipeline.py path: records reads with full UUID
        for _ in range(5):
            _record_tool(store, full, "Read")
        for _ in range(3):
            _record_tool(store, full, "Grep")

        # on_post_tool.py path: records edits with truncated id
        for _ in range(3):
            _record_tool(store, trunc, "Edit")

        # Reading with either form must see ALL 11 calls (8R + 3E).
        for sid in (full, trunc):
            ratio, reads, edits = check_read_edit_ratio(store, sid)
            assert reads == 8, f"sid={sid!r} lost reads"
            assert edits == 3, f"sid={sid!r} lost edits"
            # 8 reads / 3 edits ≈ 2.67, above WARN threshold (2.0)
            assert ratio > READ_EDIT_WARN

    def test_unknown_normalizes_to_unknown(self):
        store = _make_store()
        _record_tool(store, "", "Read")
        _record_tool(store, None, "Read")  # type: ignore[arg-type]
        # Both empty/None should land on the same "unknown" bucket.
        ratio, reads, edits = check_read_edit_ratio(store, "")
        assert reads == 2

    def test_short_sid_preserved(self):
        # A pre-truncated id shorter than the key length must still work.
        store = _make_store()
        _record_tool(store, "abc", "Read")
        _record_tool(store, "abc", "Edit")
        _record_tool(store, "abc", "Edit")
        _record_tool(store, "abc", "Edit")
        ratio, reads, edits = check_read_edit_ratio(store, "abc")
        assert reads == 1
        assert edits == 3
