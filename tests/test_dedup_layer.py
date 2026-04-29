"""Tests for ``concinno.hooks.dedup_layer`` — 軌 B 件 1 dedup.

Per the 2026-04-29 4-channel commander verdict §3 軌 B 件 1, the dedup
layer must:

* return ``False`` on the first emit of a unique ``(feature, msg)``,
* return ``True`` on every subsequent emit of the same hash within the
  same session, then
* re-allow once :func:`clear_session` is called for that session id.

Tests are stdlib-only and isolate state via ``CONCINNO_HOOK_DEDUP_STATE_PATH``
so they never touch ``~/.concinno/``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from concinno.hooks.dedup_layer import (
    clear_session,
    is_disabled,
    mark_emitted,
    should_dedup,
)


@pytest.fixture
def isolated_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Redirect dedup state to a tmp file and unset all hard switches."""
    state_path = tmp_path / "hook_dedup_session.json"
    monkeypatch.setenv("CONCINNO_HOOK_DEDUP_STATE_PATH", str(state_path))
    monkeypatch.delenv("CONCINNO_HABITUATION_DISABLED", raising=False)
    monkeypatch.delenv("CONCINNO_HOOK_DEDUP_DISABLED", raising=False)
    return state_path


def test_first_emit_is_not_dedup(isolated_state: Path) -> None:
    assert should_dedup("post_tool_critical", "ctx 100k", session_id="s1") is False


def test_second_emit_same_session_is_dedup(isolated_state: Path) -> None:
    mark_emitted("post_tool_critical", "ctx 100k", session_id="s1")
    assert should_dedup("post_tool_critical", "ctx 100k", session_id="s1") is True


def test_different_message_does_not_dedup(isolated_state: Path) -> None:
    mark_emitted("post_tool_critical", "ctx 100k", session_id="s1")
    assert should_dedup("post_tool_critical", "ctx 200k", session_id="s1") is False


def test_different_feature_does_not_dedup(isolated_state: Path) -> None:
    mark_emitted("post_tool_critical", "ctx 100k", session_id="s1")
    assert should_dedup("token_monitor", "ctx 100k", session_id="s1") is False


def test_session_boundary_clears_state(isolated_state: Path) -> None:
    mark_emitted("streak_ux", "🔥 x5", session_id="s1")
    assert should_dedup("streak_ux", "🔥 x5", session_id="s1") is True
    clear_session("s1")
    assert should_dedup("streak_ux", "🔥 x5", session_id="s1") is False


def test_clear_session_does_not_affect_other_session(isolated_state: Path) -> None:
    mark_emitted("streak_ux", "msg", session_id="s1")
    mark_emitted("streak_ux", "msg", session_id="s2")
    clear_session("s1")
    assert should_dedup("streak_ux", "msg", session_id="s1") is False
    assert should_dedup("streak_ux", "msg", session_id="s2") is True


def test_normalised_hash_collapses_relay_prefixes(isolated_state: Path) -> None:
    """Same body under different relay shapes still collapses."""
    mark_emitted(
        "cbua_pipeline", "[SHOW USER VERBATIM] body", session_id="s1",
    )
    # Same body without relay token should hit the same hash.
    assert should_dedup("cbua_pipeline", "body", session_id="s1") is True


def test_normalised_hash_collapses_concinno_brand(isolated_state: Path) -> None:
    mark_emitted(
        "cbua_pipeline",
        "[SHOW USER VERBATIM] [Concinno: cbua_pipeline] body",
        session_id="s1",
    )
    assert should_dedup("cbua_pipeline", "body", session_id="s1") is True


def test_disabled_via_env_returns_false(
    isolated_state: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONCINNO_HABITUATION_DISABLED", "1")
    mark_emitted("x", "y", session_id="s1")
    assert should_dedup("x", "y", session_id="s1") is False
    assert is_disabled() is True


def test_dedup_specific_disable(
    isolated_state: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONCINNO_HOOK_DEDUP_DISABLED", "1")
    assert is_disabled() is True


def test_empty_inputs_skip(isolated_state: Path) -> None:
    assert should_dedup("", "msg") is False
    assert should_dedup("feat", "") is False
    # Should be a no-op (no exception).
    mark_emitted("", "msg")
    mark_emitted("feat", "")


def test_session_less_uses_ttl_window(
    isolated_state: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without session_id, dedup falls back to a TTL window."""
    mark_emitted("token_monitor", "100k", session_id="")
    assert (
        should_dedup("token_monitor", "100k", session_id="", ttl_seconds=60.0)
        is True
    )
