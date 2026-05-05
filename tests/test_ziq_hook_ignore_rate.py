"""Tests for ``concinno.ziq_hook_ignore_rate`` — 軌 B 件 3 FTRL.

Per the 2026-04-29 4-channel commander verdict §3 軌 B 件 3 (Hermes
4-cap §E.1 reconciliation), the 5th ZIQ outcome namespace
``ziq.outcome.hook_ignore_rate`` learns per-hook accept-rate from
next-turn user-correction signals (per F7 fix — NOT behaviour-shifted,
to avoid Goodhart inflation).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from concinno.ziq_hook_ignore_rate import (
    NAMESPACE,
    ftrl_state_path,
    hook_accept_rate,
    is_disabled,
    pending_emits,
    record_emit,
    record_user_accept_signal,
)


@pytest.fixture
def isolated_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Path:
    state_path = tmp_path / "ziq_hook_ignore_rate.json"
    monkeypatch.setenv("CONCINNO_ZIQ_HOOK_IGNORE_RATE_PATH", str(state_path))
    monkeypatch.delenv("CONCINNO_HABITUATION_DISABLED", raising=False)
    monkeypatch.delenv(
        "CONCINNO_ZIQ_HOOK_IGNORE_RATE_DISABLED", raising=False,
    )
    return state_path


def test_namespace_constant() -> None:
    assert NAMESPACE == "ziq.outcome.hook_ignore_rate"


def test_state_path_uses_env(isolated_state: Path) -> None:
    assert ftrl_state_path() == isolated_state


def test_no_samples_default_rate_is_uninformed_prior(
    isolated_state: Path,
) -> None:
    assert hook_accept_rate("never_seen") == 0.5


def test_record_emit_queues_pending(isolated_state: Path) -> None:
    record_emit("post_tool_critical", session_id="s1")
    pending = pending_emits()
    assert len(pending) == 1
    assert pending[0]["feature"] == "post_tool_critical"
    assert pending[0]["session_id"] == "s1"


def test_user_silent_signal_increases_accept_rate(
    isolated_state: Path,
) -> None:
    record_emit("token_monitor", session_id="s1")
    rate_before = hook_accept_rate("token_monitor")
    record_user_accept_signal(user_corrected=False, session_id="s1")
    rate_after = hook_accept_rate("token_monitor")
    assert rate_after > rate_before  # 1.0 reward applied


def test_user_correction_decreases_accept_rate(
    isolated_state: Path,
) -> None:
    record_emit("streak_ux", session_id="s1")
    rate_before = hook_accept_rate("streak_ux")
    record_user_accept_signal(user_corrected=True, session_id="s1")
    rate_after = hook_accept_rate("streak_ux")
    assert rate_after < rate_before  # 0.0 reward applied


def test_user_signal_clears_pending(isolated_state: Path) -> None:
    record_emit("a", session_id="s1")
    record_emit("b", session_id="s1")
    record_user_accept_signal(user_corrected=False, session_id="s1")
    assert pending_emits() == []


def test_user_signal_filters_by_session(isolated_state: Path) -> None:
    record_emit("a", session_id="s1")
    record_emit("b", session_id="s2")
    record_user_accept_signal(user_corrected=True, session_id="s1")
    pending = pending_emits()
    # s2 entry should still be pending.
    assert len(pending) == 1
    assert pending[0]["session_id"] == "s2"


def test_state_persists_across_calls(isolated_state: Path) -> None:
    record_emit("token_monitor", session_id="s1")
    record_user_accept_signal(user_corrected=False, session_id="s1")
    rate = hook_accept_rate("token_monitor")
    # Re-import simulation: state file should have been written and
    # next read picks the same weight.
    record_emit("token_monitor", session_id="s2")
    assert hook_accept_rate("token_monitor") == rate


def test_repeated_silent_converges_toward_one(isolated_state: Path) -> None:
    for _ in range(50):
        record_emit("good_hook", session_id="s1")
        record_user_accept_signal(user_corrected=False, session_id="s1")
    assert hook_accept_rate("good_hook") > 0.7


def test_repeated_correction_converges_toward_zero(
    isolated_state: Path,
) -> None:
    """100 iterations of user_corrected=True should drag rate below 0.3.

    With alpha=0.1, decay=0.99, starting at 0.5 the EMA needs ~80 steps
    to drop below 0.3. We use 100 to leave headroom for floating-point
    drift without making the test sensitive to small param changes.
    """
    for _ in range(100):
        record_emit("noisy_hook", session_id="s1")
        record_user_accept_signal(user_corrected=True, session_id="s1")
    assert hook_accept_rate("noisy_hook") < 0.3


def test_disabled_via_env_skips_updates(
    isolated_state: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONCINNO_HABITUATION_DISABLED", "1")
    record_emit("x", session_id="s1")
    record_user_accept_signal(user_corrected=False, session_id="s1")
    assert hook_accept_rate("x") == 0.5
    assert is_disabled() is True


def test_specific_disable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONCINNO_ZIQ_HOOK_IGNORE_RATE_DISABLED", "1")
    assert is_disabled() is True


def test_empty_feature_skipped(isolated_state: Path) -> None:
    record_emit("", session_id="s1")
    assert pending_emits() == []
