"""Tests for ``concinno.cli.new_feature_cmd`` — 9-phase design scaffold.

Added in 2.17.0. The CLI scaffolds a new feature (skill / subpackage /
guard / cli / module) + drops a ``docs/<name>-design.md`` file with the
9-phase pipeline checklist and a 6-point DoD table. Tests cover:

  - kind=skill, kind=subpackage structural expectations
  - radius=chaotic → red-blue phase marked mandatory; otherwise optional
  - --dry-run prints plan, writes nothing
  - Existing target → exits 2 with clear error
  - Design doc has all 9 phase headers + DoD + commander verdict rows

All tests use pytest ``tmp_path`` — zero real-filesystem writes in the
repo during the test run.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from concinno.cli.new_feature_cmd import (
    _handle,
    build_plan,
    render_design_doc,
)


def _ns(**kw: object) -> argparse.Namespace:
    """Build an argparse.Namespace with sensible defaults for _handle."""
    defaults: dict[str, object] = {
        "name": "demo",
        "kind": "skill",
        "radius": "complicated",
        "dir": "",
        "dry_run": False,
    }
    defaults.update(kw)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# Kind=skill
# ---------------------------------------------------------------------------


def test_skill_kind_creates_three_skill_files_plus_design(tmp_path: Path) -> None:
    """kind=skill: scaffolds <name>/{SKILL.md, pipeline.md, dod-checklist.md}."""
    rc = _handle(_ns(name="demo-skill", kind="skill", dir=str(tmp_path)))
    assert rc == 0

    root = tmp_path / "demo-skill"
    assert (root / "SKILL.md").is_file()
    assert (root / "pipeline.md").is_file()
    assert (root / "dod-checklist.md").is_file()

    # Design doc lives at tmp_path/docs/<name>-design.md.
    assert (tmp_path / "docs" / "demo-skill-design.md").is_file()

    # SKILL.md frontmatter present.
    skill_md = (root / "SKILL.md").read_text(encoding="utf-8")
    assert skill_md.startswith("---\nname: demo-skill\n")
    assert "user-invocable: true" in skill_md


# ---------------------------------------------------------------------------
# Kind=subpackage
# ---------------------------------------------------------------------------


def test_subpackage_kind_scaffolds_pep621_layout(tmp_path: Path) -> None:
    """kind=subpackage: scaffolds concinno-skills-<name>/ with pyproject + src + tests."""
    rc = _handle(_ns(name="demo", kind="subpackage", dir=str(tmp_path)))
    assert rc == 0

    pkg_root = tmp_path / "concinno-skills-demo"
    src_pkg = pkg_root / "src" / "concinno_skills_demo"
    assert (pkg_root / "pyproject.toml").is_file()
    assert (src_pkg / "__init__.py").is_file()
    assert (pkg_root / "tests" / "__init__.py").is_file()
    assert (pkg_root / "tests" / "test_demo_smoke.py").is_file()
    assert (pkg_root / "README.md").is_file()
    assert (pkg_root / "CHANGELOG.md").is_file()
    assert (pkg_root / "LICENSE").is_file()

    pyproject_text = (pkg_root / "pyproject.toml").read_text(encoding="utf-8")
    assert 'name = "concinno-skills-demo"' in pyproject_text
    assert 'version = "0.1.0"' in pyproject_text


def test_subpackage_scaffold_ships_2_31_entry_points(tmp_path: Path) -> None:
    """2.33.0: scaffold writes features.py + tools.py + skills/ + example SKILL.md,
    and pyproject declares all four entry-points groups with concinno>=2.33.0.
    """
    rc = _handle(_ns(name="demo", kind="subpackage", dir=str(tmp_path)))
    assert rc == 0

    pkg_root = tmp_path / "concinno-skills-demo"
    src_pkg = pkg_root / "src" / "concinno_skills_demo"

    # 2.33.0 new scaffold files
    assert (src_pkg / "features.py").is_file()
    assert (src_pkg / "tools.py").is_file()
    assert (src_pkg / "skills" / "__init__.py").is_file()
    assert (src_pkg / "skills" / "example" / "SKILL.md").is_file()

    # pyproject declares all four entry-points groups
    pyproject_text = (pkg_root / "pyproject.toml").read_text(encoding="utf-8")
    for group in (
        'concinno.tools',
        'concinno.features',
        'concinno.skills',
        'concinno.guards',
    ):
        assert f'[project.entry-points."{group}"]' in pyproject_text, (
            f'pyproject missing entry-points group: {group}'
        )

    # Dependencies pinned to 2.33.0+ so scaffold output works with the
    # entry-points groups declared above.
    assert 'concinno>=2.33.0' in pyproject_text

    # features.py exports an empty-but-valid FEATURE_META dict.
    features_text = (src_pkg / "features.py").read_text(encoding="utf-8")
    assert 'FEATURE_META' in features_text
    assert 'dict[str, dict]' in features_text

    # Example SKILL.md has well-formed frontmatter.
    skill_md = (src_pkg / "skills" / "example" / "SKILL.md").read_text(encoding="utf-8")
    assert skill_md.startswith("---\nname: example\n")
    assert "triggers:" in skill_md

    # Smoke test also extended to check entry-points module imports.
    smoke_text = (pkg_root / "tests" / "test_demo_smoke.py").read_text(encoding="utf-8")
    assert "test_entry_points_modules_load" in smoke_text


# ---------------------------------------------------------------------------
# Radius governance
# ---------------------------------------------------------------------------


def test_chaotic_radius_marks_redteam_phase_mandatory(tmp_path: Path) -> None:
    """radius=chaotic: design doc must mark red-blue phase as **mandatory**."""
    rc = _handle(_ns(name="big-thing", kind="skill", radius="chaotic", dir=str(tmp_path)))
    assert rc == 0

    doc = (tmp_path / "docs" / "big-thing-design.md").read_text(encoding="utf-8")
    # Find the "## Phase N — redteam" header line and verify mandatory suffix.
    redteam_lines = [line for line in doc.splitlines() if "— redteam" in line]
    assert redteam_lines, f"no redteam phase found in doc: {doc[:500]}"
    assert any("**(mandatory)**" in line for line in redteam_lines), (
        f"chaotic radius should mark redteam mandatory, got: {redteam_lines}"
    )


def test_complicated_radius_marks_redteam_optional(tmp_path: Path) -> None:
    """radius=complicated: redteam phase is optional-skip, not mandatory."""
    rc = _handle(
        _ns(name="medium-thing", kind="skill", radius="complicated", dir=str(tmp_path))
    )
    assert rc == 0
    doc = (tmp_path / "docs" / "medium-thing-design.md").read_text(encoding="utf-8")
    redteam_lines = [line for line in doc.splitlines() if "— redteam" in line]
    assert redteam_lines
    assert not any("**(mandatory)**" in line for line in redteam_lines), (
        f"complicated radius should NOT mandate redteam, got: {redteam_lines}"
    )


# ---------------------------------------------------------------------------
# --dry-run
# ---------------------------------------------------------------------------


def test_dry_run_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--dry-run: prints the plan, creates no files."""
    rc = _handle(_ns(name="ghost", kind="skill", dry_run=True, dir=str(tmp_path)))
    assert rc == 0

    out = capsys.readouterr().out
    assert "Scaffold plan" in out
    assert "kind=skill" in out
    assert "ghost" in out

    # No files created anywhere under tmp_path.
    created = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert created == [], f"dry-run must write nothing, got: {created}"


