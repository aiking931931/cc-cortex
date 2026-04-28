"""Unit tests for ``concinno.skills.disclosure.SkillDisclosure``.

Each test builds a synthetic skills tree under ``tmp_path`` so the suite
stays hermetic — no dependency on the operator's real
``~/.claude/skills`` directory.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from concinno.skills.disclosure import (
    DEFAULT_FTRL_DECAY,
    RouteResult,
    SkillDisclosure,
    SkillL1,
    default_skills_dir,
    default_sps_scorer,
)


def _write_skill(
    root: Path,
    name: str,
    *,
    description: str = "",
    extra_files: dict[str, str] | None = None,
    body: str = "",
) -> Path:
    """Materialise a fake ``<name>/SKILL.md`` skill under ``root``."""
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    fm = "---\n"
    fm += f"name: {name}\n"
    if description:
        fm += f'description: "{description}"\n'
    fm += "---\n"
    text = fm + (body or f"# {name}\n\nBody.\n")
    (skill_dir / "SKILL.md").write_text(text, encoding="utf-8")
    for rel, content in (extra_files or {}).items():
        target = skill_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return skill_dir


@pytest.fixture()
def skills_root(tmp_path: Path) -> Path:
    """Three synthetic skills covering distinct trigger vocabularies."""
    _write_skill(
        tmp_path,
        "browser",
        description=(
            "In-process Playwright browser automation. "
            'Triggers on "browser", "Playwright", "headless".'
        ),
    )
    _write_skill(
        tmp_path,
        "kb_runpod",
        description=(
            "RunPod pod lifecycle and SSH cheatsheet. "
            'Triggers on "RunPod", "pod", "GPU".'
        ),
    )
    _write_skill(
        tmp_path,
        "qa",
        description=(
            "QA testing, visual verification, bug hunt. "
            'Triggers on "qa", "test", "verify".'
        ),
        extra_files={"reference.md": "# Detailed reference\n" + ("x\n" * 5)},
    )
    return tmp_path


def _make(skills_root: Path, **overrides: object) -> SkillDisclosure:
    return SkillDisclosure(skills_dir=skills_root, **overrides)  # type: ignore[arg-type]


# ── L1 ────────────────────────────────────────────────────


def test_list_l1_returns_all_skills(skills_root: Path) -> None:
    sd = _make(skills_root)
    names = {entry.name for entry in sd.list_l1()}
    assert names == {"browser", "kb_runpod", "qa"}


def test_list_l1_caches_until_ttl(skills_root: Path) -> None:
    clock = [1000.0]
    sd = _make(skills_root, l1_cache_ttl_sec=300, clock=lambda: clock[0])
    first = sd.list_l1()
    # Add a new skill on disk; with TTL active we should not see it.
    _write_skill(skills_root, "newer", description="Fresh skill.")
    cached = sd.list_l1()
    assert {entry.name for entry in cached} == {entry.name for entry in first}
    # Advance the clock past TTL → cache invalidates.
    clock[0] += 600
    refreshed = sd.list_l1()
    assert "newer" in {entry.name for entry in refreshed}


def test_refresh_l1_bypasses_ttl(skills_root: Path) -> None:
    sd = _make(skills_root, l1_cache_ttl_sec=999_999)
    sd.list_l1()
    _write_skill(skills_root, "freshly_added", description="Force-refresh.")
    refreshed = sd.refresh_l1()
    assert "freshly_added" in {entry.name for entry in refreshed}


def test_no_skills_dir_graceful(tmp_path: Path) -> None:
    """Pointing at a non-existent dir yields an empty index, not a crash."""
    sd = SkillDisclosure(skills_dir=tmp_path / "does-not-exist")
    assert sd.list_l1() == []
    assert sd.route("anything") == []


def test_corrupted_skill_md_skipped(skills_root: Path) -> None:
    """A SKILL.md without parseable frontmatter is still indexed by name."""
    bad_dir = skills_root / "broken"
    bad_dir.mkdir()
    # No frontmatter at all — body-only Markdown.
    (bad_dir / "SKILL.md").write_text("# broken\n\nNo frontmatter.\n", encoding="utf-8")
    sd = _make(skills_root)
    names = {entry.name for entry in sd.list_l1()}
    assert "broken" in names


def test_skill_dir_without_skill_md_skipped(skills_root: Path) -> None:
    (skills_root / "noskillmd").mkdir()
    sd = _make(skills_root)
    names = {entry.name for entry in sd.list_l1()}
    assert "noskillmd" not in names


# ── routing ───────────────────────────────────────────────


def test_route_query_returns_top_k(skills_root: Path) -> None:
    sd = _make(skills_root, top_k=2, min_route_score=0.0)
    out = sd.route("I need a browser playwright session")
    assert out, "expected non-empty routing result"
    assert out[0].name == "browser"
    assert len(out) <= 2


def test_route_below_min_score_filtered(skills_root: Path) -> None:
    sd = _make(skills_root, min_route_score=0.99)
    # No realistic prompt scores 0.99 against three short descriptions.
    assert sd.route("totally unrelated content xyz qwerty") == []


def test_route_empty_query_returns_empty(skills_root: Path) -> None:
    sd = _make(skills_root)
    assert sd.route("") == []
    assert sd.route("   ") == []


def test_routing_score_ordering(skills_root: Path) -> None:
    sd = _make(skills_root, min_route_score=0.0)
    out = sd.route("RunPod pod GPU")
    # kb_runpod must rank ahead of browser / qa for this query.
    assert out[0].name == "kb_runpod"
    # Scores must be monotonic non-increasing.
    for prev, curr in zip(out, out[1:]):
        assert prev.score >= curr.score


def test_top_k_capped_to_skills_count(skills_root: Path) -> None:
    sd = _make(skills_root, min_route_score=0.0)
    out = sd.route("browser playwright RunPod GPU qa test", top_k=999)
    # Only 3 fixture skills exist — cannot exceed that.
    assert len(out) <= 3


def test_sps_score_zero_for_unrelated(skills_root: Path) -> None:
    l1 = SkillL1(name="browser", description="Playwright browser automation.")
    score = default_sps_scorer("buy groceries milk eggs", l1)
    assert 0.0 <= score < 0.2


def test_route_uses_custom_sps_scorer(skills_root: Path) -> None:
    """A swap-in scorer overrides the default cosine."""

    def constant_high(_query: str, _l1: SkillL1) -> float:
        return 0.9

    sd = _make(skills_root, sps_scorer=constant_high, min_route_score=0.0)
    out = sd.route("anything")
    # Every L1 entry beats min_route_score under the constant scorer.
    assert {r.name for r in out} == {"browser", "kb_runpod", "qa"}


# ── L2 / L3 ───────────────────────────────────────────────


def test_load_l2_returns_summary(skills_root: Path) -> None:
    sd = _make(skills_root)
    body = sd.load_l2("browser")
    assert body, "L2 should return non-empty summary"
    # Frontmatter must be stripped.
    assert "---" not in body.splitlines()[0]


def test_load_l2_unknown_skill_returns_empty(skills_root: Path) -> None:
    sd = _make(skills_root)
    assert sd.load_l2("does-not-exist") == ""


def test_load_l3_returns_full_bundle(skills_root: Path) -> None:
    sd = _make(skills_root)
    bundle = sd.load_l3("qa")
    # Must include both SKILL.md and the sibling reference.
    assert "SKILL.md" in bundle
    assert "reference.md" in bundle


def test_load_l3_unknown_skill_returns_empty(skills_root: Path) -> None:
    sd = _make(skills_root)
    assert sd.load_l3("nope") == {}


# ── FTRL ──────────────────────────────────────────────────


def test_observe_use_updates_ftrl(skills_root: Path) -> None:
    sd = _make(skills_root, min_route_score=0.0, ftrl_alpha=0.5)
    # Hostile signal — drive FTRL low for kb_runpod.
    for _ in range(20):
        sd.observe_use("kb_runpod", helpful=False)
    weight = sd._ftrl_weight("kb_runpod")
    assert weight < 0.2, f"expected weight to drop, got {weight}"


def test_ftrl_weight_decay_keeps_unobserved_skills_at_one(
    skills_root: Path,
) -> None:
    sd = _make(skills_root)
    assert sd._ftrl_weight("browser") == pytest.approx(1.0)
    # Decay only kicks in when observe_use is called; latent weights stay 1.
    assert DEFAULT_FTRL_DECAY < 1.0


def test_concurrent_route_thread_safe(skills_root: Path) -> None:
    sd = _make(skills_root, min_route_score=0.0)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for _ in range(50):
                sd.route("browser playwright RunPod qa")
                sd.observe_use("browser", helpful=True)
        except BaseException as exc:  # noqa: BLE001 — capture for assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors


def test_observe_use_empty_name_noop(skills_root: Path) -> None:
    sd = _make(skills_root)
    sd.observe_use("", helpful=True)
    # Empty name must not poison the FTRL table.
    assert "" not in sd._ftrl


# ── feature config integration ────────────────────────────


def test_disable_via_feature_config() -> None:
    """The feature_config entry exists and defaults to False per 4.0.0 opt-in."""
    from concinno.feature_config import FEATURE_META

    assert "skill_disclosure" in FEATURE_META, (
        "skill_disclosure must be registered in FEATURE_META"
    )
    entry = FEATURE_META["skill_disclosure"]
    assert entry["params"]["enabled"]["default"] is False
    assert entry["category"] == "behavioral"
    assert entry["ziq_autotunable"] is True
    assert entry["cosmetic"] is False


def test_default_skills_dir_respects_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONCINNO_SKILLS_DIR", "/tmp/concinno_test_skills")
    assert default_skills_dir() == Path("/tmp/concinno_test_skills")


def test_default_skills_dir_defaults_to_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CONCINNO_SKILLS_DIR", raising=False)
    expected = Path.home() / ".claude" / "skills"
    assert default_skills_dir() == expected


def test_route_result_dataclass_shape() -> None:
    r = RouteResult(name="x", score=0.5, sps=0.5, ftrl=1.0)
    assert r.name == "x"
    assert r.score == 0.5
    assert r.sps == 0.5
    assert r.ftrl == 1.0
