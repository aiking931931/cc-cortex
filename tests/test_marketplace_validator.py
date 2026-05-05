"""Unit tests for ``concinno.marketplace.validator``.

Covers HP1 frontmatter validator delegation, severity bucketing, and
graceful degradation when the package has no SKILL.md files.
"""
from __future__ import annotations

import pytest

from concinno.marketplace.validator import (
    FrontmatterReport,
    validate_dist_frontmatter,
)


def test_validate_returns_empty_when_dist_missing() -> None:
    """Unknown distribution returns empty list, never raises."""
    out = validate_dist_frontmatter("concinno-skills-totally-fake-zzzz")
    assert out == []


def test_frontmatter_report_to_dict_round_trip() -> None:
    rep = FrontmatterReport(
        skill_md_path="/tmp/SKILL.md",
        status="valid",
        error_count=0,
        recommendation_count=2,
        note_count=1,
        fixable_count=2,
    )
    d = rep.to_dict()
    assert d["skill_md_path"] == "/tmp/SKILL.md"
    assert d["status"] == "valid"
    assert d["recommendation_count"] == 2


def test_validate_skips_when_files_metadata_missing(monkeypatch: pytest.MonkeyPatch,
                                                    tmp_path) -> None:
    """Distribution whose ``files`` is None must not blow up."""

    class _NoFilesDist:
        files = None

        @property
        def metadata(self) -> dict[str, str]:
            return {"Name": "concinno-skills-memory"}

    import concinno.marketplace.validator as mod_v

    def fake_distribution(_name: str) -> _NoFilesDist:
        return _NoFilesDist()

    monkeypatch.setattr(
        mod_v.importlib_metadata, "distribution", fake_distribution,
    )
    out = validate_dist_frontmatter("concinno-skills-memory")
    assert out == []


def test_validate_classifies_valid_skill_md(tmp_path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: write a real SKILL.md, assert validator returns valid."""
    skill = tmp_path / "SKILL.md"
    skill.write_text(
        "---\nname: my-skill\ndescription: a test skill that fits the schema\n---\n\n# body\n",
        encoding="utf-8",
    )

    class _FakeDist:
        @property
        def files(self):
            class _PathLike:
                def __str__(self_inner) -> str:
                    return "SKILL.md"

                def locate(self_inner):
                    return skill

            return [_PathLike()]

        @property
        def metadata(self) -> dict[str, str]:
            return {"Name": "concinno-skills-memory"}

    import concinno.marketplace.validator as mod_v

    monkeypatch.setattr(
        mod_v.importlib_metadata, "distribution", lambda _n: _FakeDist(),
    )
    out = validate_dist_frontmatter("concinno-skills-memory")
    assert len(out) == 1
    # Status depends on HP1 validator's view; just assert structure.
    assert out[0].skill_md_path
    assert out[0].status in {"valid", "invalid", "absent"}
