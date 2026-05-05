"""Tests for concinno.handoff_composer — full handoff markdown assembler."""

from __future__ import annotations

import pytest

from concinno.handoff_composer import (
    MANUAL_SECTION_KEYS,
    compose_handoff,
    supported_specialized_for,
)
from concinno.template_router import reset_aliases


@pytest.fixture(autouse=True)
def _reset():
    reset_aliases()
    yield
    reset_aliases()


def _section0_fixture():
    """Stable §0 values that don't depend on the live environment."""
    return {
        "pod_id": "test-pod",
        "ssh_cmd": "ssh test",
        "vault_aliases": "anthropic-main",
        "env_vars": "ANTHROPIC_API_KEY",
        "last_commit": "abc1234 test commit",
        "last_session_id": "sess_test",
        "last_token_usage": "<deferred>",
        "populated_at": "2026-04-26T12:00:00+00:00",
    }


def test_compose_includes_generic():
    body = compose_handoff(
        "totally-unknown-project",
        section0_overrides=_section0_fixture(),
    )
    assert "## §0" in body
    assert "## §5" in body


def test_compose_appends_specialized_for_release():
    body = compose_handoff(
        "concinno",
        section0_overrides=_section0_fixture(),
    )
    assert "§R1" in body
    assert "§R2" in body


def test_compose_appends_multiple_specialized():
    body = compose_handoff(
        "concinno-build",
        section0_overrides=_section0_fixture(),
    )
    assert "§R1" in body
    assert "§I1" in body


def test_compose_substitutes_section0(tmp_path):
    body = compose_handoff(
        "concinno",
        section0_overrides=_section0_fixture(),
    )
    assert "test-pod" in body
    assert "ssh test" in body
    assert "abc1234" in body


def test_compose_substitutes_status():
    body = compose_handoff(
        "concinno",
        manual_sections={"status": {
            "open_count": 3,
            "paused_count": 1,
            "done_count": 7,
            "current_focus": "ship 2.21.0",
        }},
        section0_overrides=_section0_fixture(),
    )
    assert "3 ⬜" in body
    assert "1 ⏸" in body
    assert "7 ✅" in body
    assert "ship 2.21.0" in body


def test_compose_substitutes_manual_next_step():
    body = compose_handoff(
        "concinno",
        manual_sections={"next_step": "run pytest then push"},
        section0_overrides=_section0_fixture(),
    )
    assert "run pytest then push" in body


def test_compose_substitutes_manual_decision_rationale():
    body = compose_handoff(
        "concinno",
        manual_sections={"decision_rationale": "Picked X over Y because Z"},
        section0_overrides=_section0_fixture(),
    )
    assert "Picked X over Y" in body


def test_compose_substitutes_manual_pitfalls():
    body = compose_handoff(
        "concinno",
        manual_sections={"pitfalls": "watch out for foo"},
        section0_overrides=_section0_fixture(),
    )
    assert "watch out for foo" in body


def test_compose_substitutes_manual_pointer_table():
    table = "| Topic | Read this file |\n|---|---|\n| Architecture | docs/arch.md |"
    body = compose_handoff(
        "concinno",
        manual_sections={"pointer_table": table},
        section0_overrides=_section0_fixture(),
    )
    assert "docs/arch.md" in body


def test_compose_no_specialized_when_unknown_project():
    body = compose_handoff(
        "xxx-zzz-yyy-project",
        section0_overrides=_section0_fixture(),
    )
    # Generic only; no specialized section markers
    for prefix in ("§B1", "§R1", "§S1", "§I1"):
        assert prefix not in body


def test_compose_uses_router_with_meta():
    body = compose_handoff(
        "neutral-project-name",
        extra_template_meta={"recent_commits": ["release: bump v1"]},
        section0_overrides=_section0_fixture(),
    )
    assert "§R1" in body  # release pulled in via commit hint


def test_compose_returns_nonempty():
    body = compose_handoff(
        "anything",
        section0_overrides=_section0_fixture(),
    )
    assert body.strip()


def test_supported_specialized_for_matches_classify():
    out = supported_specialized_for("concinno-build")
    assert "release" in out
    assert "build" in out


def test_manual_section_keys_documented():
    expected = {
        "decision_rationale",
        "pitfalls",
        "pointer_table",
        "next_step",
        "status",
    }
    assert set(MANUAL_SECTION_KEYS) == expected
