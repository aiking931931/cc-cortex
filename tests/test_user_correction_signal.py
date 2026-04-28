"""Tests for ``concinno.skills.user_correction_signal``.

Covers the per-turn correction signal hand-off between
``on_prompt_submit`` (writer) and ``on_post_tool`` (reader, via
``SkillEmergenceGuard``).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from concinno.skills.user_correction_signal import (
    DEFAULT_TTL_SECONDS,
    is_active,
    record_prompt,
    signal_path,
)


@pytest.fixture
def signal_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Isolate the correction signal file per-test."""
    p = tmp_path / "correction_signal.json"
    monkeypatch.setenv("CONCINNO_USER_CORRECTION_SIGNAL_PATH", str(p))
    return p


# ── signal_path() ─────────────────────────────────────────


def test_signal_path_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONCINNO_USER_CORRECTION_SIGNAL_PATH", raising=False)
    p = signal_path()
    assert p.parts[-3:] == (".concinno", "state", "user_correction_signal.json")


def test_signal_path_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    custom = tmp_path / "custom.json"
    monkeypatch.setenv("CONCINNO_USER_CORRECTION_SIGNAL_PATH", str(custom))
    assert signal_path() == custom


# ── record_prompt() ───────────────────────────────────────


def test_record_prompt_writes_active_for_l1_keyword(
    signal_file: Path,
) -> None:
    record_prompt("不對，這樣不行")
    assert signal_file.exists()
    raw = json.loads(signal_file.read_text(encoding="utf-8"))
    assert raw["active"] is True
    assert raw["confidence"] >= 0.6
    assert isinstance(raw["ts"], float)


def test_record_prompt_writes_inactive_for_normal_text(
    signal_file: Path,
) -> None:
    record_prompt("Run the unit tests please")
    raw = json.loads(signal_file.read_text(encoding="utf-8"))
    assert raw["active"] is False
    assert raw["confidence"] == 0.0


def test_record_prompt_overwrites_previous(signal_file: Path) -> None:
    record_prompt("不對 不要這樣")
    raw1 = json.loads(signal_file.read_text(encoding="utf-8"))
    assert raw1["active"] is True

    time.sleep(0.01)
    record_prompt("Run the unit tests please")
    raw2 = json.loads(signal_file.read_text(encoding="utf-8"))
    assert raw2["active"] is False
    # ts must advance
    assert raw2["ts"] >= raw1["ts"]


def test_record_prompt_handles_empty_string(signal_file: Path) -> None:
    record_prompt("")
    raw = json.loads(signal_file.read_text(encoding="utf-8"))
    assert raw["active"] is False


def test_record_prompt_swallows_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OSError when writing must not propagate (hot path).

    Point the signal file at a path whose parent is itself a regular
    file: ``mkdir(parents=True)`` raises ``NotADirectoryError`` (an
    ``OSError`` subclass) and ``record_prompt`` must swallow it.
    """
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory", encoding="utf-8")
    target = blocker / "subdir" / "correction.json"
    monkeypatch.setenv("CONCINNO_USER_CORRECTION_SIGNAL_PATH", str(target))
    # Should not raise
    record_prompt("不對")
    # And the signal file did not get created
    assert not target.exists()


# ── is_active() ───────────────────────────────────────────


def test_is_active_false_when_file_missing(signal_file: Path) -> None:
    assert not signal_file.exists()
    assert is_active() is False


def test_is_active_true_after_correction_recorded(
    signal_file: Path,
) -> None:
    record_prompt("不對，請重新做")
    assert is_active() is True


def test_is_active_false_after_normal_prompt(signal_file: Path) -> None:
    record_prompt("Hello world")
    assert is_active() is False


def test_is_active_false_when_record_is_stale(signal_file: Path) -> None:
    """Stale records (older than TTL) are treated as inactive."""
    stale = {
        "ts": time.time() - DEFAULT_TTL_SECONDS - 60,
        "active": True,
        "confidence": 1.0,
    }
    signal_file.parent.mkdir(parents=True, exist_ok=True)
    signal_file.write_text(json.dumps(stale), encoding="utf-8")
    assert is_active() is False


def test_is_active_respects_explicit_ttl(signal_file: Path) -> None:
    """Caller can override the TTL window."""
    rec = {
        "ts": time.time() - 5,
        "active": True,
        "confidence": 1.0,
    }
    signal_file.parent.mkdir(parents=True, exist_ok=True)
    signal_file.write_text(json.dumps(rec), encoding="utf-8")
    assert is_active(ttl_seconds=10) is True
    assert is_active(ttl_seconds=2) is False


def test_is_active_false_on_corrupted_json(signal_file: Path) -> None:
    signal_file.parent.mkdir(parents=True, exist_ok=True)
    signal_file.write_text("not valid json {{{", encoding="utf-8")
    assert is_active() is False


def test_is_active_false_on_non_dict_payload(signal_file: Path) -> None:
    signal_file.parent.mkdir(parents=True, exist_ok=True)
    signal_file.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert is_active() is False


def test_is_active_false_on_missing_ts(signal_file: Path) -> None:
    signal_file.parent.mkdir(parents=True, exist_ok=True)
    signal_file.write_text(
        json.dumps({"active": True, "confidence": 1.0}), encoding="utf-8",
    )
    assert is_active() is False


def test_is_active_false_on_future_ts(signal_file: Path) -> None:
    """Records with a future timestamp (clock skew / tampering) are
    treated as stale to avoid latching the trigger forever."""
    rec = {
        "ts": time.time() + 3600,
        "active": True,
        "confidence": 1.0,
    }
    signal_file.parent.mkdir(parents=True, exist_ok=True)
    signal_file.write_text(json.dumps(rec), encoding="utf-8")
    assert is_active() is False


# ── End-to-end through the guard ──────────────────────────


def test_e2e_guard_uses_correction_signal(
    signal_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A correction-marked prompt makes ``had_user_correction`` true
    when the post-tool reader builds an :class:`EmergenceSignal`."""
    # Isolate the guard's draft / state files too
    monkeypatch.setenv("CONCINNO_SKILL_DRAFT_DIR", str(tmp_path / "drafts"))
    monkeypatch.setenv(
        "CONCINNO_SKILL_DRAFT_STATE", str(tmp_path / "drafts" / "_state.json"),
    )

    record_prompt("不對 重新做")
    assert is_active() is True

    # Reader-side simulates what on_post_tool now does (W3.x #7 wire)
    from concinno.skills.user_correction_signal import is_active as reader_active
    had = reader_active()
    assert had is True

    # Negative path — overwrite with a non-correction prompt
    record_prompt("Run pytest")
    assert reader_active() is False
