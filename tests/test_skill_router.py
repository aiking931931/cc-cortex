"""Tests for cc_cortex.skill_router — Cognitive Skill Router."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cc_cortex.skill_router import SkillRouter, _parse_skill_md

# ── Helpers ───────────────────────────────────────────────────


def _write_skill(base: Path, name: str, content: str) -> Path:
    """Write a skill .md file (builtin style: flat file)."""
    base.mkdir(parents=True, exist_ok=True)
    p = base / f"{name}.md"
    p.write_text(content, encoding="utf-8")
    return p


def _write_user_skill(base: Path, name: str, content: str) -> Path:
    """Write a user skill (directory style: <name>/SKILL.md)."""
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    p = d / "SKILL.md"
    p.write_text(content, encoding="utf-8")
    return p


SAMPLE_SKILL = """\
---
name: test_skill
description: A test skill for unit testing.
---

# Test Skill

This is test content.
"""

SAMPLE_OVERRIDE = """\
---
name: test_skill
description: User override of test skill.
---

# My Custom Test Skill

My custom content replaces the builtin.
"""


# ── Discovery ─────────────────────────────────────────────────


class TestDiscovery:
    def test_discover_builtins(self, tmp_path):
        """Discovers builtin skills from flat .md files."""
        _write_skill(tmp_path / "builtins", "alpha", SAMPLE_SKILL)
        _write_skill(tmp_path / "builtins", "beta", SAMPLE_SKILL)
        router = SkillRouter(builtin_dir=tmp_path / "builtins")
        skills = router.discover()
        assert len(skills) == 2
        assert "alpha" in skills
        assert "beta" in skills
        assert skills["alpha"].is_builtin is True

    def test_discover_user_skills(self, tmp_path):
        """Discovers user skills from <name>/SKILL.md directories."""
        _write_user_skill(tmp_path / "user", "my_skill", SAMPLE_SKILL)
        router = SkillRouter(
            user_skills_dir=tmp_path / "user",
            builtin_dir=tmp_path / "empty_builtins",
        )
        skills = router.discover()
        assert len(skills) == 1
        assert "my_skill" in skills
        assert skills["my_skill"].is_builtin is False

    def test_user_overrides_builtin(self, tmp_path):
        """User skill with same name overrides builtin."""
        _write_skill(tmp_path / "builtins", "shared", SAMPLE_SKILL)
        _write_user_skill(tmp_path / "user", "shared", SAMPLE_OVERRIDE)
        router = SkillRouter(
            user_skills_dir=tmp_path / "user",
            builtin_dir=tmp_path / "builtins",
        )
        skills = router.discover()
        assert len(skills) == 1
        assert skills["shared"].is_builtin is False
        assert "User override" in skills["shared"].description

    def test_mixed_discovery(self, tmp_path):
        """Builtins + user skills coexist; override applies only to matching names."""
        _write_skill(tmp_path / "builtins", "builtin_only", SAMPLE_SKILL)
        _write_skill(tmp_path / "builtins", "both", SAMPLE_SKILL)
        _write_user_skill(tmp_path / "user", "both", SAMPLE_OVERRIDE)
        _write_user_skill(tmp_path / "user", "user_only", SAMPLE_SKILL)
        router = SkillRouter(
            user_skills_dir=tmp_path / "user",
            builtin_dir=tmp_path / "builtins",
        )
        skills = router.discover()
        assert len(skills) == 3
        assert skills["builtin_only"].is_builtin is True
        assert skills["both"].is_builtin is False  # user wins
        assert skills["user_only"].is_builtin is False

    def test_empty_directories(self, tmp_path):
        """No skills found returns empty dict."""
        router = SkillRouter(
            user_skills_dir=tmp_path / "nope",
            builtin_dir=tmp_path / "also_nope",
        )
        skills = router.discover()
        assert skills == {}

    def test_discover_real_builtins(self):
        """Package ships with 9 builtin cognitive skills."""
        router = SkillRouter()
        skills = router.discover()
        assert len(skills) == 9
        expected = {
            "three_layer", "first_principles", "prompt_select",
            "debug_loop", "decision_journal", "pdca",
            "judgment", "awareness", "learning_loop",
        }
        assert set(skills.keys()) == expected
        for s in skills.values():
            assert s.is_builtin is True
            assert s.description  # not empty
            assert s.content  # not empty


# ── Get / List ────────────────────────────────────────────────


class TestGetAndList:
    def test_get_existing(self, tmp_path):
        _write_skill(tmp_path / "b", "alpha", SAMPLE_SKILL)
        router = SkillRouter(builtin_dir=tmp_path / "b")
        skill = router.get("alpha")
        assert skill is not None
        assert skill.name == "alpha"

    def test_get_nonexistent(self, tmp_path):
        router = SkillRouter(builtin_dir=tmp_path / "b")
        assert router.get("nonexistent") is None

    def test_list_names(self, tmp_path):
        _write_skill(tmp_path / "b", "a", SAMPLE_SKILL)
        _write_skill(tmp_path / "b", "b", SAMPLE_SKILL)
        router = SkillRouter(builtin_dir=tmp_path / "b")
        names = router.list_names()
        assert sorted(names) == ["a", "b"]

    def test_list_builtins(self, tmp_path):
        _write_skill(tmp_path / "b", "builtin1", SAMPLE_SKILL)
        _write_user_skill(tmp_path / "u", "user1", SAMPLE_SKILL)
        router = SkillRouter(user_skills_dir=tmp_path / "u", builtin_dir=tmp_path / "b")
        builtins = router.list_builtins()
        assert builtins == ["builtin1"]

    def test_list_overridden(self, tmp_path):
        _write_skill(tmp_path / "b", "shared", SAMPLE_SKILL)
        _write_skill(tmp_path / "b", "only_builtin", SAMPLE_SKILL)
        _write_user_skill(tmp_path / "u", "shared", SAMPLE_OVERRIDE)
        router = SkillRouter(user_skills_dir=tmp_path / "u", builtin_dir=tmp_path / "b")
        overridden = router.list_overridden()
        assert overridden == ["shared"]


# ── Classification ────────────────────────────────────────────


class TestClassification:
    @pytest.fixture()
    def router(self):
        return SkillRouter()  # uses real builtins

    def test_stuck_suggests_debug(self, router):
        results = router.classify("I'm stuck on a bug")
        assert "debug_loop" in results

    def test_decision_suggests_three_layer(self, router):
        results = router.classify("Need to decide between option A and B")
        assert "three_layer" in results

    def test_why_suggests_first_principles(self, router):
        results = router.classify("Why do we need this? Let's rethink from scratch")
        assert "first_principles" in results

    def test_prompt_suggests_prompt_select(self, router):
        results = router.classify("What thinking mode should I use? CoT or ToT?")
        assert "prompt_select" in results

    def test_iterate_suggests_pdca(self, router):
        results = router.classify("Let's plan the iteration cycle and improve")
        assert "pdca" in results

    def test_judgment_suggests_judgment(self, router):
        results = router.classify("I'm not confident about this, should I proceed?")
        assert "judgment" in results

    def test_attention_suggests_awareness(self, router):
        results = router.classify("I keep looping and repeating the same thing")
        assert "awareness" in results

    def test_learning_suggests_learning_loop(self, router):
        results = router.classify("I was corrected, need to learn from this pattern")
        assert "learning_loop" in results

    def test_no_match_returns_empty(self, router):
        results = router.classify("hello world")
        assert results == []

    def test_top_k_limits(self, router):
        results = router.classify("decide analyze evaluate option trade-off", top_k=1)
        assert len(results) <= 1

    def test_multiple_matches_ranked(self, router):
        # "stuck bug error broken" hits debug_loop with 3 patterns
        results = router.classify("I'm stuck on a bug, there's an error and it's broken")
        assert results[0] == "debug_loop"


# ── Outcome Tracking ─────────────────────────────────────────


class TestOutcomeTracking:
    def test_record_and_stats(self, tmp_path):
        router = SkillRouter(
            builtin_dir=tmp_path / "b",
            tracking_path=tmp_path / "track.json",
        )
        router.record_outcome("debug_loop", success=True)
        router.record_outcome("debug_loop", success=True)
        router.record_outcome("debug_loop", success=False)
        stats = router.get_stats()
        assert stats["debug_loop"]["uses"] == 3
        assert stats["debug_loop"]["successes"] == 2
        assert stats["debug_loop"]["failures"] == 1
        assert stats["debug_loop"]["success_rate"] == 0.67

    def test_persistence(self, tmp_path):
        track_path = tmp_path / "track.json"
        # Write
        r1 = SkillRouter(builtin_dir=tmp_path / "b", tracking_path=track_path)
        r1.record_outcome("three_layer", success=True)
        r1.record_outcome("three_layer", success=True)
        # Read back
        r2 = SkillRouter(builtin_dir=tmp_path / "b", tracking_path=track_path)
        stats = r2.get_stats()
        assert stats["three_layer"]["uses"] == 2

    def test_corrupted_tracking_file(self, tmp_path):
        track_path = tmp_path / "track.json"
        track_path.write_text("not json", encoding="utf-8")
        router = SkillRouter(builtin_dir=tmp_path / "b", tracking_path=track_path)
        # Should not crash
        assert router.get_stats() == {}

    def test_empty_stats(self, tmp_path):
        router = SkillRouter(builtin_dir=tmp_path / "b")
        assert router.get_stats() == {}


# ── Parse helpers ─────────────────────────────────────────────


class TestParseSkillMd:
    def test_with_frontmatter(self, tmp_path):
        p = tmp_path / "test.md"
        p.write_text(SAMPLE_SKILL, encoding="utf-8")
        desc, content, aa = _parse_skill_md(p)
        assert "test skill" in desc.lower()
        assert "# Test Skill" in content
        assert aa is None

    def test_without_frontmatter(self, tmp_path):
        p = tmp_path / "plain.md"
        p.write_text("# Just Content\n\nNo frontmatter here.", encoding="utf-8")
        desc, content, aa = _parse_skill_md(p)
        assert desc == ""
        assert "# Just Content" in content
        assert aa is None

    def test_nonexistent_file(self, tmp_path):
        desc, content, aa = _parse_skill_md(tmp_path / "nope.md")
        assert desc == ""
        assert content == ""
        assert aa is None

    def test_auto_apply_parsing(self, tmp_path):
        p = tmp_path / "auto.md"
        p.write_text(
            '---\ndescription: Auto skill\n'
            'auto_apply: ["src/**/*.py", "tests/*.py"]\n'
            '---\n# Auto\n',
            encoding="utf-8",
        )
        desc, content, aa = _parse_skill_md(p)
        assert desc == "Auto skill"
        assert aa == ["src/**/*.py", "tests/*.py"]


# ── Path matching (auto_apply) ───────────────────────────────

SAMPLE_AUTO_APPLY = """\
---
name: guard_skill
description: Guard skill with auto_apply.
auto_apply: ["src/**/*.py", "tests/*.py"]
---

