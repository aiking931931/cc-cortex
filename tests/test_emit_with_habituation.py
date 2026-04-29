"""Tests for ``emit_with_habituation`` — 軌 B integration helper.

Verifies the four-layer composition order:
1. dedup_layer.should_dedup → returns "" on duplicate
2. auto_demote.current_tier == SILENT_LOG → returns ""
3. ziq_hook_ignore_rate.record_emit → registers pending
4. with_feature_prefix → standard relay shape
"""

from __future__ import annotations

from pathlib import Path

import pytest

from concinno.hooks.relay_helpers import emit_with_habituation
from concinno.ziq_hook_ignore_rate import pending_emits


@pytest.fixture
def isolated_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Isolate every 軌 B state file."""
    monkeypatch.setenv(
        "CONCINNO_HOOK_DEDUP_STATE_PATH",
        str(tmp_path / "dedup.json"),
    )
    monkeypatch.setenv(
        "CONCINNO_HOOK_DEMOTE_STATE_PATH",
        str(tmp_path / "demote.json"),
    )
    monkeypatch.setenv(
        "CONCINNO_ZIQ_HOOK_IGNORE_RATE_PATH",
        str(tmp_path / "ftrl.json"),
    )
    monkeypatch.delenv("CONCINNO_HABITUATION_DISABLED", raising=False)
    monkeypatch.delenv("CONCINNO_VERBATIM_RELAY_MODE", raising=False)
    return tmp_path


def test_first_emit_returns_branded_string(isolated_state: Path) -> None:
    out = emit_with_habituation(
        "post_tool_critical", "ctx 100k", session_id="s1", mode="prefix",
    )
    assert "[SHOW USER VERBATIM]" in out
    assert "[Concinno: post_tool_critical]" in out
    assert "ctx 100k" in out


def test_duplicate_emit_returns_empty(isolated_state: Path) -> None:
    emit_with_habituation(
        "post_tool_critical", "ctx 100k", session_id="s1", mode="prefix",
    )
    out = emit_with_habituation(
        "post_tool_critical", "ctx 100k", session_id="s1", mode="prefix",
    )
    assert out == ""


def test_silent_log_tier_returns_empty(
    isolated_state: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the feature is auto-demoted to SILENT_LOG, helper skips emit."""
    from concinno.hooks.auto_demote import record_ignore

    # Force tier all the way down (3 ignores per tier × 3 tiers).
    for _ in range(9):
        record_ignore("noisy")
    out = emit_with_habituation(
        "noisy", "msg", session_id="s1", mode="prefix",
    )
    assert out == ""


def test_emit_registers_pending_ftrl_verdict(isolated_state: Path) -> None:
    emit_with_habituation(
        "token_monitor", "100k threshold", session_id="s1", mode="prefix",
    )
    queue = pending_emits()
    assert len(queue) == 1
    assert queue[0]["feature"] == "token_monitor"


def test_off_mode_returns_empty_no_state_change(
    isolated_state: Path,
) -> None:
    out = emit_with_habituation(
        "x", "y", session_id="s1", mode="off",
    )
    assert out == ""
    # No pending emit registered (mode=off bypasses everything).
    assert pending_emits() == []
