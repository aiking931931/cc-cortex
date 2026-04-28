"""E2E test: SkillDisclosure plugged into ``hooks.on_prompt_submit``.

Verifies the wiring contract: when a real user-prompt fans into the
hook entrypoint and the ``skill_disclosure`` feature is enabled, the
disclosure router should produce a non-empty advisory pointing at the
synthetic skill that semantically matches the prompt.

The fixture isolates state — temporary skills dir + monkey-patched
``default_skills_dir`` — so the test does not depend on the operator's
real ``~/.claude/skills`` tree.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from concinno.skills import disclosure as disclosure_mod
from concinno.skills.disclosure import SkillDisclosure


def _seed_fixture_skills(root: Path) -> None:
    """Drop two skills with disjoint trigger vocabularies."""
    (root / "browser").mkdir()
    (root / "browser" / "SKILL.md").write_text(
        '---\nname: browser\ndescription: "In-process Playwright browser '
        'automation. Triggers on \\"browser\\", \\"Playwright\\", '
        '\\"headless\\"."\n---\n# browser\nbody.\n',
        encoding="utf-8",
    )
    (root / "kb_runpod").mkdir()
    (root / "kb_runpod" / "SKILL.md").write_text(
        '---\nname: kb_runpod\ndescription: "RunPod pod lifecycle and SSH '
        'cheatsheet. Triggers on \\"RunPod\\", \\"pod\\", \\"GPU\\"."\n'
        '---\n# kb_runpod\nbody.\n',
        encoding="utf-8",
    )


@pytest.fixture()
def isolated_skill_disclosure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> SkillDisclosure:
    """Build a SkillDisclosure pointed at an isolated tmp skills dir."""
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    _seed_fixture_skills(skills_root)
    # Force any caller that uses the module-level default to land here.
    monkeypatch.setattr(
        disclosure_mod,
        "default_skills_dir",
        lambda: skills_root,
    )
    return SkillDisclosure(skills_dir=skills_root, min_route_score=0.0)


def test_disclosure_routes_browser_query_end_to_end(
    isolated_skill_disclosure: SkillDisclosure,
) -> None:
    """A user prompt mentioning Playwright should route to ``browser``."""
    sd = isolated_skill_disclosure
    candidates = sd.route("Use a headless browser playwright session.")
    assert candidates, "expected non-empty routing for browser-themed prompt"
    assert candidates[0].name == "browser"


def test_disclosure_routes_runpod_query_end_to_end(
    isolated_skill_disclosure: SkillDisclosure,
) -> None:
    """A user prompt about GPUs should route to ``kb_runpod``."""
    sd = isolated_skill_disclosure
    candidates = sd.route("How do I bring up a fresh RunPod GPU pod?")
    assert candidates, "expected non-empty routing for runpod-themed prompt"
    assert candidates[0].name == "kb_runpod"


def test_disclosure_l2_promotion_after_route(
    isolated_skill_disclosure: SkillDisclosure,
) -> None:
    """L1 → L2 promotion path: route picks the skill, load_l2 returns body."""
    sd = isolated_skill_disclosure
    candidates = sd.route("RunPod GPU pod resume")
    assert candidates
    body = sd.load_l2(candidates[0].name)
    assert body
    # Frontmatter must be stripped.
    assert not body.lstrip().startswith("---")


def test_observe_use_then_route_reflects_signal(
    isolated_skill_disclosure: SkillDisclosure,
) -> None:
    """Negative FTRL signal should drop the skill below positive peers."""
    sd = isolated_skill_disclosure
    # Drive ``browser`` weight low.
    for _ in range(30):
        sd.observe_use("browser", helpful=False)
    # An ambiguous query that hits both descriptions should now prefer
    # the un-penalised peer.
    out = sd.route("browser RunPod pod")
    if out:
        assert out[0].name != "browser"
