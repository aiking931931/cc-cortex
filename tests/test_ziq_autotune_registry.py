"""Tests for concinno.ziq_autotune_registry — registry contract + factory."""

from __future__ import annotations

import pytest

from concinno.ziq_autotune_registry import (
    TUNABLE_REGISTRY,
    TunableSpec,
    clear_cache,
    describe,
    get_tuner,
    list_targets,
)


def test_registry_has_at_least_10_targets():
    """Plan Part 10 Session E directive: >= 10 hardcoded targets inventoried."""
    assert len(TUNABLE_REGISTRY) >= 10


def test_registry_keys_match_target_field():
    for key, spec in TUNABLE_REGISTRY.items():
        assert spec.target == key, (
            f"registry key '{key}' does not match spec.target '{spec.target}'"
        )


def test_list_targets_returns_sorted():
    targets = list_targets()
    assert targets == sorted(targets)
    assert len(targets) == len(TUNABLE_REGISTRY)


def test_describe_unknown_raises():
    with pytest.raises(KeyError):
        describe("bogus.target.name")


def test_describe_returns_spec():
    spec = describe("escalation.max_retries_per_tier")
    assert isinstance(spec, TunableSpec)
    assert spec.preset == 1
    assert spec.kind == "continuous"


def test_get_tuner_memoizes(monkeypatch, tmp_path):
    monkeypatch.setenv("CONCINNO_ZIQ_TUNER_DIR", str(tmp_path))
    clear_cache()
    t1 = get_tuner("escalation.circuit_threshold", auto_persist=False)
    t2 = get_tuner("escalation.circuit_threshold", auto_persist=False)
    assert t1 is t2


def test_get_tuner_fresh_bypasses_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("CONCINNO_ZIQ_TUNER_DIR", str(tmp_path))
    clear_cache()
    t1 = get_tuner("escalation.circuit_threshold", auto_persist=False)
    t2 = get_tuner(
        "escalation.circuit_threshold", fresh=True, auto_persist=False,
    )
    assert t1 is not t2


def test_get_tuner_discrete_preserves_choices(monkeypatch, tmp_path):
    monkeypatch.setenv("CONCINNO_ZIQ_TUNER_DIR", str(tmp_path))
    clear_cache()
    t = get_tuner("gaia.meta_arm", fresh=True, auto_persist=False)
    assert t.kind == "discrete"
    assert t._choices_list == ["SAS", "MAS", "hybrid"]


def test_get_tuner_continuous_applies_bounds(monkeypatch, tmp_path):
    monkeypatch.setenv("CONCINNO_ZIQ_TUNER_DIR", str(tmp_path))
    clear_cache()
    t = get_tuner("escalation.max_retries_per_tier", fresh=True, auto_persist=False)
    assert t.kind == "continuous"
    assert t.vmin == 0.0
    assert t.vmax == 5.0


def test_get_tuner_boolean_spec(monkeypatch, tmp_path):
    monkeypatch.setenv("CONCINNO_ZIQ_TUNER_DIR", str(tmp_path))
    clear_cache()
    t = get_tuner("escalation.enable_few_shot", fresh=True, auto_persist=False)
    assert t.kind == "boolean"


def test_default_thresholds_match_user_directive(monkeypatch, tmp_path):
    """User directive 2026-04-21: tune_threshold 300 / full_threshold 500."""
    monkeypatch.setenv("CONCINNO_ZIQ_TUNER_DIR", str(tmp_path))
    clear_cache()
    t = get_tuner("field_read.handoff_budget", fresh=True, auto_persist=False)
    assert t.tunable_threshold == 300
    assert t.full_threshold == 500


def test_registry_has_mixed_kinds():
    """Sanity: registry covers continuous + discrete + boolean."""
    kinds = {spec.kind or "inferred" for spec in TUNABLE_REGISTRY.values()}
    # At least one of each explicit kind
    explicit = {spec.kind for spec in TUNABLE_REGISTRY.values() if spec.kind}
    assert "continuous" in explicit
    assert "discrete" in explicit
    assert "boolean" in explicit
    _ = kinds  # appeased unused-var lint


def test_registry_presets_match_expected_types():
    """Each preset must match its declared kind — catches typos."""
    for key, spec in TUNABLE_REGISTRY.items():
        if spec.kind == "boolean":
            assert isinstance(spec.preset, bool), f"{key}: preset not bool"
        elif spec.kind == "continuous":
            assert isinstance(spec.preset, (int, float)), (
                f"{key}: preset not numeric"
            )
            # booleans are ints too — explicitly exclude.
            assert not isinstance(spec.preset, bool)
        elif spec.kind == "discrete":
            assert spec.choices is not None, f"{key}: discrete missing choices"
            assert spec.preset in spec.choices, (
                f"{key}: preset not in choices"
            )


def test_custom_thresholds_forward_to_tuner(monkeypatch, tmp_path):
    monkeypatch.setenv("CONCINNO_ZIQ_TUNER_DIR", str(tmp_path))
    clear_cache()
    t = get_tuner(
        "fewshot.min_token_len",
        tunable_threshold=10,
        full_threshold=30,
        fresh=True,
        auto_persist=False,
    )
    assert t.tunable_threshold == 10
    assert t.full_threshold == 30


def test_clear_cache_drops_all(monkeypatch, tmp_path):
    monkeypatch.setenv("CONCINNO_ZIQ_TUNER_DIR", str(tmp_path))
    clear_cache()
    t1 = get_tuner("knowledge.pattern_threshold", auto_persist=False)
    clear_cache()
    t2 = get_tuner("knowledge.pattern_threshold", auto_persist=False)
    assert t1 is not t2
