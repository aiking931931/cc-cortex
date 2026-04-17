"""Tests for concinno.feature_config module."""

from __future__ import annotations

from concinno.feature_config import (
    FEATURE_META,
    get_feature,
    list_features,
    validate_value,
)

# ── validate_value ───────────────────────────────────────


def test_validate_unknown_feature():
    result = validate_value("nonexistent_feature", "enabled", True)
    assert len(result) == 1
    assert "Unknown feature" in result[0]


def test_validate_unknown_param():
    result = validate_value("token_gate", "bogus_param", 100)
    assert len(result) == 1
    assert "Unknown param" in result[0]


def test_validate_enabled_must_be_bool():
    result = validate_value("token_gate", "enabled", "yes")
    assert len(result) == 1
    assert "must be bool" in result[0]


def test_validate_enabled_bool_ok():
    result = validate_value("token_gate", "enabled", True)
    assert result == []


def test_validate_int_below_min():
    # agent_threshold min=80000
    result = validate_value("token_gate", "agent_threshold", 50000)
    assert any("below minimum" in w for w in result)
    assert any("risk" in w.lower() or "80K" in w or "80000" in w for w in result)


def test_validate_int_above_max():
    # agent_threshold max=180000
    result = validate_value("token_gate", "agent_threshold", 200000)
    assert any("above maximum" in w for w in result)


def test_validate_value_not_recommended_gives_info():
    # agent_threshold recommended=140000
    result = validate_value("token_gate", "agent_threshold", 100000)
    assert any("Recommended" in w for w in result)


def test_validate_value_at_recommended_no_warning():
    result = validate_value("token_gate", "agent_threshold", 140000)
    assert result == []


def test_validate_bool_param_false_gives_risk_off():
    # sentinel_gate.lint_exception has risk_off
    result = validate_value("sentinel_gate", "lint_exception", False)
    assert len(result) == 1
    assert "risk" in result[0].lower() or "lint exception" in result[0].lower()


def test_validate_bool_param_true_no_warning():
    result = validate_value("sentinel_gate", "lint_exception", True)
    assert result == []


def test_validate_int_type_mismatch():
    result = validate_value("token_gate", "agent_threshold", "not_int")
    assert len(result) == 1
    assert "must be int" in result[0]


# ── get_feature ──────────────────────────────────────────


def test_get_feature_valid():
    info = get_feature("token_gate")
    assert info is not None
    assert info["name"] == "token_gate"
    assert info["category"] == "hard_gate"
    assert "params" in info
    assert "agent_threshold" in info["params"]


def test_get_feature_invalid():
    assert get_feature("does_not_exist") is None


def test_get_feature_with_lang_zh():
    info = get_feature("token_gate", lang="zh")
    assert info is not None
    # description should be the zh version
    assert info["description"] == FEATURE_META["token_gate"]["description_zh"]


# ── list_features ────────────────────────────────────────


def test_list_features_returns_list():
    features = list_features()
    assert isinstance(features, list)
    assert len(features) > 0


def test_list_features_structure():
    features = list_features()
    for feat in features:
        assert "name" in feat
        assert "category" in feat
        assert "description" in feat
        assert "enabled" in feat
        assert "params" in feat


# ── FEATURE_META categories ──────────────────────────────


def test_feature_meta_has_expected_categories():
    categories = {m["category"] for m in FEATURE_META.values()}
    expected = {"hard_gate", "hard_quality", "ux", "context"}
    assert expected.issubset(categories), f"Missing categories: {expected - categories}"