# ---------------------------------------------------------------------------
# Collision handling
# ---------------------------------------------------------------------------


def test_existing_dir_exits_two_with_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Existing target file → exit 2 with clear error message."""
    # Pre-create one of the target files so execute_plan trips.
    (tmp_path / "collide").mkdir()
    (tmp_path / "collide" / "SKILL.md").write_text("pre-existing", encoding="utf-8")

    rc = _handle(_ns(name="collide", kind="skill", dir=str(tmp_path)))
    assert rc == 2

    err = capsys.readouterr().err
    assert "error" in err.lower()
    assert "already exists" in err.lower()

    # Pre-existing content unchanged.
    assert (tmp_path / "collide" / "SKILL.md").read_text(encoding="utf-8") == "pre-existing"


# ---------------------------------------------------------------------------
# Design-doc content
# ---------------------------------------------------------------------------


def test_design_doc_has_all_nine_phases_plus_dod_and_verdict() -> None:
    """Design doc contains all 9 phase headers + 6-point DoD + 5-axis verdict."""
    doc = render_design_doc("foo", kind="skill", radius="complicated")

    # All 9 phases by name.
    expected_phases = (
        "— think",
        "— prd",
        "— rfc",
        "— redteam",
        "— tdd",
        "— impl",
        "— review",
        "— qa",
        "— ship",
    )
    for phase_tag in expected_phases:
        assert phase_tag in doc, f"missing phase tag '{phase_tag}' in design doc"

    # 6-point DoD labels.
    for label in ("Switchable", "ZIQ", "3-layer", "Lazy", "CP/SOTA/logic-max", "CBUA"):
        assert label in doc, f"missing DoD point '{label}'"

    # 5-axis commander verdict labels.
    for axis in ("真做完", "接線", "功能正常", "AI 能力提升", "UX 方便"):
        assert axis in doc, f"missing commander verdict axis '{axis}'"

    # Ecosystem integration phase (10) must be present.
    assert "Ecosystem integration" in doc


def test_build_plan_is_pure_no_writes(tmp_path: Path) -> None:
    """build_plan should compute a plan without touching the filesystem."""
    plan = build_plan("pure", kind="module", radius="simple", target_dir=tmp_path)
    assert plan.name == "pure"
    assert plan.kind == "module"
    assert plan.radius == "simple"
    assert len(plan.files) >= 2  # module stub + test + design doc
    # Nothing written.
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


# ---------------------------------------------------------------------------
# Wiring / argparse
# ---------------------------------------------------------------------------


def test_register_exposes_new_feature_subcommand() -> None:
    """`register(sub)` attaches a `new-feature` subparser."""
    from concinno.cli.new_feature_cmd import register

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    register(sub)

    # Parse a dry-run invocation and verify defaults.
    args = parser.parse_args(
        ["new-feature", "smoke", "--kind", "skill", "--radius", "simple", "--dry-run"]
    )
    assert args.command == "new-feature"
    assert args.name == "smoke"
    assert args.kind == "skill"
    assert args.radius == "simple"
    assert args.dry_run is True
