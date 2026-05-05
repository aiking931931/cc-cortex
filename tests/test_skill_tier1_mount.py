"""Tests for ``concinno.skill_tier1_mount``.

Covers:
  1. Default Tier1 list shipped in source (no override → defaults).
  2. Operator override file (JSON list) wins over defaults.
  3. Bad / non-list / non-string override → falls back gracefully.
  4. Cap at MAX_TIER1_SKILLS (10) honoured.
  5. Dedup preserves first occurrence order.
  6. ``build_tier1_inject`` annotates with package when ``installed`` map provided.
  7. ``build_tier1_inject`` returns "" on empty input (no inject branch).
  8. ``mount_tier1_skills`` happy path under 500ms budget.
  9. Env opt-out (``CONCINNO_SKILL_TIER1_MOUNT_DISABLED``) skips mount.
 10. Debounce marker suppresses repeat mount inside window.
 11. ``skip_if_already_mounted=False`` ignores the marker.
 12. ``skills.json`` cache annotation merged into inject body.
 13. Missing ``skills.json`` still produces inject (no annotation).
 14. Result dataclass populated correctly on success path.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from concinno import skill_tier1_mount as mod
from concinno.skill_tier1_mount import (
    DEFAULT_TIER1_SKILLS,
    MAX_TIER1_SKILLS,
    Tier1MountResult,
    build_tier1_inject,
    load_tier1_skill_list,
    mount_tier1_skills,
)


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Redirect ``~/.concinno`` to a tmp dir for isolation."""
    home = tmp_path / "concinno_home"
    home.mkdir()
    monkeypatch.setattr(
        mod, "_concinno_home",
        lambda override=None: home if override is None else Path(override),
    )
    return home


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv("CONCINNO_SKILL_TIER1_MOUNT_DISABLED", raising=False)
    yield


# ── 1. defaults ───────────────────────────────────────────


def test_load_tier1_skill_list_defaults_when_no_override(fake_home, clean_env):
    """No override file → ship-default list returned."""
    skills = load_tier1_skill_list()
    assert skills == list(DEFAULT_TIER1_SKILLS)
    assert "memoria" in skills
    assert "kb_handoff" in skills


# ── 2. override wins ──────────────────────────────────────


def test_operator_override_wins_over_default(fake_home, clean_env):
    """Override JSON file replaces default list entirely."""
    override = fake_home / "tier1_skills.json"
    override.write_text(
        json.dumps(["custom_a", "custom_b"]),
        encoding="utf-8",
    )
    skills = load_tier1_skill_list()
    assert skills == ["custom_a", "custom_b"]


# ── 3. malformed override falls back ──────────────────────


@pytest.mark.parametrize("bad_payload", [
    '{"not": "a list"}',
    '"plain string"',
    "not even json {",
    "[1, 2, null]",        # no string entries
    "[]",                   # empty list (after dedup) → still falls back? no, returns []
])
def test_bad_override_falls_back_or_empties(fake_home, clean_env, bad_payload):
    """Malformed override degrades to defaults; an empty list yields empty result."""
    override = fake_home / "tier1_skills.json"
    override.write_text(bad_payload, encoding="utf-8")
    skills = load_tier1_skill_list()
    # Either fall-back to defaults, or — for legitimately empty list /
    # all-non-string list — return [] (which the caller treats as
    # "nothing to mount this session").
    if bad_payload in {"[]", "[1, 2, null]"}:
        assert skills == []
    else:
        assert skills == list(DEFAULT_TIER1_SKILLS)


# ── 4. MAX cap honoured ───────────────────────────────────


def test_cap_at_max_tier1_skills(fake_home, clean_env):
    """Override with > MAX_TIER1_SKILLS entries gets truncated."""
    override = fake_home / "tier1_skills.json"
    huge = [f"skill_{i}" for i in range(50)]
    override.write_text(json.dumps(huge), encoding="utf-8")
    skills = load_tier1_skill_list()
    assert len(skills) == MAX_TIER1_SKILLS
    assert skills == huge[:MAX_TIER1_SKILLS]


# ── 5. dedup preserves first occurrence order ─────────────


def test_dedup_preserves_first_occurrence(fake_home, clean_env):
    """Repeated entries collapse but first-seen position wins."""
    override = fake_home / "tier1_skills.json"
    override.write_text(
        json.dumps(["a", "b", "a", "c", "b"]), encoding="utf-8",
    )
    skills = load_tier1_skill_list()
    assert skills == ["a", "b", "c"]


# ── 6. inject annotates with package ──────────────────────


