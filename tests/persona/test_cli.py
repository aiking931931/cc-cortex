"""Smoke tests for the ``concinno persona`` CLI surface."""

from __future__ import annotations

import argparse
from pathlib import Path

from concinno.cli.persona_cmd import register
from concinno.persona.cli import (
    format_pinned_text,
    format_recall_text,
    list_pinned,
    pin_memory,
    recall_memory,
    run_chat,
    to_json,
)

PERSONA_MD = """---
name: alice
personality: curious
voice: casual
memory_seed:
  - "born in Tokyo"
emotional_state:
  default: neutral
  intensity: 0.5
  decay_rate: 0.95
---

Alice is a sample persona for tests.
"""


def _write_persona(tmp_path: Path) -> Path:
    p = tmp_path / "alice.md"
    p.write_text(PERSONA_MD, encoding="utf-8")
    return p


def test_register_attaches_subcommands() -> None:
    parser = argparse.ArgumentParser(prog="concinno")
    sub = parser.add_subparsers(dest="command")
    register(sub)
    # Now `persona` should be parsable.
    ns = parser.parse_args(["persona", "pin", "--state", "x.jsonl", "--content", "y"])
    assert ns.command == "persona"
    assert ns.persona_action == "pin"


def test_pin_then_pinned_round_trip(tmp_path: Path) -> None:
    state = tmp_path / "alice.jsonl"
    rc = pin_memory(str(state), "user is Bob", reason="intro")
    assert rc == 0
    rows = list_pinned(str(state))
    assert any(r["content"] == "user is Bob" for r in rows)


def test_recall_finds_pinned(tmp_path: Path) -> None:
    state = tmp_path / "alice.jsonl"
    pin_memory(str(state), "user is Bob", reason="intro")
    rows = recall_memory(str(state), "Bob", top_k=3)
    assert rows
    assert any("Bob" in r["text"] for r in rows)


def test_run_chat_with_echo_provider(tmp_path: Path) -> None:
    persona = _write_persona(tmp_path)
    state = tmp_path / "alice.jsonl"
    reply = run_chat(
        str(persona),
        state_path=str(state),
        provider="echo",
        message="hello",
    )
    assert "hello" in reply
    # State should have been written.
    assert state.exists()


def test_format_pinned_text_handles_empty() -> None:
    assert format_pinned_text([]) == "(no pinned memories)"


def test_format_recall_text_handles_empty() -> None:
    assert format_recall_text([]) == "(no matches)"


def test_to_json_is_valid() -> None:
    import json

    payload = to_json([{"content": "x", "reason": "y", "pinned_at": "z"}])
    parsed = json.loads(payload)
    assert parsed[0]["content"] == "x"


def test_pin_memory_rejects_empty_content() -> None:
    rc = pin_memory("/tmp/anywhere.jsonl", "")
    assert rc == 2
