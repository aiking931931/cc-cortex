"""Tests for :mod:`concinno.cache.memdir`.

Covers the append-only dated log invariants:

- Line rendering and round-trip parsing (with / without tags).
- Append rollover when either the line cap or the byte cap would be
  exceeded by the next entry.
- ``read_day`` / ``read_window`` ordering, including rollover-suffix
  handling and corrupt-line skipping.
- ``find_relevant`` keyword matching, max_entries cap, max_age_days
  window, and newest-first ordering.
- ``truncate_entrypoint`` dual-cap behaviour (line cap, byte cap,
  below-caps unchanged).
- Path helpers and env-var-driven default root.
- :meth:`Memdir.stats` reporting.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from concinno.cache.memdir import (
    DEFAULT_MAX_BYTES_PER_FILE,
    DEFAULT_MAX_ENTRYPOINT_BYTES,
    DEFAULT_MAX_ENTRYPOINT_LINES,
    DEFAULT_MAX_LINES_PER_FILE,
    ENTRYPOINT_FILENAME,
    Memdir,
    MemdirStats,
    MemoryEntry,
)

# ---------------------------------------------------------------------------
# MemoryEntry rendering / parsing
# ---------------------------------------------------------------------------


def test_memory_entry_to_md_line_basic() -> None:
    entry = MemoryEntry(
        timestamp=datetime(2026, 4, 13, 9, 30, 15),
        kind="tool_call",
        summary="Read session_memory.py",
    )
    line = entry.to_md_line()
    assert line == "- [09:30:15] tool_call: Read session_memory.py\n"


def test_memory_entry_to_md_line_with_tags() -> None:
    entry = MemoryEntry(
        timestamp=datetime(2026, 4, 13, 9, 30, 15),
        kind="decision",
        summary="use FTRL for routing",
        tags=("ziq", "bench"),
    )
    line = entry.to_md_line()
    assert line == (
        "- [09:30:15] decision: use FTRL for routing (tags: ziq,bench)\n"
    )


def test_memory_entry_to_md_line_with_details() -> None:
    entry = MemoryEntry(
        timestamp=datetime(2026, 4, 13, 9, 30, 15),
        kind="blocker",
        summary="OOM on A100",
        details={"seq": "4096", "gpu": "A100"},
    )
    rendered = entry.to_md_line()
    lines = rendered.splitlines()
    assert lines[0] == "- [09:30:15] blocker: OOM on A100"
    assert lines[1].startswith("<!-- details:")
    assert "seq=4096" in lines[1]
    assert "gpu=A100" in lines[1]


def test_parse_md_line_roundtrip() -> None:
    original = MemoryEntry(
        timestamp=datetime(2026, 4, 13, 9, 30, 15),
        kind="decision",
        summary="use FTRL for routing",
        tags=("ziq", "bench"),
    )
    rendered = original.to_md_line().rstrip("\n")
    parsed = MemoryEntry.parse_md_line(rendered, day=date(2026, 4, 13))
    assert parsed is not None
    assert parsed.kind == "decision"
    assert parsed.summary == "use FTRL for routing"
    assert parsed.tags == ("ziq", "bench")
    assert parsed.timestamp == datetime(2026, 4, 13, 9, 30, 15)


def test_parse_md_line_rejects_malformed() -> None:
    assert MemoryEntry.parse_md_line("not a memdir line", day=date(2026, 4, 13)) is None
    assert MemoryEntry.parse_md_line("", day=date(2026, 4, 13)) is None
    assert (
        MemoryEntry.parse_md_line("- no brackets here", day=date(2026, 4, 13)) is None
    )
    # Bad time component.
    assert (
        MemoryEntry.parse_md_line(
            "- [25:99:99] foo: bar", day=date(2026, 4, 13)
        )
        is None
    )


def test_parse_md_line_skips_details_line() -> None:
    assert (
        MemoryEntry.parse_md_line(
            "<!-- details: seq=4096; gpu=A100 -->", day=date(2026, 4, 13)
        )
        is None
    )


# ---------------------------------------------------------------------------
# append / rollover
# ---------------------------------------------------------------------------


def _make_entry(
    day: date,
    hh: int = 9,
    mm: int = 0,
    ss: int = 0,
    *,
    kind: str = "tool",
    summary: str = "x",
    tags: tuple[str, ...] = (),
) -> MemoryEntry:
    return MemoryEntry(
        timestamp=datetime(day.year, day.month, day.day, hh, mm, ss),
        kind=kind,
        summary=summary,
        tags=tags,
    )


def test_append_writes_primary_file_for_today(tmp_path: Path) -> None:
    memdir = Memdir(root=tmp_path)
    day = date(2026, 4, 13)
    path = memdir.append(_make_entry(day, 9, 0, 0, summary="first event"))
    assert path == tmp_path / "2026-04-13.md"
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "first event" in content
    assert content.endswith("\n")


def test_append_rolls_over_when_line_cap_hit(tmp_path: Path) -> None:
    memdir = Memdir(root=tmp_path, max_lines_per_file=3, max_bytes_per_file=10_000)
    day = date(2026, 4, 13)
    paths = [
        memdir.append(_make_entry(day, 9, 0, i, summary=f"entry {i}"))
        for i in range(5)
    ]
    assert paths[0] == tmp_path / "2026-04-13.md"
    assert paths[1] == tmp_path / "2026-04-13.md"
    assert paths[2] == tmp_path / "2026-04-13.md"
    # 4th entry would push the primary file past 3 lines → rollover.
    assert paths[3] == tmp_path / "2026-04-13_02.md"
    assert paths[4] == tmp_path / "2026-04-13_02.md"
    primary = (tmp_path / "2026-04-13.md").read_text(encoding="utf-8")
    assert primary.count("\n") == 3


def test_append_rolls_over_when_byte_cap_hit(tmp_path: Path) -> None:
    # Each entry renders to ~40-60 bytes. Pick a byte cap that fits exactly
    # one entry so the second one must roll.
    entry = _make_entry(date(2026, 4, 13), summary="bytes rollover test")
    size = len(entry.to_md_line().encode("utf-8"))
    memdir = Memdir(
        root=tmp_path,
        max_lines_per_file=1_000,
        max_bytes_per_file=size + 1,  # room for one entry only
    )
    day = date(2026, 4, 13)
    p1 = memdir.append(_make_entry(day, 9, 0, 0, summary="bytes rollover test"))
    p2 = memdir.append(_make_entry(day, 9, 0, 1, summary="bytes rollover test"))
    assert p1 == tmp_path / "2026-04-13.md"
    assert p2 == tmp_path / "2026-04-13_02.md"


def test_append_continues_from_existing_highest_suffix(tmp_path: Path) -> None:
    memdir = Memdir(root=tmp_path, max_lines_per_file=1, max_bytes_per_file=10_000)
    day = date(2026, 4, 13)
    memdir.append(_make_entry(day, 9, 0, 0, summary="a"))
    memdir.append(_make_entry(day, 9, 0, 1, summary="b"))
    memdir.append(_make_entry(day, 9, 0, 2, summary="c"))
    files = sorted(p.name for p in tmp_path.glob("*.md"))
    assert files == ["2026-04-13.md", "2026-04-13_02.md", "2026-04-13_03.md"]


# ---------------------------------------------------------------------------
# read_day / read_window
# ---------------------------------------------------------------------------


def test_read_day_returns_all_entries_in_order(tmp_path: Path) -> None:
    memdir = Memdir(root=tmp_path)
    day = date(2026, 4, 13)
    for i in range(3):
        memdir.append(_make_entry(day, 9, 0, i, summary=f"entry {i}"))
    entries = memdir.read_day(day)
    assert [e.summary for e in entries] == ["entry 0", "entry 1", "entry 2"]


def test_read_day_handles_rollover_files(tmp_path: Path) -> None:
    memdir = Memdir(root=tmp_path, max_lines_per_file=2, max_bytes_per_file=10_000)
    day = date(2026, 4, 13)
    for i in range(5):
        memdir.append(_make_entry(day, 9, 0, i, summary=f"e{i}"))
    entries = memdir.read_day(day)
    assert len(entries) == 5
    assert [e.summary for e in entries] == ["e0", "e1", "e2", "e3", "e4"]


def test_read_day_skips_corrupt_lines(tmp_path: Path) -> None:
    memdir = Memdir(root=tmp_path)
    day = date(2026, 4, 13)
    memdir.append(_make_entry(day, 9, 0, 0, summary="good"))
    path = tmp_path / "2026-04-13.md"
    with path.open("a", encoding="utf-8") as fh:
        fh.write("this is not a valid memdir line\n")
        fh.write("- [BAD:TIME] kind: nope\n")
    memdir.append(_make_entry(day, 9, 0, 1, summary="also good"))
    entries = memdir.read_day(day)
    assert [e.summary for e in entries] == ["good", "also good"]


def test_read_day_empty_when_no_files(tmp_path: Path) -> None:
    memdir = Memdir(root=tmp_path)
    assert memdir.read_day(date(2026, 4, 13)) == []


def test_read_window_concatenates_days(tmp_path: Path) -> None:
    memdir = Memdir(root=tmp_path)
    d1 = date(2026, 4, 11)
    d2 = date(2026, 4, 12)
    d3 = date(2026, 4, 13)
    memdir.append(_make_entry(d1, 9, 0, 0, summary="day1"))
    memdir.append(_make_entry(d2, 9, 0, 0, summary="day2"))
    memdir.append(_make_entry(d3, 9, 0, 0, summary="day3"))
    window = memdir.read_window(start=d1, end=d3)
    assert [e.summary for e in window] == ["day1", "day2", "day3"]


def test_read_window_empty_for_reversed_range(tmp_path: Path) -> None:
    memdir = Memdir(root=tmp_path)
    day = date(2026, 4, 13)
    memdir.append(_make_entry(day, summary="x"))
    assert memdir.read_window(start=day, end=day - timedelta(days=1)) == []


# ---------------------------------------------------------------------------
# find_relevant
# ---------------------------------------------------------------------------


def test_find_relevant_keyword_match(tmp_path: Path) -> None:
    memdir = Memdir(root=tmp_path)
    today = date.today()
    memdir.append(_make_entry(today, 9, 0, 0, summary="ran ZIQ benchmark on pod"))
    memdir.append(_make_entry(today, 9, 0, 1, summary="edited unrelated config"))
    hits = memdir.find_relevant("ziq benchmark")
    assert len(hits) == 1
    assert "ZIQ" in hits[0].summary


def test_find_relevant_respects_max_entries(tmp_path: Path) -> None:
    memdir = Memdir(root=tmp_path)
    today = date.today()
    for i in range(10):
        memdir.append(_make_entry(today, 9, 0, i, summary=f"benchmark run {i}"))
    hits = memdir.find_relevant("benchmark", max_entries=3)
    assert len(hits) == 3


def test_find_relevant_respects_max_age_days(tmp_path: Path) -> None:
    memdir = Memdir(root=tmp_path)
    today = date.today()
    stale = today - timedelta(days=30)
    memdir.append(_make_entry(stale, 9, 0, 0, summary="ancient benchmark event"))
    memdir.append(_make_entry(today, 9, 0, 0, summary="fresh benchmark event"))
    hits = memdir.find_relevant("benchmark", max_age_days=7)
    assert len(hits) == 1
    assert hits[0].summary == "fresh benchmark event"


def test_find_relevant_returns_newest_first(tmp_path: Path) -> None:
    memdir = Memdir(root=tmp_path)
    today = date.today()
    memdir.append(_make_entry(today, 9, 0, 0, summary="benchmark alpha"))
    memdir.append(_make_entry(today, 10, 0, 0, summary="benchmark beta"))
    memdir.append(_make_entry(today, 11, 0, 0, summary="benchmark gamma"))
    hits = memdir.find_relevant("benchmark")
    assert [h.summary for h in hits] == [
        "benchmark gamma",
        "benchmark beta",
        "benchmark alpha",
    ]


def test_find_relevant_empty_query(tmp_path: Path) -> None:
    memdir = Memdir(root=tmp_path)
    today = date.today()
    memdir.append(_make_entry(today, 9, 0, 0, summary="anything"))
    assert memdir.find_relevant("") == []
    assert memdir.find_relevant("   ") == []


# ---------------------------------------------------------------------------
# truncate_entrypoint
# ---------------------------------------------------------------------------


def test_truncate_entrypoint_lines_cap(tmp_path: Path) -> None:
    memdir = Memdir(root=tmp_path)
    content = "\n".join(f"line {i}" for i in range(500))
    result = memdir.truncate_entrypoint(content, max_lines=10, max_bytes=10_000)
    # First 10 lines plus the warning marker.
    assert "line 0" in result
    assert "line 9" in result
    assert "line 10" not in result
    assert "max_lines" in result


def test_truncate_entrypoint_bytes_cap(tmp_path: Path) -> None:
    memdir = Memdir(root=tmp_path)
    # One giant line that blows the byte cap without blowing the line cap.
    content = "x" * 1_000 + "\n" + "y" * 1_000 + "\n"
    result = memdir.truncate_entrypoint(
        content, max_lines=1_000, max_bytes=500
    )
    assert len(result.encode("utf-8")) <= 500 + 200  # warning appended
    assert "max_bytes" in result


def test_truncate_entrypoint_below_caps_unchanged(tmp_path: Path) -> None:
    memdir = Memdir(root=tmp_path)
    content = "- [a](a.md) — hook\n- [b](b.md) — hook\n"
    result = memdir.truncate_entrypoint(
        content, max_lines=200, max_bytes=25_000
    )
    assert result == content
    assert "truncated" not in result


# ---------------------------------------------------------------------------
# Paths / listing
# ---------------------------------------------------------------------------


def test_entrypoint_path_under_root(tmp_path: Path) -> None:
    memdir = Memdir(root=tmp_path)
    assert memdir.entrypoint_path() == tmp_path / ENTRYPOINT_FILENAME


def test_file_path_for_suffix_formats(tmp_path: Path) -> None:
    memdir = Memdir(root=tmp_path)
    day = date(2026, 4, 13)
    assert memdir.file_path_for(day) == tmp_path / "2026-04-13.md"
    assert memdir.file_path_for(day, suffix=1) == tmp_path / "2026-04-13.md"
    assert memdir.file_path_for(day, suffix=2) == tmp_path / "2026-04-13_02.md"
    assert memdir.file_path_for(day, suffix=10) == tmp_path / "2026-04-13_10.md"


def test_list_files_sorted_oldest_first(tmp_path: Path) -> None:
    memdir = Memdir(root=tmp_path)
    memdir.append(_make_entry(date(2026, 4, 13), summary="c"))
    memdir.append(_make_entry(date(2026, 4, 11), summary="a"))
    memdir.append(_make_entry(date(2026, 4, 12), summary="b"))
    # Also write a MEMORY.md next to the files — must be excluded.
    (tmp_path / ENTRYPOINT_FILENAME).write_text("index", encoding="utf-8")
    files = memdir.list_files()
    names = [p.name for p in files]
    assert names == ["2026-04-11.md", "2026-04-12.md", "2026-04-13.md"]
    assert ENTRYPOINT_FILENAME not in names


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------


def test_stats_reports_total_entries_files(tmp_path: Path) -> None:
    memdir = Memdir(root=tmp_path)
    memdir.append(_make_entry(date(2026, 4, 11), 9, 0, 0, summary="a"))
    memdir.append(_make_entry(date(2026, 4, 12), 9, 0, 0, summary="b"))
    memdir.append(_make_entry(date(2026, 4, 13), 9, 0, 0, summary="c"))
    memdir.append(_make_entry(date(2026, 4, 13), 9, 0, 1, summary="d"))
    stats = memdir.stats()
    assert stats.total_entries == 4
    assert stats.total_files == 3
    assert stats.oldest_day == date(2026, 4, 11)
    assert stats.newest_day == date(2026, 4, 13)
    assert stats.total_bytes > 0


def test_stats_empty_when_no_files(tmp_path: Path) -> None:
    memdir = Memdir(root=tmp_path)
    stats = memdir.stats()
    assert stats == MemdirStats()


# ---------------------------------------------------------------------------
# Default root / env var
# ---------------------------------------------------------------------------


def test_default_root_respects_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    custom = tmp_path / "custom_memdir"
    monkeypatch.setenv("CONCINNO_MEMDIR", str(custom))
    memdir = Memdir()
    assert memdir.root == custom
    memdir.append(_make_entry(date(2026, 4, 13), summary="env-rooted"))
    assert (custom / "2026-04-13.md").exists()


def test_constructor_rejects_bad_caps(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        Memdir(root=tmp_path, max_lines_per_file=0)
    with pytest.raises(ValueError):
        Memdir(root=tmp_path, max_bytes_per_file=0)


def test_public_constants_exported() -> None:
    assert DEFAULT_MAX_LINES_PER_FILE == 200
    assert DEFAULT_MAX_BYTES_PER_FILE == 25_000
    assert DEFAULT_MAX_ENTRYPOINT_LINES == 200
    assert DEFAULT_MAX_ENTRYPOINT_BYTES == 25_000
