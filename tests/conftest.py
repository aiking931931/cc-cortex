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
