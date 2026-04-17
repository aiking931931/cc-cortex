"""Tests for concinno.cache.microcompact.

Covers:

- COMPACTABLE_TOOLS set parity and immutability.
- Fork-agent guard and non-compactable tool filtering.
- Time-based trigger (fires, idempotent, TTL boundary).
- Token-budget trigger (soft, hard more aggressive, under-budget no-op).
- Flush (sink call, mark on success, preserve on failure, empty, no-sink).
- Stats, save/load round-trip, dedup across triggers.
- compact_if_needed integration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import pytest

from concinno.cache.microcompact import (
    COMPACTABLE_TOOLS,
    TIME_BASED_MC_CLEARED_MESSAGE,
    CacheEdit,
    Microcompactor,
    compact_if_needed,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeSink:
    """Record every submission; return ``len(edits)`` by default."""

    submissions: list[list[CacheEdit]] = field(default_factory=list)
    raise_on_submit: bool = False
    applied_override: int | None = None

    def submit(self, edits: Sequence[CacheEdit]) -> int:
        if self.raise_on_submit:
            raise RuntimeError("boom")
        self.submissions.append(list(edits))
        if self.applied_override is not None:
            return self.applied_override
        return len(edits)


def _make(
    tmp_path,
    *,
    sink: FakeSink | None = None,
    is_main_thread: bool = True,
    ttl: float = 10.0,
    soft: int = 100,
    hard: int = 200,
    session_id: str = "abcdef0123",
) -> Microcompactor:
    return Microcompactor(
        cache_dir=str(tmp_path),
        session_id=session_id,
        time_based_ttl_s=ttl,
        token_budget_soft=soft,
        token_budget_hard=hard,
        is_main_thread=is_main_thread,
        sink=sink,
    )


def _register_at(mc: Microcompactor, call_id: str, *, tokens: int, ts: float) -> None:
    """Register a call and then rewrite its timestamp for deterministic tests."""
    mc.register_tool_call(
        call_id=call_id,
        tool_name="Read",
        input_hash=f"h-{call_id}",
        result_tokens=tokens,
    )
    for tc in mc.state.tool_calls:
        if tc.call_id == call_id:
            tc.timestamp = ts
            return


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_compactable_tools_set_contains_canon() -> None:
    expected = {
        "Read",
        "Bash",
        "Grep",
        "Glob",
        "WebSearch",
        "WebFetch",
        "Edit",
        "Write",
    }
    assert expected <= COMPACTABLE_TOOLS


def test_compactable_tools_immutable() -> None:
    assert isinstance(COMPACTABLE_TOOLS, frozenset)
    with pytest.raises(AttributeError):
        COMPACTABLE_TOOLS.add("Task")  # type: ignore[attr-defined]


def test_time_based_cleared_message_is_sentinel() -> None:
    # Host code depends on this exact string for parity with TS.
    assert "microcompact" in TIME_BASED_MC_CLEARED_MESSAGE
    assert TIME_BASED_MC_CLEARED_MESSAGE.startswith("[")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_register_non_compactable_is_noop(tmp_path) -> None:
    mc = _make(tmp_path)
    mc.register_tool_call(
        call_id="t1", tool_name="TodoWrite", input_hash="h", result_tokens=500
    )
    assert mc.stats()["tool_calls_total"] == 0


def test_register_fork_thread_is_noop(tmp_path) -> None:
    mc = _make(tmp_path, is_main_thread=False)
    mc.register_tool_call(
        call_id="t1", tool_name="Read", input_hash="h", result_tokens=500
    )
    assert mc.stats()["tool_calls_total"] == 0


def test_register_duplicate_call_id_idempotent(tmp_path) -> None:
    mc = _make(tmp_path)
    for _ in range(3):
        mc.register_tool_call(
            call_id="t1", tool_name="Read", input_hash="h", result_tokens=500
        )
    assert mc.stats()["tool_calls_total"] == 1


def test_note_assistant_message_updates_timestamp(tmp_path) -> None:
    mc = _make(tmp_path)
    mc.note_assistant_message(timestamp=12345.0)
    assert mc.state.last_assistant_ts == 12345.0


# ---------------------------------------------------------------------------
# Time-based trigger
# ---------------------------------------------------------------------------


def test_time_based_trigger_queues_old_results(tmp_path) -> None:
    mc = _make(tmp_path, ttl=10.0)
    _register_at(mc, "t1", tokens=500, ts=0.0)
    _register_at(mc, "t2", tokens=500, ts=5.0)

    edits = mc.evaluate_time_based_trigger(now=20.0)

    assert len(edits) == 2
    assert {e.call_id for e in edits} == {"t1", "t2"}
    assert all(e.reason == "time_based" for e in edits)


def test_time_based_trigger_idempotent(tmp_path) -> None:
    mc = _make(tmp_path, ttl=10.0)
    _register_at(mc, "t1", tokens=500, ts=0.0)

    first = mc.evaluate_time_based_trigger(now=20.0)
    second = mc.evaluate_time_based_trigger(now=30.0)

    assert len(first) == 1
    assert second == []
    assert mc.stats()["pending_edits"] == 1


def test_time_based_trigger_respects_ttl_boundary(tmp_path) -> None:
    mc = _make(tmp_path, ttl=10.0)
    _register_at(mc, "young", tokens=500, ts=95.0)  # age = 5s, not old
    _register_at(mc, "old", tokens=500, ts=80.0)    # age = 20s, old

    edits = mc.evaluate_time_based_trigger(now=100.0)

    assert len(edits) == 1
    assert edits[0].call_id == "old"


# ---------------------------------------------------------------------------
# Token-budget trigger
# ---------------------------------------------------------------------------


def test_token_budget_trigger_under_budget_noop(tmp_path) -> None:
    mc = _make(tmp_path, soft=100, hard=200)
    _register_at(mc, "t1", tokens=50, ts=1.0)
    assert mc.evaluate_token_budget_trigger(current_tokens=80) == []
    assert mc.stats()["pending_edits"] == 0


def test_token_budget_soft_trigger(tmp_path) -> None:
    mc = _make(tmp_path, soft=100, hard=200)
    _register_at(mc, "old", tokens=40, ts=1.0)
    _register_at(mc, "mid", tokens=40, ts=2.0)
    _register_at(mc, "new", tokens=40, ts=3.0)

    edits = mc.evaluate_token_budget_trigger(current_tokens=130)

    # 130 - 40 = 90 <= soft(100). One eviction suffices; oldest first.
    assert len(edits) == 1
    assert edits[0].call_id == "old"
    assert edits[0].reason == "token_budget"


def test_token_budget_hard_more_aggressive_than_soft(tmp_path) -> None:
    mc_soft = _make(tmp_path, soft=100, hard=200, session_id="aaaaaaaa")
    mc_hard = _make(tmp_path, soft=100, hard=200, session_id="bbbbbbbb")
    for mc in (mc_soft, mc_hard):
        _register_at(mc, "t1", tokens=30, ts=1.0)
        _register_at(mc, "t2", tokens=30, ts=2.0)
        _register_at(mc, "t3", tokens=30, ts=3.0)
        _register_at(mc, "t4", tokens=30, ts=4.0)

    # Soft path (between soft and hard) — trim to soft (100).
    soft_edits = mc_soft.evaluate_token_budget_trigger(current_tokens=150)
    # Hard path (over hard) — trim to soft - (hard - soft) = 0.
    hard_edits = mc_hard.evaluate_token_budget_trigger(current_tokens=250)

    assert len(hard_edits) > len(soft_edits)


def test_time_and_token_triggers_dedupe_same_call_id(tmp_path) -> None:
    mc = _make(tmp_path, ttl=10.0, soft=100, hard=200)
    _register_at(mc, "t1", tokens=500, ts=0.0)

    time_edits = mc.evaluate_time_based_trigger(now=100.0)
    token_edits = mc.evaluate_token_budget_trigger(current_tokens=600)

    assert len(time_edits) == 1
    assert token_edits == []
    assert mc.stats()["pending_edits"] == 1


# ---------------------------------------------------------------------------
# Flush
# ---------------------------------------------------------------------------


def test_flush_calls_sink_with_pending_edits(tmp_path) -> None:
    sink = FakeSink()
    mc = _make(tmp_path, sink=sink, ttl=1.0)
    _register_at(mc, "t1", tokens=500, ts=0.0)
    mc.evaluate_time_based_trigger(now=100.0)

    applied = mc.flush()

    assert applied == 1
    assert len(sink.submissions) == 1
    assert sink.submissions[0][0].call_id == "t1"


def test_flush_marks_results_deleted_on_success(tmp_path) -> None:
    sink = FakeSink()
    mc = _make(tmp_path, sink=sink, ttl=1.0)
    _register_at(mc, "t1", tokens=500, ts=0.0)
    mc.evaluate_time_based_trigger(now=100.0)

    mc.flush()

    assert mc.state.tool_calls[0].result_deleted is True
    assert mc.state.pending_edits == []


def test_flush_preserves_edits_on_sink_failure(tmp_path) -> None:
    sink = FakeSink(raise_on_submit=True)
    mc = _make(tmp_path, sink=sink, ttl=1.0)
    _register_at(mc, "t1", tokens=500, ts=0.0)
    mc.evaluate_time_based_trigger(now=100.0)

    applied = mc.flush()

    assert applied == 0
    assert mc.state.tool_calls[0].result_deleted is False
    assert len(mc.state.pending_edits) == 1


def test_sink_exception_does_not_mark_deleted(tmp_path) -> None:
    sink = FakeSink(raise_on_submit=True)
    mc = _make(tmp_path, sink=sink, ttl=1.0)
    _register_at(mc, "t1", tokens=500, ts=0.0)
    mc.evaluate_time_based_trigger(now=100.0)

    mc.flush()
    # Retry once with a working sink — state must still be consistent.
    sink.raise_on_submit = False
    applied = mc.flush()

    assert applied == 1
    assert mc.state.tool_calls[0].result_deleted is True


def test_flush_no_sink_returns_zero(tmp_path) -> None:
    mc = _make(tmp_path, sink=None, ttl=1.0)
    _register_at(mc, "t1", tokens=500, ts=0.0)
    mc.evaluate_time_based_trigger(now=100.0)

    assert mc.flush() == 0
    assert mc.stats()["pending_edits"] == 1  # unchanged


def test_flush_empty_pending_returns_zero(tmp_path) -> None:
    sink = FakeSink()
    mc = _make(tmp_path, sink=sink)
    assert mc.flush() == 0
    assert sink.submissions == []


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


def test_stats_tracks_totals_and_reclaim(tmp_path) -> None:
    sink = FakeSink()
    mc = _make(tmp_path, sink=sink, ttl=1.0)
    _register_at(mc, "t1", tokens=500, ts=0.0)
    _register_at(mc, "t2", tokens=300, ts=0.0)
    mc.evaluate_time_based_trigger(now=100.0)

    # Pre-flush stats.
    pre = mc.stats()
    assert pre["tool_calls_total"] == 2
    assert pre["tool_calls_deleted"] == 0
    assert pre["pending_edits"] == 2
    assert pre["tokens_reclaimed_estimate"] == 0

    mc.flush()
    post = mc.stats()
    assert post["tool_calls_deleted"] == 2
    assert post["pending_edits"] == 0
    assert post["tokens_reclaimed_estimate"] == 800


def test_tokens_reclaimed_estimate_matches_deleted_tokens(tmp_path) -> None:
    sink = FakeSink()
    mc = _make(tmp_path, sink=sink, ttl=1.0)
    _register_at(mc, "t1", tokens=123, ts=0.0)
    _register_at(mc, "t2", tokens=456, ts=0.0)
    mc.evaluate_time_based_trigger(now=100.0)
    mc.flush()
    assert mc.stats()["tokens_reclaimed_estimate"] == 123 + 456


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_save_load_roundtrip(tmp_path) -> None:
    mc1 = _make(tmp_path, session_id="sess0001")
    _register_at(mc1, "t1", tokens=500, ts=10.0)
    _register_at(mc1, "t2", tokens=600, ts=20.0)
    mc1.note_assistant_message(timestamp=99.0)
    mc1.evaluate_time_based_trigger(now=1000.0)
    mc1.save()

    mc2 = _make(tmp_path, session_id="sess0001")
    mc2.load()

    assert len(mc2.state.tool_calls) == 2
    assert {tc.call_id for tc in mc2.state.tool_calls} == {"t1", "t2"}
    assert len(mc2.state.pending_edits) == 2
    assert mc2.state.last_assistant_ts == 99.0


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------


def test_compact_if_needed_runs_both_triggers(tmp_path) -> None:
    sink = FakeSink()
    mc = _make(tmp_path, sink=sink, ttl=10.0, soft=100, hard=200)
    _register_at(mc, "old", tokens=40, ts=0.0)   # time-based candidate
    _register_at(mc, "mid", tokens=40, ts=50.0)  # not time-old (age=10 at now=60)
    _register_at(mc, "new", tokens=40, ts=55.0)

    applied = compact_if_needed(mc, now=60.0, current_tokens=130)

    assert applied >= 1
    assert len(sink.submissions) == 1
    submitted_ids = {e.call_id for e in sink.submissions[0]}
    assert "old" in submitted_ids


def test_hard_budget_constructor_validation(tmp_path) -> None:
    with pytest.raises(ValueError):
        Microcompactor(
            cache_dir=str(tmp_path),
            session_id="x",
            token_budget_soft=100,
            token_budget_hard=50,
        )
