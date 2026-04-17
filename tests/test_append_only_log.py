"""Tests for concinno.cache.append_only_log."""
from __future__ import annotations

import json
import time
from pathlib import Path

from concinno.cache.append_only_log import (
    SCHEMA_VERSION,
    AppendOnlyLog,
    LogEvent,
)


def _mk_event(
    log: AppendOnlyLog,
    session_id: str,
    *,
    ts: float | None = None,
    event_type: str = "llm_call",
    payload: dict | None = None,
    parent: str | None = None,
    tokens: int = 0,
) -> LogEvent:
    return LogEvent(
        event_id=log.new_event_id(),
        session_id=session_id,
        timestamp=ts if ts is not None else time.time(),
        event_type=event_type,
        payload=payload or {"msg": "hello"},
        parent_event_id=parent,
        tokens_estimate=tokens,
    )


# ---------------------------------------------------------------------------
# LogEvent dataclass
# ---------------------------------------------------------------------------


def test_log_event_fields_and_defaults():
    ev = LogEvent(
        event_id="abc",
        session_id="s1",
        timestamp=123.0,
        event_type="llm_call",
    )
    assert ev.event_id == "abc"
    assert ev.session_id == "s1"
    assert ev.timestamp == 123.0
    assert ev.event_type == "llm_call"
    assert ev.payload == {}
    assert ev.parent_event_id is None
    assert ev.tokens_estimate == 0
    assert ev.schema_version == SCHEMA_VERSION


def test_log_event_distinct_default_payload():
    """Default factory must not share dict between instances."""
    a = LogEvent(event_id="1", session_id="s", timestamp=0.0, event_type="x")
    b = LogEvent(event_id="2", session_id="s", timestamp=0.0, event_type="x")
    a.payload["k"] = "v"
    assert "k" not in b.payload


# ---------------------------------------------------------------------------
# Constructor / log_dir resolution
# ---------------------------------------------------------------------------


def test_constructor_explicit_dir(tmp_path: Path):
    d = tmp_path / "custom_log"
    log = AppendOnlyLog(d)
    assert log.log_dir == d
    assert d.is_dir()


def test_constructor_env_override(tmp_path: Path, monkeypatch):
    env_dir = tmp_path / "from_env"
    monkeypatch.setenv("CCC_APPEND_LOG_DIR", str(env_dir))
    log = AppendOnlyLog()
    assert log.log_dir == env_dir
    assert env_dir.is_dir()


