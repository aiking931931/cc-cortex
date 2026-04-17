"""Tests for concinno.cache.session_memory."""

from __future__ import annotations

from pathlib import Path

import pytest

from concinno.cache.session_memory import (
    DEFAULT_INIT_TOOL_COUNT,
    DEFAULT_MAX_MD_BYTES,
    DEFAULT_MAX_MD_LINES,
    DEFAULT_MD_FILENAME,
    DEFAULT_UPDATE_TOOL_COUNT,
    DistillInput,
    DistillOutput,
    SessionMemory,
)

# ---------------------------------------------------------------------------
# Fake sink
# ---------------------------------------------------------------------------


class FakeDistillSink:
    """Stateful test sink that records calls and returns scripted output."""

    def __init__(
        self,
        *,
        markdown: str = "# session\n\nhello world\n",
        success: bool = True,
        error: str | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self.calls: list[DistillInput] = []
        self._markdown = markdown
        self._success = success
        self._error = error
        self._raise_exc = raise_exc

    def distill(self, inp: DistillInput) -> DistillOutput:
        self.calls.append(inp)
        if self._raise_exc is not None:
            raise self._raise_exc
        return DistillOutput(
            markdown=self._markdown,
            success=self._success,
            error=self._error,
        )


def _make(
    tmp_path: Path,
    **overrides,
) -> SessionMemory:
    """Factory that points cache_dir at tmp_path with sensible defaults."""
    kwargs: dict = {
        "cache_dir": str(tmp_path),
        "session_id": "testsession",
    }
    kwargs.update(overrides)
    return SessionMemory(**kwargs)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_init_default_thresholds(tmp_path: Path) -> None:
    sm = _make(tmp_path)
    assert sm.init_threshold == DEFAULT_INIT_TOOL_COUNT
    assert sm.update_threshold == DEFAULT_UPDATE_TOOL_COUNT
    assert sm.max_bytes == DEFAULT_MAX_MD_BYTES
    assert sm.max_lines == DEFAULT_MAX_MD_LINES
    assert sm.md_path().name == DEFAULT_MD_FILENAME


def test_init_rejects_non_positive_thresholds(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _make(tmp_path, init_threshold=0)
    with pytest.raises(ValueError):
        _make(tmp_path, update_threshold=-1)
    with pytest.raises(ValueError):
        _make(tmp_path, max_bytes=0)
    with pytest.raises(ValueError):
        _make(tmp_path, max_lines=-5)


# ---------------------------------------------------------------------------
# record_tool_call
# ---------------------------------------------------------------------------


def test_record_tool_call_increments_count(tmp_path: Path) -> None:
    sm = _make(tmp_path)
    sm.record_tool_call(tool_name="Read", summary="read foo.py")
    sm.record_tool_call(tool_name="Edit", summary="edit bar.py")
    assert sm.tool_count_total == 2


def test_record_tool_call_appends_to_ring_buffer(tmp_path: Path) -> None:
    sm = _make(tmp_path)
    sm.record_tool_call(tool_name="Read", summary="read foo.py")
    sm.record_tool_call(tool_name="Bash", summary="ls -la")
    events = list(sm._events)  # type: ignore[attr-defined]
    assert events == ["Read: read foo.py", "Bash: ls -la"]


def test_ring_buffer_bounded_at_50(tmp_path: Path) -> None:
    sm = _make(tmp_path)
    for i in range(60):
        sm.record_tool_call(tool_name="Read", summary=f"call {i}")
    events = list(sm._events)  # type: ignore[attr-defined]
    assert len(events) == 50
    # first 10 should be dropped → the earliest retained is "call 10"
    assert events[0] == "Read: call 10"
    assert events[-1] == "Read: call 59"
    # tool_count_total still tracks all 60
    assert sm.tool_count_total == 60


# ---------------------------------------------------------------------------
# should_update
# ---------------------------------------------------------------------------


def test_should_update_false_below_init_threshold(tmp_path: Path) -> None:
    sm = _make(tmp_path, init_threshold=5)
    for _ in range(4):
        sm.record_tool_call(tool_name="Read", summary="x")
    assert sm.should_update() is False


def test_should_update_true_at_init_threshold_when_no_md(tmp_path: Path) -> None:
    sm = _make(tmp_path, init_threshold=5)
    for _ in range(5):
        sm.record_tool_call(tool_name="Read", summary="x")
    assert sm.should_update() is True


def test_should_update_false_after_init_before_update_threshold(
    tmp_path: Path,
) -> None:
    sink = FakeDistillSink()
    sm = _make(tmp_path, init_threshold=3, update_threshold=10, sink=sink)
    for _ in range(3):
        sm.record_tool_call(tool_name="Read", summary="x")
    out = sm.update()
    assert out is not None and out.success
    # 3 calls happened; 0 new since last update → below update threshold
    assert sm.should_update() is False
    # Add 5 more → still below update_threshold=10
    for _ in range(5):
        sm.record_tool_call(tool_name="Read", summary="y")
    assert sm.should_update() is False


def test_should_update_true_after_update_threshold(tmp_path: Path) -> None:
    sink = FakeDistillSink()
    sm = _make(tmp_path, init_threshold=3, update_threshold=10, sink=sink)
    for _ in range(3):
        sm.record_tool_call(tool_name="Read", summary="x")
    sm.update()
    for _ in range(10):
        sm.record_tool_call(tool_name="Bash", summary="y")
    assert sm.should_update() is True


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


def test_update_none_when_not_ready(tmp_path: Path) -> None:
    sink = FakeDistillSink()
    sm = _make(tmp_path, init_threshold=5, sink=sink)
    sm.record_tool_call(tool_name="Read", summary="x")
    assert sm.update() is None
    assert sink.calls == []


def test_update_calls_sink_with_prior_summary(tmp_path: Path) -> None:
    sink = FakeDistillSink(markdown="# v1\n")
    sm = _make(tmp_path, init_threshold=1, update_threshold=1, sink=sink)
    sm.record_tool_call(tool_name="Read", summary="a")
    first = sm.update()
    assert first is not None and first.success

    sink._markdown = "# v2\n"  # type: ignore[attr-defined]
    sm.record_tool_call(tool_name="Edit", summary="b")
    second = sm.update()
    assert second is not None and second.success

    # second call should see the v1 markdown as prior_summary
    assert len(sink.calls) == 2
    assert sink.calls[1].prior_summary == "# v1\n"
    assert "Edit: b" in sink.calls[1].recent_events


def test_update_writes_md_file(tmp_path: Path) -> None:
    sink = FakeDistillSink(markdown="# hello\nworld\n")
    sm = _make(tmp_path, init_threshold=1, sink=sink)
    sm.record_tool_call(tool_name="Read", summary="x")
    out = sm.update()
    assert out is not None and out.success
    assert sm.md_path().exists()
    assert sm.md_path().read_text(encoding="utf-8") == "# hello\nworld\n"


def test_update_updates_tool_count_at_last_update(tmp_path: Path) -> None:
    sink = FakeDistillSink()
    sm = _make(tmp_path, init_threshold=5, sink=sink)
    for _ in range(5):
        sm.record_tool_call(tool_name="Read", summary="x")
    sm.update()
    assert sm.state.tool_count_at_last_update == 5
    assert sm.state.tool_count_total == 5


def test_update_no_sink_raises(tmp_path: Path) -> None:
    sm = _make(tmp_path, init_threshold=1)
    sm.record_tool_call(tool_name="Read", summary="x")
    with pytest.raises(RuntimeError, match="no distill sink"):
        sm.update()


def test_update_sink_failure_records_failure(tmp_path: Path) -> None:
    sink = FakeDistillSink(success=False, error="LLM timed out", markdown="")
    sm = _make(tmp_path, init_threshold=1, sink=sink)
    sm.record_tool_call(tool_name="Read", summary="x")
    out = sm.update()
    assert out is not None
    assert out.success is False
    assert out.error == "LLM timed out"
    assert sm.state.distill_failures == 1
    assert sm.state.distill_successes == 0
    # md file should NOT have been written
    assert sm.md_path().exists() is False


def test_update_sink_exception_wrapped_as_failure(tmp_path: Path) -> None:
    sink = FakeDistillSink(raise_exc=RuntimeError("boom"))
    sm = _make(tmp_path, init_threshold=1, sink=sink)
    sm.record_tool_call(tool_name="Read", summary="x")
    out = sm.update()
    assert out is not None
    assert out.success is False
    assert out.error is not None and "boom" in out.error
    assert sm.state.distill_failures == 1


# ---------------------------------------------------------------------------
# truncate_content
# ---------------------------------------------------------------------------


def test_truncate_content_enforces_max_lines(tmp_path: Path) -> None:
    sm = _make(tmp_path, max_lines=3, max_bytes=10_000)
    content = "".join(f"line {i}\n" for i in range(20))
    out = sm.truncate_content(content)
    lines = out.splitlines()
    # 3 original lines + blank-ish warning lines
    assert lines[:3] == ["line 0", "line 1", "line 2"]
    assert "truncated" in out
    assert "max_lines" in out


def test_truncate_content_enforces_max_bytes_at_newline(tmp_path: Path) -> None:
    sm = _make(tmp_path, max_lines=10_000, max_bytes=120)
    # 20 lines of 10 bytes each = 200 bytes, well above the 120 cap
    content = "".join(f"line-{i:03d}\n" for i in range(20))
    assert len(content.encode("utf-8")) > 120
    out = sm.truncate_content(content)
    encoded = out.encode("utf-8")
    assert len(encoded) <= 120
    assert "truncated" in out
    assert "max_bytes" in out
    # the head should end on a newline (we cut on line boundary)
    head = out.split("<!--")[0]
    assert head.endswith("\n")


def test_truncate_content_below_caps_unchanged(tmp_path: Path) -> None:
    sm = _make(tmp_path, max_lines=100, max_bytes=10_000)
    content = "# small\nbody\n"
    assert sm.truncate_content(content) == content


def test_truncate_adds_warning_suffix(tmp_path: Path) -> None:
    sm = _make(tmp_path, max_lines=2, max_bytes=10_000)
    content = "a\nb\nc\nd\n"
    out = sm.truncate_content(content)
    assert out.rstrip().endswith("-->")
    assert "truncated" in out


def test_truncate_content_handles_single_giant_line(tmp_path: Path) -> None:
    sm = _make(tmp_path, max_lines=10, max_bytes=40)
    # one 500-byte line with no newlines
    content = "x" * 500
    out = sm.truncate_content(content)
    assert "truncated" in out
    assert len(out.encode("utf-8")) <= DEFAULT_MAX_MD_BYTES


# ---------------------------------------------------------------------------
# read_summary
# ---------------------------------------------------------------------------


def test_read_summary_returns_empty_when_no_md(tmp_path: Path) -> None:
    sm = _make(tmp_path)
    assert sm.read_summary() == ""


def test_read_summary_returns_written_content(tmp_path: Path) -> None:
    sink = FakeDistillSink(markdown="# saved\nstuff\n")
    sm = _make(tmp_path, init_threshold=1, sink=sink)
    sm.record_tool_call(tool_name="Read", summary="x")
    sm.update()
    assert sm.read_summary() == "# saved\nstuff\n"


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------


def test_save_load_roundtrip(tmp_path: Path) -> None:
    sink = FakeDistillSink(markdown="# md\n")
    sm = _make(tmp_path, init_threshold=1, sink=sink)
    for _ in range(5):
        sm.record_tool_call(tool_name="Read", summary="x")
    sm.update()

    # New instance pointing at the same cache_dir/session_id should see
    # the persisted state after load().
    sm2 = _make(tmp_path, init_threshold=1)
    sm2.load()
    assert sm2.state.tool_count_total == 5
    assert sm2.state.tool_count_at_last_update == 5
    assert sm2.state.distill_successes == 1


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------


def test_stats_tracks_success_failure(tmp_path: Path) -> None:
    ok_sink = FakeDistillSink(markdown="# ok\n")
    sm = _make(tmp_path, init_threshold=1, update_threshold=1, sink=ok_sink)
    sm.record_tool_call(tool_name="Read", summary="x")
    sm.update()
    stats = sm.stats()
    assert stats["distill_successes"] == 1
    assert stats["distill_failures"] == 0
    assert stats["tool_count_total"] == 1
    assert stats["recent_events_buffered"] == 1
    assert stats["md_bytes"] > 0
    assert stats["md_lines"] >= 1


# ---------------------------------------------------------------------------
# md_path parent dir creation
# ---------------------------------------------------------------------------


def test_md_path_creates_parent_dir_on_write(tmp_path: Path) -> None:
    sink = FakeDistillSink(markdown="# x\n")
    sm = _make(
        tmp_path,
        session_id="deeply/nested/id",
        init_threshold=1,
        sink=sink,
    )
    assert sm.md_path().parent.exists() is False
    sm.record_tool_call(tool_name="Read", summary="x")
    sm.update()
    assert sm.md_path().parent.exists() is True
    assert sm.md_path().exists() is True


# ---------------------------------------------------------------------------
# Additional coverage
# ---------------------------------------------------------------------------


def test_record_tool_call_survives_multiple_sessions(tmp_path: Path) -> None:
    """Tool counts accumulate even without distillation running."""
    sm = _make(tmp_path, init_threshold=1000, update_threshold=1000)
    for i in range(25):
        sm.record_tool_call(tool_name="Read", summary=f"{i}")
    assert sm.tool_count_total == 25
    assert sm.should_update() is False


def test_update_defense_in_depth_truncation(tmp_path: Path) -> None:
    """Sink returns oversized markdown; SessionMemory still caps it."""
    huge = "".join(f"line {i}\n" for i in range(500))
    sink = FakeDistillSink(markdown=huge)
    sm = _make(tmp_path, init_threshold=1, max_lines=10, sink=sink)
    sm.record_tool_call(tool_name="Read", summary="x")
    out = sm.update()
    assert out is not None and out.success
    written = sm.md_path().read_text(encoding="utf-8")
    assert "truncated" in written
    # the preserved lines should be the first 10 originals
    assert written.startswith("line 0\nline 1\n")
