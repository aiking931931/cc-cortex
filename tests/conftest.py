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


# 4.0.0 (default-off feature gates): the SHIP-LEVEL default for every
# blocker feature flips to ``enabled=False``. The legacy test suites
# were written assuming guards-default-ON and assert e.g. "this
# malicious prompt MUST be blocked" without explicitly setting up the
# gate. Rather than touching ~hundreds of pre-existing tests, this
# autouse fixture re-enables every ``DEFAULT_OFF_4_0_0`` feature for
# the test process via process-wide config patching.
#
# Skip-list: tests that own the default-off invariants themselves —
# they manage ``enabled`` per-test and would fight this fixture.
@pytest.fixture(autouse=True)
def _restore_default_on_for_legacy_tests(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """Pin every 4.0.0 default-off feature back to ``enabled=True``
    for legacy tests that pre-date the flip.

    The fixture monkey-patches ``Config.feature`` so that any call
    asking for ``key="enabled"`` on a ``DEFAULT_OFF_4_0_0`` feature
    returns True UNLESS the test explicitly set the underlying
    config to False (we still let user-level overrides win).
    """
    module_file = (request.node.fspath.basename
                   if hasattr(request.node, "fspath") else "")
    if module_file in {
        "test_config.py",  # owns config default invariants
        "test_config_loader.py",  # owns config default invariants
        "test_feature_config.py",  # owns FEATURE_META invariants
        "test_feature_meta_schema_v2_36.py",  # schema invariants
        "test_default_off_4_0_0.py",  # 4.0.0 defaults regression test
        "test_feature_enabled_wiring.py",  # wiring uses explicit state
        "test_feature_enabled_wiring_part2.py",  # wiring uses explicit state
        "test_dspy_optimizer.py",  # tests default-off behavior of dspy_prompt_optimization
    }:
        return
    try:
        from concinno.core.config import Config
        from concinno.feature_config import DEFAULT_OFF_4_0_0
    except Exception:
        return

    original_feature = Config.feature

    def patched_feature(self, name, key="enabled"):
        # Force legacy default-on for the 27 4.0.0-flipped features
        # regardless of user-level cc_config.json (the developer running
        # pytest may have set ALL of them to enabled=False locally —
        # legitimate runtime config, illegitimate test fixture). Tests
        # that want the real flipped behaviour live in
        # ``test_default_off_4_0_0.py`` (skip-listed above).
        if key == "enabled" and name in DEFAULT_OFF_4_0_0:
            return True
        return original_feature(self, name, key)

    monkeypatch.setattr(Config, "feature", patched_feature)
