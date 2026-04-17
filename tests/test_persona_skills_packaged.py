"""Verify the 17 migrated persona/output-format skills are packaged correctly."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

SKILLS_DIR = Path(__file__).parent.parent / "src" / "concinno" / "templates" / "skills"

EXPECTED_PERSONAS = {
    "persona-bizops-analyst",
    "persona-deep-researcher",
    "persona-finance-analyst",
    "persona-game-strategist",
    "persona-multi-agent-judge",
    "persona-openenv-explorer",
    "persona-perfectionist-creator",
    "persona-safety-researcher",
    "persona-security-researcher",
    "persona-software-engineer",
    "persona-tool-precise-agent",
    "persona-web-navigator",
}
EXPECTED_FORMATS = {
    "output-code-block",
    "output-free-strict",
    "output-numeric",
    "output-structured-json",
    "output-tool-call",
}
EXPECTED = EXPECTED_PERSONAS | EXPECTED_FORMATS

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def test_all_17_skill_dirs_exist():
    found = {p.name for p in SKILLS_DIR.iterdir() if p.is_dir()}
    missing = EXPECTED - found
    assert not missing, f"missing skills: {missing}"


def test_each_skill_has_skill_md():
    for slug in EXPECTED:
        f = SKILLS_DIR / slug / "SKILL.md"
        assert f.is_file(), f"missing {f}"


@pytest.mark.parametrize("slug", sorted(EXPECTED))
def test_frontmatter_valid(slug):
    body = (SKILLS_DIR / slug / "SKILL.md").read_text(encoding="utf-8")
    m = FRONTMATTER_RE.match(body)
    assert m, f"{slug}: no frontmatter block"
    fm = m.group(1)
    assert "name:" in fm
    assert "description:" in fm
    assert "triggers:" in fm
    assert "category:" in fm
    assert "source:" in fm


@pytest.mark.parametrize("slug", sorted(EXPECTED))
def test_name_field_matches_dirname(slug):
    body = (SKILLS_DIR / slug / "SKILL.md").read_text(encoding="utf-8")
    m = FRONTMATTER_RE.match(body)
    assert m
    name_match = re.search(r"^name:\s*(\S+)", m.group(1), re.MULTILINE)
    assert name_match, f"{slug}: no name field"
    assert name_match.group(1) == slug, f"{slug}: name field {name_match.group(1)} != dirname"


def test_personas_have_persona_category():
    for slug in EXPECTED_PERSONAS:
        body = (SKILLS_DIR / slug / "SKILL.md").read_text(encoding="utf-8")
        assert "category: persona" in body


def test_formats_have_output_format_category():
    for slug in EXPECTED_FORMATS:
        body = (SKILLS_DIR / slug / "SKILL.md").read_text(encoding="utf-8")
        assert "category: output-format" in body
