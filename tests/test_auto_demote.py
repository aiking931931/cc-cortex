"""Tests for ``concinno.hooks.auto_demote`` — 軌 B 件 2 tier auto-demote.

Per the 2026-04-29 4-channel commander verdict §3 軌 B 件 2:

* default tier is ``CRITICAL``,
* N=3 consecutive ``record_ignore`` calls step the tier down one rung,
* ``record_accept`` resets the consecutive-ignore counter,
* tier ladder = ``CRITICAL → HIGH → NORMAL → SILENT_LOG`` (sticky at
  ``SILENT_LOG`` — never below).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from concinno.hooks.auto_demote import (
    TIERS,
    current_tier,
    is_disabled,
    record_accept,
    record_ignore,
    reset,
)


@pytest.fixture
def isolated_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Path:
    state_path = tmp_path / "hook_demote_state.json"
    monkeypatch.setenv("CONCINNO_HOOK_DEMOTE_STATE_PATH", str(state_path))
    monkeypatch.delenv("CONCINNO_HABITUATION_DISABLED", raising=False)
    monkeypatch.delenv("CONCINNO_HOOK_AUTO_DEMOTE_DISABLED", raising=False)
    monkeypatch.delenv(
        "CONCINNO_HABITUATION_IGNORE_THRESHOLD", raising=False,
    )
    return state_path


def test_default_tier_is_critical(isolated_state: Path) -> None:
    assert current_tier("any_feature") == "CRITICAL"


def test_three_ignores_demote_critical_to_high(isolated_state: Path) -> None:
    record_ignore("post_tool_critical")
    record_ignore("post_tool_critical")
    tier = record_ignore("post_tool_critical")
    assert tier == "HIGH"
    assert current_tier("post_tool_critical") == "HIGH"


def test_six_ignores_demote_to_normal(isolated_state: Path) -> None:
    for _ in range(6):
        record_ignore("streak_ux")
    assert current_tier("streak_ux") == "NORMAL"


def test_nine_ignores_demote_to_silent_log(isolated_state: Path) -> None:
    for _ in range(9):
        record_ignore("token_monitor")
    assert current_tier("token_monitor") == "SILENT_LOG"


def test_silent_log_is_terminal(isolated_state: Path) -> None:
    """Already at SILENT_LOG — further ignores stay at SILENT_LOG."""
    for _ in range(15):
        record_ignore("noisy")
    assert current_tier("noisy") == "SILENT_LOG"


def test_accept_resets_counter(isolated_state: Path) -> None:
    record_ignore("post_tool_critical")
    record_ignore("post_tool_critical")
    record_accept("post_tool_critical")
    # After 2 ignores + 1 accept, the next ignore should NOT demote.
    record_ignore("post_tool_critical")
    assert current_tier("post_tool_critical") == "CRITICAL"


def test_accept_does_not_promote_demoted_tier(
    isolated_state: Path,
) -> None:
    """Accept resets counter but tier stays demoted."""
    for _ in range(3):
        record_ignore("x")
    assert current_tier("x") == "HIGH"
    record_accept("x")
    assert current_tier("x") == "HIGH"


def test_threshold_env_override(
    isolated_state: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONCINNO_HABITUATION_IGNORE_THRESHOLD", "1")
    tier = record_ignore("aggressive_feature")
    assert tier == "HIGH"


def test_disabled_via_env_keeps_critical(
    isolated_state: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONCINNO_HABITUATION_DISABLED", "1")
    for _ in range(10):
        record_ignore("x")
    assert current_tier("x") == "CRITICAL"
    assert is_disabled() is True


def test_specific_disable_keeps_critical(
    isolated_state: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONCINNO_HOOK_AUTO_DEMOTE_DISABLED", "1")
    for _ in range(10):
        record_ignore("x")
    assert current_tier("x") == "CRITICAL"


def test_reset_individual_feature(isolated_state: Path) -> None:
    for _ in range(3):
        record_ignore("a")
    for _ in range(3):
        record_ignore("b")
    assert current_tier("a") == "HIGH"
    assert current_tier("b") == "HIGH"
    reset("a")
    assert current_tier("a") == "CRITICAL"
    assert current_tier("b") == "HIGH"


def test_reset_all(isolated_state: Path) -> None:
    for _ in range(3):
        record_ignore("a")
    reset()
    assert current_tier("a") == "CRITICAL"


def test_tiers_constant_shape() -> None:
    assert TIERS == ("CRITICAL", "HIGH", "NORMAL", "SILENT_LOG")


def test_empty_feature_name_is_safe(isolated_state: Path) -> None:
    assert current_tier("") == "CRITICAL"
    assert record_ignore("") == "CRITICAL"
    assert record_accept("") == "CRITICAL"
