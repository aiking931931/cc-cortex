"""Tests for the persona JSONL state log."""

from __future__ import annotations

from pathlib import Path

from concinno.persona.state import (
    PersonaState,
    TurnRecord,
    make_consolidate,
    make_pin,
    make_turn,
    make_unpin,
)


def test_turn_record_jsonl_round_trip() -> None:
    r = make_turn("hi", "hello there")
    line = r.to_jsonl()
    r2 = TurnRecord.from_jsonl(line)
    assert r2.user == "hi"
    assert r2.assistant == "hello there"
    assert r2.kind == "turn"


def test_state_append_persists_to_disk(tmp_path: Path) -> None:
    log = tmp_path / "alice.jsonl"
    s = PersonaState(log_path=log)
    s.append(make_turn("hi", "hi back"))
    s.append(make_pin("user is Bob", "intro"))
    s.append(make_consolidate("checkpoint"))

    text = log.read_text(encoding="utf-8").splitlines()
    assert len(text) == 3
    s2 = PersonaState.load(log)
    assert len(s2.records) == 3
    assert s2.turns()[0].user == "hi"


def test_state_load_handles_missing_file(tmp_path: Path) -> None:
    s = PersonaState.load(tmp_path / "does-not-exist.jsonl")
    assert s.records == []


def test_state_load_skips_corrupt_lines(tmp_path: Path) -> None:
    log = tmp_path / "broken.jsonl"
    log.write_text(
        "this is not json\n"
        '{"ts": "2026-04-25T00:00:00Z", "kind": "turn", "user": "ok", "assistant": "ok"}\n',
        encoding="utf-8",
    )
    s = PersonaState.load(log)
    assert len(s.records) == 1
    assert s.records[0].user == "ok"


def test_state_save_snapshot_atomic(tmp_path: Path) -> None:
    s = PersonaState.empty()
    s.append(make_turn("a", "b"))
    s.append(make_unpin("old fact"))
    out = tmp_path / "snap.jsonl"
    s.save_snapshot(out)
    reloaded = PersonaState.load(out)
    assert len(reloaded.records) == 2
    # Ensure tempfile was cleaned up.
    assert not (tmp_path / "snap.jsonl.tmp").exists()


def test_state_turns_filters_to_chat_only() -> None:
    s = PersonaState.empty()
    s.append(make_turn("a", "b"))
    s.append(make_pin("x"))
    s.append(make_turn("c", "d"))
    s.append(make_consolidate("cp"))
    turns = s.turns()
    assert len(turns) == 2
    assert turns[0].user == "a"
    assert turns[1].user == "c"