# Guard Skill
"""


class TestMatchPath:
    def test_match_glob(self, tmp_path):
        _write_skill(tmp_path / "b", "guard", SAMPLE_AUTO_APPLY)
        router = SkillRouter(builtin_dir=tmp_path / "b")
        assert "guard" in router.match_path("src/foo/bar.py")
        assert "guard" in router.match_path("tests/test_x.py")

    def test_no_match(self, tmp_path):
        _write_skill(tmp_path / "b", "guard", SAMPLE_AUTO_APPLY)
        router = SkillRouter(builtin_dir=tmp_path / "b")
        assert router.match_path("docs/readme.md") == []

    def test_skill_without_auto_apply(self, tmp_path):
        _write_skill(tmp_path / "b", "plain", SAMPLE_SKILL)
        router = SkillRouter(builtin_dir=tmp_path / "b")
        assert router.match_path("anything.py") == []

    def test_user_skill_auto_apply(self, tmp_path):
        _write_user_skill(
            tmp_path / "u", "my_guard", SAMPLE_AUTO_APPLY
        )
        router = SkillRouter(
            user_skills_dir=tmp_path / "u",
            builtin_dir=tmp_path / "empty",
        )
        assert "my_guard" in router.match_path("src/core/x.py")


# ── Integration: end-to-end ───────────────────────────────────


class TestEndToEnd:
    def test_full_workflow(self, tmp_path):
        """Complete workflow: discover → classify → get → record."""
        # Setup: builtin + user override
        _write_skill(tmp_path / "b", "debug_loop", """\
---
name: debug_loop
description: Basic debug loop.
---
# Debug
Observe → Hypothesize → Test
""")
        _write_user_skill(tmp_path / "u", "debug_loop", """\
---
name: debug_loop
description: My enhanced debug loop with domain knowledge.
---
# My Debug Loop
Step 1: Check the logs first (always).
Step 2: Hypothesize based on recent changes.
Step 3: Binary search the problem space.
""")
        track = tmp_path / "track.json"
        router = SkillRouter(
            user_skills_dir=tmp_path / "u",
            builtin_dir=tmp_path / "b",
            tracking_path=track,
        )

        # Discover
        skills = router.discover()
        assert "debug_loop" in skills
        assert skills["debug_loop"].is_builtin is False  # user wins

        # Classify
        suggestions = router.classify("I'm stuck on a bug")
        assert "debug_loop" in suggestions

        # Get content
        skill = router.get("debug_loop")
        assert "Check the logs" in skill.content  # user version

        # Track
        router.record_outcome("debug_loop", success=True)
        stats = router.get_stats()
        assert stats["debug_loop"]["success_rate"] == 1.0

        # Persistence
        assert track.exists()
        data = json.loads(track.read_text(encoding="utf-8"))
        assert data["debug_loop"]["uses"] == 1
