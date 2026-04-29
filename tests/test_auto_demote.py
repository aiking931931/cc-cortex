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


# ── F3 ship-fix wave: production wiring of record_ignore ─────────


def test_correction_feeds_record_ignore_via_on_prompt_submit(
    isolated_state: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end F3 wiring: user correction fans the pending queue
    of :mod:`concinno.ziq_hook_ignore_rate` into per-feature
    ``record_ignore`` calls so the auto-demote tier ladder actually
    moves in production.

    Without this wiring the tier ladder is dead code per FATAL-3 in
    ``2026-04-29-4-6-0-ship-redteam-attack.md`` §2.3.
    """
    from concinno.hooks.on_prompt_submit import (
        _feed_correction_into_auto_demote,
    )
    from concinno.ziq_hook_ignore_rate import record_emit

    # Isolate FTRL state to a tmp file as well so two separate state
    # files do not pollute each other's assertions.
    ftrl_state = tmp_path / "ziq_hook_ignore_rate.json"
    monkeypatch.setenv(
        "CONCINNO_ZIQ_HOOK_IGNORE_RATE_PATH", str(ftrl_state),
    )

    # Simulate three hook fires in the same session.
    record_emit("post_tool_critical", session_id="S-fix3")
    record_emit("post_tool_critical", session_id="S-fix3")
    record_emit("streak_ux", session_id="S-fix3")

    # Default tier == CRITICAL before the correction signal.
    assert current_tier("post_tool_critical") == "CRITICAL"
    assert current_tier("streak_ux") == "CRITICAL"

    # Three back-to-back corrections should step both features down.
    for _ in range(3):
        _feed_correction_into_auto_demote(session_id="S-fix3")

    # Both features visited the tier ladder — we expect at least one
    # demotion (the wiring fires record_ignore per pending feature
    # de-duped to once per turn).
    assert current_tier("post_tool_critical") == "HIGH"
    assert current_tier("streak_ux") == "HIGH"


def test_correction_wire_skips_other_sessions(
    isolated_state: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A correction in session A must not demote pendings from session B."""
    from concinno.hooks.on_prompt_submit import (
        _feed_correction_into_auto_demote,
    )
    from concinno.ziq_hook_ignore_rate import record_emit

    ftrl_state = tmp_path / "ziq_hook_ignore_rate.json"
    monkeypatch.setenv(
        "CONCINNO_ZIQ_HOOK_IGNORE_RATE_PATH", str(ftrl_state),
    )

    record_emit("foreign_feature", session_id="S-other")
    for _ in range(5):
        _feed_correction_into_auto_demote(session_id="S-mine")

    assert current_tier("foreign_feature") == "CRITICAL"