def test_constructor_default_home(tmp_path: Path, monkeypatch):
    """Default dir uses Path.home() so no personal paths leak in."""
    monkeypatch.delenv("CCC_APPEND_LOG_DIR", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    log = AppendOnlyLog()
    assert log.log_dir == tmp_path / ".concinno_cache" / "append_only_log"
    assert log.log_dir.is_dir()


def test_explicit_arg_overrides_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CCC_APPEND_LOG_DIR", str(tmp_path / "env_dir"))
    explicit = tmp_path / "explicit"
    log = AppendOnlyLog(explicit)
    assert log.log_dir == explicit


# ---------------------------------------------------------------------------
# append()
# ---------------------------------------------------------------------------


def test_append_single_event_creates_file(tmp_path: Path):
    log = AppendOnlyLog(tmp_path)
    ev = _mk_event(log, "sess-1", ts=time.time())
    log.append(ev)

    # Exactly one day dir, one jsonl file, one line
    day_dirs = [p for p in tmp_path.iterdir() if p.is_dir()]
    assert len(day_dirs) == 1
    files = list(day_dirs[0].glob("*.jsonl"))
    assert len(files) == 1
    assert files[0].name == "sess-1.jsonl"
    lines = files[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["event_id"] == ev.event_id
    assert data["schema_version"] == SCHEMA_VERSION


def test_append_multiple_events_same_session(tmp_path: Path):
    log = AppendOnlyLog(tmp_path)
    for i in range(5):
        log.append(_mk_event(log, "sess-1", ts=1000.0 + i))
    events = log.read_session("sess-1")
    assert len(events) == 5
    assert [e.timestamp for e in events] == [1000.0, 1001.0, 1002.0, 1003.0, 1004.0]


# ---------------------------------------------------------------------------
# append_batch()
# ---------------------------------------------------------------------------


def test_append_batch_cross_session(tmp_path: Path):
    log = AppendOnlyLog(tmp_path)
    now = time.time()
    batch = [
        _mk_event(log, "sess-A", ts=now),
        _mk_event(log, "sess-B", ts=now),
        _mk_event(log, "sess-A", ts=now + 1),
    ]
    log.append_batch(batch)
    a_events = log.read_session("sess-A")
    b_events = log.read_session("sess-B")
    assert len(a_events) == 2
    assert len(b_events) == 1


def test_append_batch_empty_noop(tmp_path: Path):
    log = AppendOnlyLog(tmp_path)
    log.append_batch([])  # must not raise
    # No day dirs created for empty batch
    day_dirs = [p for p in tmp_path.iterdir() if p.is_dir()]
    assert day_dirs == []


# ---------------------------------------------------------------------------
# read_session() behavior
# ---------------------------------------------------------------------------


def test_read_session_sorted_by_timestamp(tmp_path: Path):
    log = AppendOnlyLog(tmp_path)
    # Append in non-chrono order within the same day
    now = time.time()
    log.append(_mk_event(log, "sess-1", ts=now + 10))
    log.append(_mk_event(log, "sess-1", ts=now + 1))
    log.append(_mk_event(log, "sess-1", ts=now + 5))
    events = log.read_session("sess-1")
    assert [e.timestamp for e in events] == sorted(e.timestamp for e in events)


def test_read_session_limit(tmp_path: Path):
    log = AppendOnlyLog(tmp_path)
    now = time.time()
    for i in range(10):
        log.append(_mk_event(log, "sess-1", ts=now + i))
    events = log.read_session("sess-1", limit=3)
    assert len(events) == 3
    # Earliest 3 after the sort
    assert [e.timestamp for e in events] == [now, now + 1, now + 2]


def test_read_session_missing(tmp_path: Path):
    log = AppendOnlyLog(tmp_path)
    assert log.read_session("nonexistent") == []


def test_read_session_empty_dir(tmp_path: Path):
    log = AppendOnlyLog(tmp_path)
    # Directory exists but no data written
    assert log.read_session("any") == []


def test_read_session_cross_day(tmp_path: Path):
    """Same session_id split across two YYYY-MM-DD dirs."""
    log = AppendOnlyLog(tmp_path)
    sid = "sess-multiday"
    # Manually construct two day dirs
    d1 = tmp_path / "2026-04-14"
    d2 = tmp_path / "2026-04-15"
    d1.mkdir()
    d2.mkdir()
    ev1 = LogEvent(
        event_id="e1", session_id=sid, timestamp=100.0, event_type="x",
        payload={"day": 1},
    )
    ev2 = LogEvent(
        event_id="e2", session_id=sid, timestamp=200.0, event_type="x",
        payload={"day": 2},
    )
    (d1 / f"{sid}.jsonl").write_text(
        json.dumps({
            "event_id": ev1.event_id,
            "session_id": ev1.session_id,
            "timestamp": ev1.timestamp,
            "event_type": ev1.event_type,
            "payload": ev1.payload,
            "parent_event_id": None,
            "tokens_estimate": 0,
            "schema_version": SCHEMA_VERSION,
        }) + "\n",
        encoding="utf-8",
    )
    (d2 / f"{sid}.jsonl").write_text(
        json.dumps({
            "event_id": ev2.event_id,
            "session_id": ev2.session_id,
            "timestamp": ev2.timestamp,
            "event_type": ev2.event_type,
            "payload": ev2.payload,
            "parent_event_id": None,
            "tokens_estimate": 0,
            "schema_version": SCHEMA_VERSION,
        }) + "\n",
        encoding="utf-8",
    )
    events = log.read_session(sid)
    assert len(events) == 2
    assert [e.event_id for e in events] == ["e1", "e2"]
    assert events[0].payload["day"] == 1
    assert events[1].payload["day"] == 2


# ---------------------------------------------------------------------------
# iter_session() streaming
# ---------------------------------------------------------------------------


def test_iter_session_yields_all(tmp_path: Path):
    log = AppendOnlyLog(tmp_path)
    now = time.time()
    for i in range(4):
        log.append(_mk_event(log, "sess-1", ts=now + i))
    it = log.iter_session("sess-1")
    # Ensure it's a generator (not pre-materialized list)
    assert hasattr(it, "__next__")
    events = list(it)
    assert len(events) == 4


def test_iter_session_missing(tmp_path: Path):
    log = AppendOnlyLog(tmp_path)
    assert list(log.iter_session("ghost")) == []


# ---------------------------------------------------------------------------
# list_sessions()
# ---------------------------------------------------------------------------


def test_list_sessions_all(tmp_path: Path):
    log = AppendOnlyLog(tmp_path)
    log.append(_mk_event(log, "sess-A"))
    log.append(_mk_event(log, "sess-B"))
    log.append(_mk_event(log, "sess-A"))  # duplicate session
    sessions = log.list_sessions()
    assert sessions == ["sess-A", "sess-B"]


def test_list_sessions_by_date(tmp_path: Path):
    log = AppendOnlyLog(tmp_path)
    # Create two day dirs manually
    d1 = tmp_path / "2026-04-14"
    d2 = tmp_path / "2026-04-15"
    d1.mkdir()
    d2.mkdir()
    (d1 / "sess-old.jsonl").write_text("", encoding="utf-8")
    (d2 / "sess-new.jsonl").write_text("", encoding="utf-8")
    assert log.list_sessions(date="2026-04-14") == ["sess-old"]
    assert log.list_sessions(date="2026-04-15") == ["sess-new"]
    assert log.list_sessions(date="2099-01-01") == []


# ---------------------------------------------------------------------------
# search()
# ---------------------------------------------------------------------------


def test_search_substring(tmp_path: Path):
    log = AppendOnlyLog(tmp_path)
    log.append(_mk_event(log, "s1", payload={"text": "architecture decision: FTRL"}))
    log.append(_mk_event(log, "s1", payload={"text": "unrelated"}))
    log.append(_mk_event(log, "s2", payload={"text": "Also FTRL here"}))
    results = log.search("ftrl")  # case-insensitive
    assert len(results) == 2
    texts = {r.payload["text"] for r in results}
    assert "architecture decision: FTRL" in texts
    assert "Also FTRL here" in texts


def test_search_limit(tmp_path: Path):
    log = AppendOnlyLog(tmp_path)
    for i in range(20):
        log.append(_mk_event(log, "s", payload={"msg": f"foo {i}"}))
    results = log.search("foo", limit=5)
    assert len(results) == 5


def test_search_date_range(tmp_path: Path):
    log = AppendOnlyLog(tmp_path)
    d1 = tmp_path / "2026-04-10"
    d2 = tmp_path / "2026-04-20"
    d1.mkdir()
    d2.mkdir()
    payload_line = json.dumps({
        "event_id": "e1",
        "session_id": "s",
        "timestamp": 1.0,
        "event_type": "x",
        "payload": {"text": "match me"},
        "parent_event_id": None,
        "tokens_estimate": 0,
        "schema_version": SCHEMA_VERSION,
    })
    (d1 / "s.jsonl").write_text(payload_line + "\n", encoding="utf-8")
    (d2 / "s.jsonl").write_text(payload_line + "\n", encoding="utf-8")
    in_range = log.search("match", date_range=("2026-04-15", "2026-04-25"))
    assert len(in_range) == 1
    out_range = log.search("match", date_range=("2099-01-01", "2099-12-31"))
    assert out_range == []


# ---------------------------------------------------------------------------
# stats()
# ---------------------------------------------------------------------------


def test_stats_empty(tmp_path: Path):
    log = AppendOnlyLog(tmp_path)
    s = log.stats()
    assert s["total_events"] == 0
    assert s["total_bytes"] == 0
    assert s["days_with_data"] == 0
    assert s["log_dir"] == str(tmp_path)
    assert s["schema_version"] == SCHEMA_VERSION


def test_stats_after_writes(tmp_path: Path):
    log = AppendOnlyLog(tmp_path)
    for _ in range(3):
        log.append(_mk_event(log, "s"))
    s = log.stats()
    assert s["total_events"] == 3
    assert s["total_bytes"] > 0
    assert s["days_with_data"] == 1


# ---------------------------------------------------------------------------
# Crash-safety / robustness
# ---------------------------------------------------------------------------


def test_corrupted_line_skipped(tmp_path: Path):
    log = AppendOnlyLog(tmp_path)
    log.append(_mk_event(log, "s1", ts=100.0))
    # Corrupt the file by appending garbage
    day_dirs = [p for p in tmp_path.iterdir() if p.is_dir()]
    file_ = day_dirs[0] / "s1.jsonl"
    with open(file_, "a", encoding="utf-8") as fh:
        fh.write("{not valid json\n")
        fh.write("\n")  # empty line
    log.append(_mk_event(log, "s1", ts=200.0))
    events = log.read_session("s1")
    assert len(events) == 2
    assert {e.timestamp for e in events} == {100.0, 200.0}


def test_corrupted_line_skipped_in_search(tmp_path: Path):
    log = AppendOnlyLog(tmp_path)
    log.append(_mk_event(log, "s", payload={"msg": "target"}))
    day_dirs = [p for p in tmp_path.iterdir() if p.is_dir()]
    file_ = day_dirs[0] / "s.jsonl"
    with open(file_, "a", encoding="utf-8") as fh:
        fh.write("target but not json\n")
    results = log.search("target")
    # Only the well-formed event matches; the garbage line is skipped
    assert len(results) == 1


def test_future_schema_extra_fields_skipped(tmp_path: Path):
    """A future-version event with unknown fields is silently skipped."""
    log = AppendOnlyLog(tmp_path)
    d = tmp_path / "2026-04-15"
    d.mkdir()
    # Valid event
    valid = {
        "event_id": "e1",
        "session_id": "s",
        "timestamp": 1.0,
        "event_type": "x",
        "payload": {},
        "parent_event_id": None,
        "tokens_estimate": 0,
        "schema_version": SCHEMA_VERSION,
    }
    # Future-schema event with an unknown field
    future = dict(valid)
    future["event_id"] = "e2"
    future["brand_new_field"] = "hi"
    (d / "s.jsonl").write_text(
        json.dumps(valid) + "\n" + json.dumps(future) + "\n",
        encoding="utf-8",
    )
    events = log.read_session("s")
    assert len(events) == 1
    assert events[0].event_id == "e1"


# ---------------------------------------------------------------------------
# Unicode / i18n
# ---------------------------------------------------------------------------


def test_unicode_payload_roundtrip(tmp_path: Path):
    log = AppendOnlyLog(tmp_path)
    payload = {"zh": "繁體中文", "emoji": "🤖✨", "mixed": "café"}
    log.append(_mk_event(log, "s", payload=payload))
    events = log.read_session("s")
    assert len(events) == 1
    assert events[0].payload == payload
    # Ensure the raw file is UTF-8 (ensure_ascii=False)
    day_dirs = [p for p in tmp_path.iterdir() if p.is_dir()]
    raw = (day_dirs[0] / "s.jsonl").read_text(encoding="utf-8")
    assert "繁體中文" in raw
    assert "🤖" in raw


# ---------------------------------------------------------------------------
# Append-only invariant (no mutation API)
# ---------------------------------------------------------------------------


def test_no_mutation_api():
    """Append-only invariant: no update / delete / clear methods."""
    forbidden = {"update", "delete", "remove", "clear", "truncate", "overwrite"}
    api = {name for name in dir(AppendOnlyLog) if not name.startswith("_")}
    assert forbidden.isdisjoint(api), (
        f"AppendOnlyLog must not expose mutation methods, found: "
        f"{forbidden & api}"
    )


# ---------------------------------------------------------------------------
# Parent event linking
# ---------------------------------------------------------------------------


def test_parent_event_id_preserved(tmp_path: Path):
    log = AppendOnlyLog(tmp_path)
    root = _mk_event(log, "s", ts=100.0, event_type="decision")
    child = _mk_event(
        log, "s", ts=101.0, event_type="tool_call", parent=root.event_id,
    )
    log.append(root)
    log.append(child)
    events = log.read_session("s")
    assert events[0].parent_event_id is None
    assert events[1].parent_event_id == root.event_id


def test_new_event_id_unique():
    ids = {AppendOnlyLog.new_event_id() for _ in range(1000)}
    assert len(ids) == 1000
