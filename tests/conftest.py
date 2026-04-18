"""Shared pytest fixtures for concinno test suite.

Auto-raises destruction_gate escape env flags so tests that exercise
gated functions (cleanup / backup / rollback) pass without plastering
``reason=`` kwargs into every call site. Gate-specific behaviour is
covered by dedicated tests in ``test_destruction_guard.py`` which
explicitly set / unset the flags.
"""

from __future__ import annotations

import os

import pytest

_ESCAPE_FLAGS = (
    "CONCINNO_INLINE_SQUASH",
    "CONCINNO_GIT_GC",
    "CONCINNO_BACKUP_PRUNE",
    "CONCINNO_GIT_ROLLBACK",
    "CONCINNO_STALE_CLEANUP",
    "CONCINNO_LOG_ROTATE",
)


@pytest.fixture(autouse=True)
def _destruction_gate_escape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every test sees the destruction_gate in pass-through mode.

    The gate treats ``CLAUDE_PROJECT_DIR`` + op-specific flag as "hook
    context" and allows the call. Tests that need to verify the gate
    *does* fire (see ``test_destruction_guard.TestDestructionGate``)
    pop the flags themselves via ``monkeypatch.delenv``.
    """
    monkeypatch.setenv(
        "CLAUDE_PROJECT_DIR",
        os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd(),
    )
    for flag in _ESCAPE_FLAGS:
        monkeypatch.setenv(flag, "1")


# 2.7.2 Gap 5: AI King's personal ``~/.concinno/config.json`` (locale=zh-TW,
# mode=handoff) was leaking into the test run and breaking three invariant
# tests that assume PyPI ship defaults (locale=en, mode=general). The env
# layer wins over every JSON layer in :func:`concinno.config.load` so
# pinning the env vars here gives every test the shipped defaults without
# needing to rewrite ``Path.home``. Tests that deliberately exercise the
# config loader (``test_config.py`` / ``test_config_loader.py``) already
# manage these vars themselves, so we skip them to avoid fixture-ordering
# fights.
@pytest.fixture(autouse=True)
def _pin_ship_default_config(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """Force shipped defaults (locale=en, mode=general) for every test.

    Uses env layer overrides because :func:`concinno.config.load`
    resolves env > project > user > default — so even a stale
    ``~/.concinno/config.json`` on the developer's machine cannot
    change the values a test sees.
    """
    module_file = (request.node.fspath.basename
                   if hasattr(request.node, "fspath") else "")
    if module_file in {
        "test_config.py",
        "test_config_loader.py",
    }:
        return
    monkeypatch.setenv("CONCINNO_LOCALE", "en")
    monkeypatch.setenv("CONCINNO_MODE", "general")
    # Reset cached i18n state so previous tests' locale doesn't bleed in.
    try:
        from concinno import i18n
        i18n._loaded = False  # type: ignore[attr-defined]
        i18n._display_locale = ""  # type: ignore[attr-defined]
        i18n._message_cache.clear()  # type: ignore[attr-defined]
        i18n._pattern_cache.clear()  # type: ignore[attr-defined]
    except Exception:
        pass


# F8 (2.7.1): ship-default ``ux_injection=false`` gates every LLM-facing
# UX inject site. Pre-existing tests were written when UX was always on
# and assert that inject sites produce non-empty output. A dedicated
# ``test_ux_gate.py`` verifies the gate itself and overrides this
# fixture locally; everywhere else we force UX on so the legacy
# behaviour under test is reachable.
@pytest.fixture(autouse=True)
def _enable_ux_injection_for_legacy_tests(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """Enable ux_injection for every test except the UX-gate suite itself.

    ``test_ux_gate.py`` owns the ship-default invariants and manages
    the env var on each test; skipping it here prevents a fixture-
    ordering fight between autouse fixtures.
    """
    module_file = (request.node.fspath.basename
                   if hasattr(request.node, "fspath") else "")
    if module_file in {
        "test_ux_gate.py",
        "test_config.py",  # owns config default invariants
        "test_config_loader.py",  # owns config default invariants
    }:
        return
    monkeypatch.setenv("CONCINNO_UX_INJECTION", "1")
    try:
        from concinno.cache import ux_gate
        ux_gate.reset_cache()
    except Exception:
        pass
