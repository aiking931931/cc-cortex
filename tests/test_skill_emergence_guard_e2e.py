"""E2E test for SkillEmergenceGuard wiring into on_post_tool hook.

Per MEMORY #4d island-caller rule, every new module must have at least
one production caller wired in the same wave with an end-to-end test
proving the wire actually works. This test is that proof.

Path under test: ``concinno.hooks.on_post_tool._run_skill_emergence``
fires the guard from the real PostToolUse hook entry point and writes
a draft markdown file to the staging directory.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from concinno.hooks.on_post_tool import _run_skill_emergence


@pytest.fixture
def hook_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Isolate the staging dir + force the feature ON for this test."""
    monkeypatch.setenv("CONCINNO_SKILL_DRAFT_DIR", str(tmp_path))
    monkeypatch.setenv(
        "CONCINNO_SKILL_DRAFT_STATE", str(tmp_path / "_state.json"),
    )
    monkeypatch.setattr(
        "concinno.skills.skill_emergence_guard._is_enabled",
        lambda: True,
    )
    return tmp_path


def test_e2e_hook_wires_guard_to_propose_draft(hook_env: Path) -> None:
    """5 Bash calls with the same shape from the hook → 1 draft on disk.

    Simulates the real PostToolUse stream: the hook entry-point sees
    five identical ``ruff check`` calls. After the third (default
    min_occurrences=3), the guard fires and writes a markdown draft.
    """
    # Force min_occurrences to 3 since this is the spec default
    with mock.patch(
        "concinno.skills.skill_emergence_guard._read_feature_param",
        side_effect=lambda p, default: 0.0 if "cooldown" in p else default,
    ):
        for _ in range(5):
            _run_skill_emergence(
                tool_name="Bash",
                tool_input={"command": "ruff check src/"},
                hook_data={"tool_result": "All checks passed!"},
            )

    md_files = list(hook_env.glob("*.md"))
    assert len(md_files) >= 1, (
        "SkillEmergenceGuard wired into on_post_tool should have written "
        f"≥1 draft markdown into {hook_env}, got {md_files}"
    )
    text = md_files[0].read_text(encoding="utf-8")
    assert "tool_pattern_repeat" in text or "user_correction" in text or (
        "error_to_working" in text
    )
    assert "concinno skill-emerge accept" in text


def test_e2e_hook_silent_when_feature_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabled feature → no markdown written even with 100 calls."""
    monkeypatch.setenv("CONCINNO_SKILL_DRAFT_DIR", str(tmp_path))
    monkeypatch.setenv(
        "CONCINNO_SKILL_DRAFT_STATE", str(tmp_path / "_state.json"),
    )
    monkeypatch.setattr(
        "concinno.skills.skill_emergence_guard._is_enabled",
        lambda: False,
    )
    for _ in range(100):
        _run_skill_emergence(
            tool_name="Bash",
            tool_input={"command": "pytest"},
            hook_data={"tool_result": "ok"},
        )
    md_files = list(tmp_path.glob("*.md"))
    assert md_files == []
