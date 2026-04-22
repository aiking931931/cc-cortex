"""tests.test_meta_skills_cross_channel — CrossChannelMemoryBridge unit tests.

Verifies:
  - record → jsonl append per channel
  - fetch_context excludes current channel by default, includes ★ globally
  - user_id isolation via directory scoping
  - mark_milestone promotes and persists across reopens
  - Safe id coercion rejects empty / path-traversal ids
"""

from __future__ import annotations

from pathlib import Path

import pytest

from concinno.meta_skills.cross_channel import (
    CrossChannelMemoryBridge,
    MemoryEntry,
)


def _bridge(
    root: Path,
    *,
    user: str = "alice",
    channels: list[str] | None = None,
) -> CrossChannelMemoryBridge:
    return CrossChannelMemoryBridge(
        channels or ["discord", "gmail", "telegram"],
        user_id=user,
        root=root,
    )


def test_record_appends_jsonl(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path)
    e1 = bridge.record("discord", "hello")
    e2 = bridge.record("discord", "world")
    channel_file = tmp_path / "alice" / "discord.jsonl"
    assert channel_file.exists()
    lines = channel_file.read_text("utf-8").strip().splitlines()
    assert len(lines) == 2
    assert e1.entry_id != e2.entry_id
    assert e1.channel == "discord"


def test_fetch_excludes_current_channel_by_default(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path)
    bridge.record("discord", "in_discord_only")
    bridge.record("gmail", "from_gmail")
    bridge.record("telegram", "from_telegram")
    ctx = bridge.fetch_context("discord")
    assert "in_discord_only" not in ctx
    assert "from_gmail" in ctx
    assert "from_telegram" in ctx


def test_fetch_includes_current_when_requested(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path)
    bridge.record("discord", "self_msg")
    bridge.record("gmail", "other_msg")
    ctx = bridge.fetch_context("discord", include_current=True)
    assert "self_msg" in ctx
    assert "other_msg" in ctx


def test_starred_entries_cross_channel(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path)
    bridge.record("discord", "just_chat")
    e_star = bridge.record("gmail", "MILESTONE_A", starred=True)
    bridge.record("telegram", "unrelated")
    # Fetching from the SAME channel as the star should still surface it.
    ctx = bridge.fetch_context("gmail")
    assert "MILESTONE_A" in ctx
    assert "★" in ctx
    assert e_star.starred is True


def test_user_id_isolation(tmp_path: Path) -> None:
    alice = _bridge(tmp_path, user="alice")
    bob = _bridge(tmp_path, user="bob")
    alice.record("discord", "alice_secret")
    bob.record("discord", "bob_secret")
    # Core isolation claim — one user doesn't see the other's channel data.
    alice_full = alice.list_channel("discord")
    bob_full = bob.list_channel("discord")
    assert len(alice_full) == 1
    assert len(bob_full) == 1
    assert alice_full[0].message == "alice_secret"
    assert bob_full[0].message == "bob_secret"


def test_mark_milestone_promotes(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path)
    entry = bridge.record("discord", "about_to_star")
    assert entry.starred is False
    ok = bridge.mark_milestone("discord", entry.entry_id)
    assert ok is True
    # Reopen a fresh bridge to confirm persistence.
    fresh = _bridge(tmp_path)
    entries = fresh.list_channel("discord")
    assert len(entries) == 1
    assert entries[0].starred is True


def test_mark_milestone_missing_returns_false(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path)
    bridge.record("discord", "anything")
    assert bridge.mark_milestone("discord", "no_such_id") is False


def test_unknown_channel_rejected(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path)
    with pytest.raises(KeyError):
        bridge.record("slack", "never_registered")


def test_empty_channel_list_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        CrossChannelMemoryBridge([], user_id="x", root=tmp_path)


def test_unsafe_user_id_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        CrossChannelMemoryBridge(["discord"], user_id="...", root=tmp_path)


def test_path_traversal_sanitised(tmp_path: Path) -> None:
    # Slashes + dots collapse to underscores, no escape.
    bridge = CrossChannelMemoryBridge(
        ["discord"], user_id="a/b/../c", root=tmp_path
    )
    bridge.record("discord", "safe_write")
    # User dir must be a direct child of root.
    children = [p for p in tmp_path.iterdir() if p.is_dir()]
    assert len(children) == 1
    assert children[0].parent == tmp_path


def test_long_message_truncated(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path)
    long_msg = "x" * 10_000
    entry = bridge.record("discord", long_msg)
    assert entry.message.endswith("…[truncated]")
    assert len(entry.message) < 5000


def test_memory_entry_roundtrip() -> None:
    orig = MemoryEntry(
        entry_id="abc",
        channel="discord",
        ts=123.4,
        message="hi",
        starred=True,
        metadata={"k": "v"},
    )
    serialized = orig.to_jsonable()
    restored = MemoryEntry.from_jsonable(serialized)
    assert restored == orig


def test_list_starred_global(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path)
    bridge.record("discord", "normal_a")
    bridge.record("gmail", "star_b", starred=True)
    bridge.record("telegram", "star_c", starred=True)
    stars = bridge.list_starred()
    assert [e.message for e in stars] == ["star_b", "star_c"]


def test_malformed_jsonl_line_skipped(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path)
    bridge.record("discord", "good")
    # Sneak a bad line into the file.
    f = tmp_path / "alice" / "discord.jsonl"
    with f.open("a", encoding="utf-8") as fh:
        fh.write("not json\n")
    entries = bridge.list_channel("discord")
    assert len(entries) == 1
    assert entries[0].message == "good"
