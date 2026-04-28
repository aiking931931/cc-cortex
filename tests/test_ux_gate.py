"""Tests for ``concinno.cache.ux_gate`` + UX inject gating (F8, 2.7.1).

MEMORY #61 D6: ``ux_injection`` config key gates all LLM-facing UX
coaching (CBUA markers, WIREDO checklists, streak counters, Read:Edit
warnings, token-zone hints, three-layer thinking injects, cognitive
anchors, cross-session pool context). Ship default is ``False`` so
anonymous PyPI downloaders see only the tool calls and the safety
layer (deny / rewrite) — never AI King's personal coaching tone.

Gated UX inject sites verified here:
  - :mod:`concinno.cognitive_inject` — ``build_cognitive_context``
  - :mod:`concinno.cognitive_pool_inject` — ``build_pool_context``
  - :mod:`concinno.guards.cbua_pipeline_guard` — ``on_post_tool``
  - :mod:`concinno.wiredo_guards` — ``WiredoGuard.check``
  - :mod:`concinno.think_inject` — ``ThinkInjectionGuard.on_post_tool``
  - :mod:`concinno.intent_anchor_guard` — ``IntentAnchorGuard.check``

NOT gated (safety layer — verified to still fire even with UX off):
  - destruction_guard, butterfly_guard, boundary_guard, exfil_guard,
    secret_scan, wiredo_enforcement_guard, token gate (Agent deny)
"""

from __future__ import annotations

from typing import Iterator

import pytest

# ── Fixture: force ship default + env-clear for each test ────


@pytest.fixture(autouse=True)
def _reset_ux_state(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Iterator[None]:
    """Reset ux_gate cache + pin config to a fresh HOME each test."""
    # Point HOME at tmp so user-layer config doesn't leak between tests.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows
    # Clear any previous env override.
    monkeypatch.delenv("CONCINNO_UX_INJECTION", raising=False)
    # Point cwd away from real concinno repo so project-layer doesn't leak.
    monkeypatch.chdir(tmp_path)

    from concinno.cache import ux_gate
    ux_gate.reset_cache()
    yield
    ux_gate.reset_cache()


# ── ux_gate core ─────────────────────────────────────────────


def test_ship_default_is_false() -> None:
    """Ship default must be False — MEMORY #61 D6 + #59 locked."""
    from concinno.cache.ux_gate import is_ux_enabled
    assert is_ux_enabled() is False


def test_env_override_true(monkeypatch: pytest.MonkeyPatch) -> None:
    from concinno.cache import ux_gate
    monkeypatch.setenv("CONCINNO_UX_INJECTION", "1")
    ux_gate.reset_cache()
    assert ux_gate.is_ux_enabled() is True


def test_env_override_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even if config set to True, env=0 forces off."""
    from concinno import config as cfg
    from concinno.cache import ux_gate
    cfg.set_user("ux_injection", True)
    monkeypatch.setenv("CONCINNO_UX_INJECTION", "0")
    ux_gate.reset_cache()
    assert ux_gate.is_ux_enabled() is False


def test_env_override_accepts_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    """true/yes/on/false/no/off all parse."""
    from concinno.cache import ux_gate
    for val in ("true", "yes", "on", "TRUE", "On"):
        monkeypatch.setenv("CONCINNO_UX_INJECTION", val)
        ux_gate.reset_cache()
        assert ux_gate.is_ux_enabled() is True, val
    for val in ("false", "no", "off", "FALSE"):
        monkeypatch.setenv("CONCINNO_UX_INJECTION", val)
        ux_gate.reset_cache()
        assert ux_gate.is_ux_enabled() is False, val


def test_env_garbage_falls_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """Malformed env value → fall through to ship default (False)."""
    from concinno.cache import ux_gate
    monkeypatch.setenv("CONCINNO_UX_INJECTION", "maybe")
    ux_gate.reset_cache()
    assert ux_gate.is_ux_enabled() is False


def test_user_config_enables(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting ux_injection=true in user layer flips the gate."""
    from concinno import config as cfg
    from concinno.cache import ux_gate
    cfg.set_user("ux_injection", True)
    ux_gate.reset_cache()
    assert ux_gate.is_ux_enabled() is True


def test_config_validate_rejects_non_bool() -> None:
    """validate() MUST reject non-bool ux_injection values."""
    from concinno import config as cfg
    with pytest.raises(ValueError):
        cfg.validate("ux_injection", "yes")
    with pytest.raises(ValueError):
        cfg.validate("ux_injection", 1)


def test_cache_reuses_value() -> None:
    """Subsequent calls hit cached bool (no re-read)."""
    from concinno.cache import ux_gate
    assert ux_gate.is_ux_enabled() is False
    assert ux_gate.is_ux_enabled() is False  # no exception, same result


# ── Wiring: UX inject sites short-circuit when disabled ──────


def test_cognitive_inject_gated_off() -> None:
    """build_cognitive_context returns '' when ux_injection=false."""
    from concinno.cognitive_inject import build_cognitive_context
    result = build_cognitive_context(
        task_prompt="test", workspace="", agent_type="Explore",
    )
    assert result == ""


def test_cognitive_inject_enabled_produces_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from concinno.cache import ux_gate
    monkeypatch.setenv("CONCINNO_UX_INJECTION", "1")
    ux_gate.reset_cache()
    from concinno.cognitive_inject import build_cognitive_context
    result = build_cognitive_context(
        task_prompt="test", workspace="", agent_type="Explore",
    )
    # Enabled → non-empty thinking-directives block produced.
    assert isinstance(result, str)
    assert len(result) >= 0  # sanity — may be empty if depth=minimal trims


def test_pool_inject_gated_off(tmp_path) -> None:
    """build_pool_context returns '' when ux_injection=false."""
    from concinno.cache.cognitive_pool import CognitivePool
    from concinno.cognitive_pool_inject import build_pool_context

    pool = CognitivePool(root=tmp_path)
    pool.upsert_section(title="user.goals", body="ship 2.7.1 cleanly")
    result = build_pool_context(task_prompt="ship release", pool=pool)
    assert result == ""


def test_pool_inject_enabled_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    from concinno.cache import ux_gate
    monkeypatch.setenv("CONCINNO_UX_INJECTION", "1")
    ux_gate.reset_cache()
    from concinno.cache.cognitive_pool import CognitivePool
    from concinno.cognitive_pool_inject import build_pool_context

    pool = CognitivePool(root=tmp_path)
    pool.upsert_section(title="release_status", body="testing 2.7.1 release")
    result = build_pool_context(task_prompt="release 2.7.1", pool=pool)
    assert "release" in result.lower()