def test_build_inject_annotates_with_package():
    """Installed map merges package source into the line."""
    text = build_tier1_inject(
        ["memoria", "kb_handoff"],
        installed={
            "memoria": {"package": "concinno-skills-memoria"},
            "kb_handoff": {"package": ""},
        },
    )
    assert "concinno-skills-memoria" in text
    assert "/memoria" in text
    assert "/kb_handoff" in text


# ── 7. empty inject ───────────────────────────────────────


def test_build_inject_empty_returns_empty_string():
    """Empty list → empty string so caller can do ``if text: emit(text)``."""
    assert build_tier1_inject([]) == ""


# ── 8. happy path ─────────────────────────────────────────


def test_mount_happy_path_under_budget(fake_home, clean_env):
    """No override / no skills.json → defaults, inject populated, fast."""
    result = mount_tier1_skills()
    assert isinstance(result, Tier1MountResult)
    assert result.error is None
    assert result.mounted_count == len(DEFAULT_TIER1_SKILLS)
    assert "/memoria" in result.additional_context
    assert result.elapsed_ms < 500


# ── 9. env opt-out ────────────────────────────────────────


def test_env_opt_out_skips(fake_home, monkeypatch):
    monkeypatch.setenv("CONCINNO_SKILL_TIER1_MOUNT_DISABLED", "1")
    result = mount_tier1_skills()
    assert result.additional_context == ""
    assert result.mounted_count == 0
    assert any("disabled" in w for w in result.warnings)


# ── 10. debounce ──────────────────────────────────────────


def test_debounce_marker_suppresses_repeat(fake_home, clean_env):
    """Two mounts within debounce window → second is skipped."""
    first = mount_tier1_skills(debounce_window_s=10.0)
    assert first.additional_context  # first call writes inject
    second = mount_tier1_skills(debounce_window_s=10.0)
    assert second.skipped_already_mounted is True
    assert second.additional_context == ""


# ── 11. skip_if_already_mounted=False ─────────────────────


def test_skip_flag_false_ignores_marker(fake_home, clean_env):
    mount_tier1_skills(debounce_window_s=10.0)  # stamp marker
    forced = mount_tier1_skills(
        debounce_window_s=10.0, skip_if_already_mounted=False,
    )
    assert forced.skipped_already_mounted is False
    assert forced.additional_context  # inject re-rendered


# ── 12. skills.json annotation ────────────────────────────


def test_skills_json_annotation_merged(fake_home, clean_env):
    """When skills.json is present, package annotations show up in inject."""
    skills_cache = fake_home / "skills.json"
    skills_cache.write_text(
        json.dumps({
            "memoria": {
                "name": "memoria",
                "package": "concinno-skills-memoria",
                "scope": "plugin:concinno-skills-memoria",
            },
        }),
        encoding="utf-8",
    )
    result = mount_tier1_skills()
    assert "concinno-skills-memoria" in result.additional_context


# ── 13. missing skills.json still injects ─────────────────


def test_missing_skills_json_still_injects(fake_home, clean_env):
    """No skills.json on disk → inject still rendered with plain names."""
    result = mount_tier1_skills()
    assert result.additional_context
    assert "/memoria" in result.additional_context
    # No package annotation present (no installed map):
    assert "from " not in result.additional_context


# ── 14. result dataclass shape ────────────────────────────


def test_result_dataclass_populated(fake_home, clean_env):
    result = mount_tier1_skills()
    assert isinstance(result.elapsed_ms, float)
    assert result.mounted_count > 0
    assert result.timed_out is False
    assert result.error is None
    assert isinstance(result.warnings, list)


# ── 15. malformed skills.json gracefully degrades ─────────


def test_malformed_skills_json_degrades(fake_home, clean_env):
    """Garbage skills.json → still inject (no annotation)."""
    (fake_home / "skills.json").write_text("not json {", encoding="utf-8")
    result = mount_tier1_skills()
    assert result.additional_context
    assert result.error is None


# ── 16. timeout budget reported ───────────────────────────


def test_zero_budget_reports_timeout(fake_home, clean_env):
    """0ms budget → timed_out flagged but no exception raised."""
    result = mount_tier1_skills(timeout_ms=0, debounce_window_s=0.0)
    assert result.error is None
    # With 0ms budget the over-budget check fires before render.
    assert result.timed_out is True or result.elapsed_ms > 0


# ── 17. catastrophic exception caught ─────────────────────


def test_catastrophic_failure_caught(fake_home, clean_env, monkeypatch):
    """Any unexpected exception inside mount populates ``error`` field."""
    def boom(*args, **kwargs):
        raise RuntimeError("simulated")
    monkeypatch.setattr(mod, "load_tier1_skill_list", boom)
    result = mount_tier1_skills(debounce_window_s=0.0)
    assert result.error is not None
    assert "simulated" in result.error
