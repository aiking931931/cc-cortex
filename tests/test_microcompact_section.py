"""Tests for section-edit extension of concinno.cache.microcompact.

Covers the v1.16 additive surface on top of ``microcompact.py``:

- :class:`SectionEdit` dataclass shape and defaults.
- ``queue_section_replace`` / ``queue_section_delete`` queueing.
- ``pending_section_edits`` snapshot copy semantics.
- ``flush_sections`` with capable sink, legacy sink, no sink, and
  sink-exception retry.
- ``compact_all`` helper flushing both queues.
- ``stats()`` additive keys.
- save/load round-trip including a legacy state file missing the new
  ``pending_section_edits`` key (backwards compatibility).
- Smoke: existing tool-result flow still works after the extension.
- ``CacheEditAction`` literal alias carries the two new values.

Intentionally does **not** duplicate the 25 existing tests in
``test_microcompact.py`` — this file is additive, same as the code
under test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence, get_args

from concinno.cache.microcompact import (
    CacheEdit,
    CacheEditAction,
    Microcompactor,
    SectionEdit,
    compact_all,
)

# ---------------------------------------------------------------------------
# Section-id / marker fixtures (matches cognitive_pool constants literally —
# microcompact itself never parses these, so we only need representative
# opaque strings for round-trip assertions.)
# ---------------------------------------------------------------------------

SID_ALPHA = "a1b2c3d4"
SID_BETA = "e5f6a7b8"

MARKER_START_ALPHA = f"<!-- cp-section:{SID_ALPHA} -->"
MARKER_START_BETA = f"<!-- cp-section:{SID_BETA} -->"
MARKER_END = "<!-- /cp-section -->"


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeSectionSink:
    """Sink that understands both ``submit`` and ``submit_sections``.

    Mirrors the ``FakeSink`` in ``test_microcompact.py`` but adds the
    sibling method so the section-flush path can be exercised.
    """

    submissions: list[list[CacheEdit]] = field(default_factory=list)
    section_submissions: list[list[SectionEdit]] = field(default_factory=list)
    raise_on_submit: bool = False
    raise_on_sections: bool = False
    applied_override: int | None = None
    section_applied_override: int | None = None

    def submit(self, edits: Sequence[CacheEdit]) -> int:
        if self.raise_on_submit:
            raise RuntimeError("boom")
        self.submissions.append(list(edits))
        if self.applied_override is not None:
            return self.applied_override
        return len(edits)

    def submit_sections(self, edits: Sequence[SectionEdit]) -> int:
        if self.raise_on_sections:
            raise RuntimeError("section-boom")
        self.section_submissions.append(list(edits))
        if self.section_applied_override is not None:
            return self.section_applied_override
        return len(edits)


@dataclass
class LegacySink:
    """Sink that implements only the original ``submit``.

    Used to verify that ``flush_sections`` degrades gracefully when the
    host hasn't wired up section support yet.
    """

    submissions: list[list[CacheEdit]] = field(default_factory=list)

    def submit(self, edits: Sequence[CacheEdit]) -> int:
        self.submissions.append(list(edits))
        return len(edits)


def _make(
    tmp_path,
    *,
    sink=None,
    session_id: str = "secsess01",
) -> Microcompactor:
    return Microcompactor(
        cache_dir=str(tmp_path),
        session_id=session_id,
        time_based_ttl_s=10.0,
        token_budget_soft=100,
        token_budget_hard=200,
        sink=sink,
    )


def _queue_one(mc: Microcompactor, *, section_id: str = SID_ALPHA) -> SectionEdit:
    return mc.queue_section_replace(
        section_id=section_id,
        start_marker=f"<!-- cp-section:{section_id} -->",
        end_marker=MARKER_END,
        new_body="fresh body\n",
        reason="manual",
    )


# ---------------------------------------------------------------------------
# 1. Dataclass shape
# ---------------------------------------------------------------------------


def test_section_edit_dataclass_fields() -> None:
    """SectionEdit carries the 6 documented fields with correct defaults."""
    edit = SectionEdit(
        section_id=SID_ALPHA,
        action="replace_section",
        start_marker=MARKER_START_ALPHA,
        end_marker=MARKER_END,
    )
    assert edit.section_id == SID_ALPHA
    assert edit.action == "replace_section"
    assert edit.start_marker == MARKER_START_ALPHA
    assert edit.end_marker == MARKER_END
    # Optional fields default to empty string.
    assert edit.new_body == ""
    assert edit.reason == ""


# ---------------------------------------------------------------------------
# 2. Queue mechanics
# ---------------------------------------------------------------------------


def test_queue_section_replace_adds_to_pending(tmp_path) -> None:
    mc = _make(tmp_path)
    assert mc.stats()["section_edits_pending"] == 0

    mc.queue_section_replace(
        section_id=SID_ALPHA,
        start_marker=MARKER_START_ALPHA,
        end_marker=MARKER_END,
        new_body="body v2\n",
        reason="drift",
    )

    pending = mc.pending_section_edits()
    assert len(pending) == 1
    assert pending[0].action == "replace_section"
    assert pending[0].new_body == "body v2\n"
    assert pending[0].reason == "drift"
    assert mc.stats()["section_edits_pending"] == 1


def test_queue_section_delete_adds_to_pending(tmp_path) -> None:
    mc = _make(tmp_path)
    mc.queue_section_delete(
        section_id=SID_BETA,
        start_marker=MARKER_START_BETA,
        end_marker=MARKER_END,
        reason="stale",
    )

    pending = mc.pending_section_edits()
    assert len(pending) == 1
    assert pending[0].action == "delete_section"
    assert pending[0].section_id == SID_BETA
    # delete semantics: body forced empty regardless.
    assert pending[0].new_body == ""


def test_pending_section_edits_returns_snapshot_copy(tmp_path) -> None:
    """Mutating the returned list must not reach internal state."""
    mc = _make(tmp_path)
    _queue_one(mc)

    snapshot = mc.pending_section_edits()
    snapshot.clear()

    # Internal state is untouched.
    assert len(mc.pending_section_edits()) == 1
    assert mc.stats()["section_edits_pending"] == 1


def test_queue_section_replace_returns_edit_object(tmp_path) -> None:
    mc = _make(tmp_path)
    returned = mc.queue_section_replace(
        section_id=SID_ALPHA,
        start_marker=MARKER_START_ALPHA,
        end_marker=MARKER_END,
        new_body="hello",
    )
    assert isinstance(returned, SectionEdit)
    # The returned object is the same one sitting in the pending queue.
    assert mc.pending_section_edits()[0] is returned


# ---------------------------------------------------------------------------
# 3. Flush path
# ---------------------------------------------------------------------------


def test_flush_sections_calls_submit_sections_on_capable_sink(tmp_path) -> None:
    sink = FakeSectionSink()
    mc = _make(tmp_path, sink=sink)
    _queue_one(mc)

    applied = mc.flush_sections()

    assert applied == 1
    assert len(sink.section_submissions) == 1
    assert sink.section_submissions[0][0].section_id == SID_ALPHA
    # The capable sink's submit (tool-result) path was NOT touched.
    assert sink.submissions == []


def test_flush_sections_no_sink_returns_zero(tmp_path) -> None:
    mc = _make(tmp_path, sink=None)
    _queue_one(mc)
    assert mc.flush_sections() == 0
    # Queue preserved — host may attach a sink and retry later.
    assert mc.stats()["section_edits_pending"] == 1


def test_flush_sections_legacy_sink_warns_and_returns_zero(
    tmp_path, caplog
) -> None:
    """A sink without submit_sections degrades gracefully."""
    sink = LegacySink()
    mc = _make(tmp_path, sink=sink)
    _queue_one(mc)

    import logging as _logging

    with caplog.at_level(_logging.WARNING, logger="concinno.cache.microcompact"):
        applied = mc.flush_sections()

    assert applied == 0
    # Queue preserved.
    assert mc.stats()["section_edits_pending"] == 1
    # Legacy submit was not called — this is a section path, not tool path.
    assert sink.submissions == []
    # Warning emitted so hosts can notice misconfiguration.
    assert any(
        "submit_sections" in rec.message for rec in caplog.records
    )


def test_flush_sections_clears_queue_on_success(tmp_path) -> None:
    sink = FakeSectionSink()
    mc = _make(tmp_path, sink=sink)
    _queue_one(mc, section_id=SID_ALPHA)
    _queue_one(mc, section_id=SID_BETA)

    applied = mc.flush_sections()

    assert applied == 2
    assert mc.stats()["section_edits_pending"] == 0
    # Running total reflects the successful flush.
    assert mc.stats()["section_edits_applied_total"] == 2


def test_flush_sections_preserves_queue_on_sink_failure(tmp_path) -> None:
    sink = FakeSectionSink(raise_on_sections=True)
    mc = _make(tmp_path, sink=sink)
    _queue_one(mc)

    applied = mc.flush_sections()

    assert applied == 0
    # Queue preserved for retry.
    assert mc.stats()["section_edits_pending"] == 1
    assert mc.stats()["section_edits_applied_total"] == 0

    # Recover the sink and retry — state must still be consistent.
    sink.raise_on_sections = False
    applied2 = mc.flush_sections()
    assert applied2 == 1
    assert mc.stats()["section_edits_pending"] == 0
    assert mc.stats()["section_edits_applied_total"] == 1


# ---------------------------------------------------------------------------
# 4. compact_all helper
# ---------------------------------------------------------------------------


def test_compact_all_helper_flushes_both_queues(tmp_path) -> None:
    sink = FakeSectionSink()
    mc = _make(tmp_path, sink=sink)

    # Seed the tool-result queue via the documented smoke path.
    mc.register_tool_call(
        call_id="t1", tool_name="Read", input_hash="h", result_tokens=500
    )
    # Force the entry to be older than TTL so time-based queues it.
    for tc in mc.state.tool_calls:
        tc.timestamp = 0.0
    mc.evaluate_time_based_trigger(now=9999.0)
    assert mc.stats()["pending_edits"] == 1

    # Seed the section queue.
    _queue_one(mc)
    assert mc.stats()["section_edits_pending"] == 1

    tool_applied, section_applied = compact_all(mc)

    assert tool_applied == 1
    assert section_applied == 1
    assert mc.stats()["pending_edits"] == 0
    assert mc.stats()["section_edits_pending"] == 0
    # Both sink endpoints were exercised.
    assert len(sink.submissions) == 1
    assert len(sink.section_submissions) == 1


# ---------------------------------------------------------------------------
# 5. Stats keys
# ---------------------------------------------------------------------------


def test_stats_includes_section_edit_counters(tmp_path) -> None:
    mc = _make(tmp_path)
    s = mc.stats()
    # New keys exist and start at zero.
    assert "section_edits_pending" in s
    assert "section_edits_applied_total" in s
    assert s["section_edits_pending"] == 0
    assert s["section_edits_applied_total"] == 0
    # Pre-existing keys still present.
    for k in (
        "tool_calls_total",
        "tool_calls_deleted",
        "pending_edits",
        "tokens_reclaimed_estimate",
    ):
        assert k in s


# ---------------------------------------------------------------------------
# 6. Persistence round-trip + legacy compatibility
# ---------------------------------------------------------------------------


def test_save_load_roundtrip_with_pending_section_edits(tmp_path) -> None:
    mc1 = _make(tmp_path, session_id="rt01")
    mc1.queue_section_replace(
        section_id=SID_ALPHA,
        start_marker=MARKER_START_ALPHA,
        end_marker=MARKER_END,
        new_body="persisted body\n",
        reason="persist-test",
    )
    mc1.queue_section_delete(
        section_id=SID_BETA,
        start_marker=MARKER_START_BETA,
        end_marker=MARKER_END,
        reason="persist-delete",
    )
    mc1.save()

    mc2 = _make(tmp_path, session_id="rt01")
    mc2.load()

    restored = mc2.pending_section_edits()
    assert len(restored) == 2
    by_id = {e.section_id: e for e in restored}
    assert by_id[SID_ALPHA].action == "replace_section"
    assert by_id[SID_ALPHA].new_body == "persisted body\n"
    assert by_id[SID_ALPHA].reason == "persist-test"
    assert by_id[SID_BETA].action == "delete_section"
    assert by_id[SID_BETA].new_body == ""


def test_load_legacy_state_without_section_edits_key(tmp_path) -> None:
    """A state file written by pre-1.16 must load cleanly."""
    # Manually write a v1.15-shaped payload directly to the state store
    # path so we exercise the defaulting branch in load().
    from concinno.core.state_store import StateStore

    store = StateStore(str(tmp_path))
    legacy_payload = {
        "tool_calls": [
            {
                "call_id": "t1",
                "tool_name": "Read",
                "input_hash": "h",
                "result_tokens": 500,
                "timestamp": 1.0,
                "result_deleted": False,
            }
        ],
        "pending_edits": [
            {
                "call_id": "t1",
                "action": "delete_tool_result",
                "reason": "manual",
            }
        ],
        "last_assistant_ts": 42.0,
        "main_thread_only": True,
        # Note: no pending_section_edits, no section_edits_applied_total
    }
    store.write("microcompact", "legacy01", legacy_payload)
    # Sanity check our synthetic file is actually on disk as dict.
    raw = store.read("microcompact", "legacy01", default={})
    assert isinstance(raw, dict)
    assert "pending_section_edits" not in raw

    mc = _make(tmp_path, session_id="legacy01")
    mc.load()

    # Tool-call side restored intact.
    assert len(mc.state.tool_calls) == 1
    assert len(mc.state.pending_edits) == 1
    # New fields defaulted.
    assert mc.pending_section_edits() == []
    assert mc.stats()["section_edits_pending"] == 0
    assert mc.stats()["section_edits_applied_total"] == 0


# ---------------------------------------------------------------------------
# 7. Ordering semantics
# ---------------------------------------------------------------------------


def test_multiple_section_edits_same_id_dedupe_or_allow(tmp_path) -> None:
    """Policy: multiple pending edits on the same section id are ALLOWED
    and applied in insertion order on flush.

    We intentionally do NOT dedupe in the queue — callers that need
    last-writer-wins semantics should dedupe upstream before queueing.
    This keeps microcompact's responsibility surface minimal: it owns
    queue-and-dispatch, not conflict resolution.
    """
    sink = FakeSectionSink()
    mc = _make(tmp_path, sink=sink)

    mc.queue_section_replace(
        section_id=SID_ALPHA,
        start_marker=MARKER_START_ALPHA,
        end_marker=MARKER_END,
        new_body="v1",
        reason="first",
    )
    mc.queue_section_replace(
        section_id=SID_ALPHA,
        start_marker=MARKER_START_ALPHA,
        end_marker=MARKER_END,
        new_body="v2",
        reason="second",
    )

    assert mc.stats()["section_edits_pending"] == 2

    applied = mc.flush_sections()

    assert applied == 2
    submitted = sink.section_submissions[0]
    # Insertion order preserved.
    assert [e.new_body for e in submitted] == ["v1", "v2"]
    assert [e.reason for e in submitted] == ["first", "second"]


# ---------------------------------------------------------------------------
# 8. Backwards-compat smoke
# ---------------------------------------------------------------------------


def test_existing_tool_result_flow_unchanged(tmp_path) -> None:
    """Smoke: delete-tool-result path still works end-to-end."""
    sink = FakeSectionSink()
    mc = _make(tmp_path, sink=sink)
    mc.register_tool_call(
        call_id="t1", tool_name="Read", input_hash="h", result_tokens=500
    )
    for tc in mc.state.tool_calls:
        tc.timestamp = 0.0

    edits = mc.evaluate_time_based_trigger(now=9999.0)
    assert len(edits) == 1
    assert edits[0].action == "delete_tool_result"

    applied = mc.flush()
    assert applied == 1
    assert mc.state.tool_calls[0].result_deleted is True
    # CacheEdit still constructible with the original 3-arg keyword form.
    ce = CacheEdit(call_id="x", action="delete_tool_result", reason="r")
    assert ce.action == "delete_tool_result"


def test_cache_edit_action_literal_includes_new_values() -> None:
    """The widened Literal alias is the module-level source of truth."""
    members = set(get_args(CacheEditAction))
    assert "delete_tool_result" in members
    assert "delete_section" in members
    assert "replace_section" in members
    assert len(members) == 3
