"""Tests for concinno.template_router — cold-start handoff template router."""

from __future__ import annotations

import pytest

from concinno.handoff_templates import SPECIALIZED_TEMPLATE_NAMES
from concinno.template_router import (
    DEFAULT_ALIASES,
    classify,
    current_aliases,
    register_alias,
    reset_aliases,
)


@pytest.fixture(autouse=True)
def _reset_aliases_each_test():
    """Each test starts with the shipped alias table."""
    reset_aliases()
    yield
    reset_aliases()


# ── Per-template-type matching ────────────────────────────


def test_benchmark_keyword_matches_benchmark():
    assert classify("gaia-bench-runner") == ["benchmark"]


def test_release_keyword_matches_release():
    assert classify("concinno") == ["release"]


def test_research_keyword_matches_research():
    assert classify("ablation-experiments-2026") == ["research"]


def test_build_keyword_matches_build():
    assert classify("ai-king-vscode-build") == ["build"]


# ── Multi-template ─────────────────────────────────────────


def test_release_and_build_both_match():
    # "concinno" hits release alias, "build" hits build alias
    out = classify("concinno-build-pipeline")
    # Order follows SPECIALIZED_TEMPLATE_NAMES (release, then build).
    assert out == ["release", "build"]


def test_benchmark_and_research_both_match():
    out = classify("gaia-ablation-2026")
    assert out == ["benchmark", "research"]


# ── Empty / fallback ──────────────────────────────────────


def test_empty_project_name_returns_empty():
    assert classify("") == []
    assert classify("   ") == []


def test_unknown_project_name_returns_empty():
    assert classify("zzz-totally-unrelated-name-xyz") == []


# ── Meta-driven matching ──────────────────────────────────


def test_meta_recent_commits_pulls_in_template():
    # Project name is ambiguous; commits give the hint.
    out = classify(
        "my-monorepo",
        meta={"recent_commits": ["feat(release): v2.21.0"]},
    )
    assert out == ["release"]


def test_meta_recent_paths_pulls_in_template():
    out = classify(
        "my-mono",
        meta={"recent_paths": ["src/benchmark/runner.py"]},
    )
    assert out == ["benchmark"]


def test_meta_unknown_keys_ignored():
    out = classify(
        "concinno",
        meta={"completely_unknown_key": "whatever"},
    )
    assert out == ["release"]


# ── Substring vs exact ────────────────────────────────────


def test_partial_substring_match_works():
    # "bench" is a substring of "subbenchmark"
    assert classify("my-subbenchmark-tool") == ["benchmark"]


def test_case_insensitive_match():
    assert classify("MY-CONCINNO-PROJECT") == ["release"]


# ── register_alias / reset_aliases ────────────────────────


def test_register_alias_adds_keyword():
    register_alias("benchmark", "myscoreboard")
    assert classify("myscoreboard-2026") == ["benchmark"]


def test_register_alias_unknown_template_raises():
    with pytest.raises(ValueError):
        register_alias("nonexistent_template", "foo")


def test_register_alias_dedup():
    register_alias("benchmark", "bench")  # already there
    aliases = current_aliases()["benchmark"]
    # No duplicate inserted
    assert sum(1 for a in aliases if a.lower() == "bench") == 1


def test_reset_aliases_restores_defaults():
    register_alias("benchmark", "totally_new_keyword")
    reset_aliases()
    assert "totally_new_keyword" not in current_aliases()["benchmark"]


# ── Schema sanity ─────────────────────────────────────────


def test_default_aliases_keys_match_specialized():
    assert set(DEFAULT_ALIASES.keys()) == set(SPECIALIZED_TEMPLATE_NAMES)


def test_classify_only_returns_known_templates():
    out = classify("gaia-concinno-experiments-build")
    for name in out:
        assert name in SPECIALIZED_TEMPLATE_NAMES
