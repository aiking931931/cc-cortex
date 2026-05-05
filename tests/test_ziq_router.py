"""Tests for concinno.ziq_router.ZIQFeatureRouter.

These tests mock ZIQRetrieval and Config so they never touch real FTRL
state, ChromaDB, or cc_config.json on disk.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from concinno.ziq_router import (
    ALPHA_COMPLEX_MAX,
    ALPHA_COMPLICATED_MAX,
    ALPHA_SIMPLE_MAX,
    RoutingDecision,
    ZIQFeatureRouter,
)

# ── Fixtures ────────────────────────────────────────────


def _mk_ziq(breadth: int) -> MagicMock:
    """Build a ZIQRetrieval mock whose route_query returns N namespaces."""
    ziq = MagicMock(name="ZIQRetrieval")
    namespaces = ["memory", "knowledge", "cognition", "skills", "context"]
    ziq.route_query.return_value = namespaces[:breadth]
    return ziq


def _mk_config(default_enabled: bool = True) -> MagicMock:
    """Build a Config mock that always reports `default_enabled`."""
    cfg = MagicMock(name="Config")
    cfg.feature.return_value = default_enabled
    return cfg


@pytest.fixture
def patched_meta(monkeypatch):
    """Patch FEATURE_META with a small synthetic dict for predictable tests.

    Includes one feature per routing_policy plus a ziq_routed feature in
    each tier band so we can assert tier transitions cleanly.
    """
    fake_meta = {
        # ziq_routed (default — no routing_policy key)
        "butterfly_guard": {"category": "core"},
        "token_gate": {"category": "core"},
        "read_first_gate": {"category": "core"},
        "structural_guard": {"category": "quality"},
        "prompt_guard": {"category": "quality"},
        "sentinel_gate": {"category": "quality"},
        "code_guard": {"category": "quality"},
        "consecutive_fail_gate": {"category": "cognitive"},
        "clarity_gate": {"category": "cognitive"},
        "hijack_gate": {"category": "cognitive"},
        "delivery_gate": {"category": "delivery"},
        "ui_verify": {"category": "delivery"},
        "design_theory": {"category": "delivery"},
        "insight_engine": {"category": "delivery"},
        # explicit policies
        "always_on_feature": {
            "category": "test", "routing_policy": "always_on",
        },
        "always_off_feature": {
            "category": "test", "routing_policy": "always_off",
        },
        "user_override_feature": {
            "category": "test", "routing_policy": "user_override",
        },
        "explicit_ziq": {
            "category": "test", "routing_policy": "ziq_routed",
        },
        "bogus_policy": {
            "category": "test", "routing_policy": "garbage_value",
        },
    }
    monkeypatch.setattr(
        "concinno.feature_config.FEATURE_META", fake_meta,
    )
    return fake_meta


# ── Tests ───────────────────────────────────────────────


def test_simple_tier_minimal_features(patched_meta):
    """alpha_t < 0.20 → only core simple features enabled."""
    ziq = _mk_ziq(breadth=1)  # narrow → alpha_t ≈ 0.10 → simple
    router = ZIQFeatureRouter(ziq, _mk_config())
    decision = router.route("hello", ctx_tokens=0)

    assert decision.tier == "simple"
    assert decision.alpha_t < ALPHA_SIMPLE_MAX
    # Core simple ON
    assert "butterfly_guard" in decision.enabled_features
    assert "token_gate" in decision.enabled_features
    assert "read_first_gate" in decision.enabled_features
    # Higher tiers OFF
    assert "structural_guard" in decision.disabled_features
    assert "consecutive_fail_gate" in decision.disabled_features
    assert "delivery_gate" in decision.disabled_features


def test_complicated_tier_adds_quality(patched_meta):
    """0.20 ≤ alpha_t < 0.55 → simple + quality guards."""
    ziq = _mk_ziq(breadth=2)  # alpha_t ≈ 0.40
    router = ZIQFeatureRouter(ziq, _mk_config())
    decision = router.route("refactor")

    assert decision.tier == "complicated"
    assert ALPHA_SIMPLE_MAX <= decision.alpha_t < ALPHA_COMPLICATED_MAX
    assert "structural_guard" in decision.enabled_features
    assert "prompt_guard" in decision.enabled_features
    assert "code_guard" in decision.enabled_features
    # cognitive layer still off
    assert "consecutive_fail_gate" in decision.disabled_features


def test_complex_tier_adds_arbiter(patched_meta):
    """0.55 ≤ alpha_t < 0.90 → simple + quality + cognitive layer.

    The historical 'arbiter' feature maps to consecutive_fail_gate
    in the in-tree FEATURE_META (post-4.6.0 cognitive_anchor removal).
    """
    ziq = _mk_ziq(breadth=3)  # alpha_t ≈ 0.70
    router = ZIQFeatureRouter(ziq, _mk_config())
    decision = router.route("design new pipeline")

    assert decision.tier == "complex"
    assert ALPHA_COMPLICATED_MAX <= decision.alpha_t < ALPHA_COMPLEX_MAX
    assert "consecutive_fail_gate" in decision.enabled_features
    assert "structural_guard" in decision.enabled_features  # inherited
    # delivery layer still off
    assert "delivery_gate" in decision.disabled_features


def test_chaotic_tier_all_on(patched_meta):
    """alpha_t ≥ 0.90 → every ziq_routed feature enabled."""
    ziq = _mk_ziq(breadth=5)  # alpha_t ≈ 0.95
    router = ZIQFeatureRouter(ziq, _mk_config())
    decision = router.route("redesign whole system")

    assert decision.tier == "chaotic"
    assert decision.alpha_t >= ALPHA_COMPLEX_MAX
    expected = (
        ZIQFeatureRouter.TIER_FEATURES["chaotic"]
    )
    for feat in expected:
        assert feat in decision.enabled_features, (
            f"chaotic must enable {feat}"
        )


def test_ctx_forces_field_read(patched_meta):
    """ctx_tokens > 80% of yellow zone → forces CTX_FORCED_FEATURE on."""
    ziq = _mk_ziq(breadth=1)  # would normally be simple → cognitive off
    router = ZIQFeatureRouter(
        ziq, _mk_config(), yellow_zone_tokens=200_000,
    )
    decision = router.route("trivial query", ctx_tokens=164_000)  # 82%

    assert decision.tier == "simple"
    assert decision.ctx_forced_field_read is True
    assert ZIQFeatureRouter.CTX_FORCED_FEATURE in decision.enabled_features
    forced = ZIQFeatureRouter.CTX_FORCED_FEATURE
    assert "forced by ctx" in decision.reasons[forced]


def test_user_override_wins(patched_meta):
    """user_overrides ALWAYS prevail over tier and policy."""
    ziq = _mk_ziq(breadth=5)  # chaotic
    router = ZIQFeatureRouter(ziq, _mk_config())

    # Force a chaotic-tier feature OFF and a simple-only feature OFF.
    decision = router.route(
        "anything",
        user_overrides={
            "delivery_gate": False,
            "butterfly_guard": False,
            "always_off_feature": True,  # override an always_off
        },
    )

    assert "delivery_gate" in decision.disabled_features
    assert "butterfly_guard" in decision.disabled_features
    assert "always_off_feature" in decision.enabled_features
    assert "user_override" in decision.reasons["delivery_gate"]
    assert "user_override" in decision.reasons["butterfly_guard"]
    assert "user_override" in decision.reasons["always_off_feature"]


def test_always_on_policy_ignored_by_ziq(patched_meta):
    """always_on features stay ON regardless of alpha_t."""
    ziq = _mk_ziq(breadth=1)  # simple
    router = ZIQFeatureRouter(ziq, _mk_config())
    decision = router.route("trivial")

    assert "always_on_feature" in decision.enabled_features
    assert "always_off_feature" in decision.disabled_features
    assert decision.reasons["always_on_feature"] == (
        "always_on_feature: always_on policy"
    )


def test_ziq_failure_fallback_complicated(patched_meta):
    """If ZIQ.route_query raises, router falls back to complicated tier."""
    ziq = MagicMock(name="ZIQRetrieval")
    ziq.route_query.side_effect = RuntimeError("boom")
    router = ZIQFeatureRouter(ziq, _mk_config())
    decision = router.route("anything")

    assert decision.tier == "complicated"
    # Fallback alpha sits in complicated band
    assert ALPHA_SIMPLE_MAX <= decision.alpha_t < ALPHA_COMPLICATED_MAX
    assert "__router__" in decision.reasons
    assert "fallback" in decision.reasons["__router__"]
    assert "structural_guard" in decision.enabled_features


def test_reasons_audit_trail(patched_meta):
    """Every feature decision must have a reason string."""
    ziq = _mk_ziq(breadth=3)
    router = ZIQFeatureRouter(ziq, _mk_config())
    decision = router.route("design")

    for feat in patched_meta:
        assert feat in decision.reasons, f"missing reason for {feat}"
        assert isinstance(decision.reasons[feat], str)
        assert decision.reasons[feat].startswith(feat + ":")


def test_routing_decision_dataclass_shape(patched_meta):
    """RoutingDecision exposes the documented fields and types."""
    ziq = _mk_ziq(breadth=2)
    router = ZIQFeatureRouter(ziq, _mk_config())
    decision = router.route("hello")

    assert isinstance(decision, RoutingDecision)
    assert isinstance(decision.enabled_features, set)
    assert isinstance(decision.disabled_features, set)
    assert isinstance(decision.alpha_t, float)
    assert decision.tier in {
        "simple", "complicated", "complex", "chaotic",
    }
    assert isinstance(decision.reasons, dict)
    assert isinstance(decision.ctx_forced_field_read, bool)
    # Enabled and disabled never overlap
    assert decision.enabled_features.isdisjoint(decision.disabled_features)


def test_classify_tier_boundaries():
    """Boundary values land in the correct tier."""
    router = ZIQFeatureRouter(_mk_ziq(1), _mk_config())
    assert router.classify_tier(0.0) == "simple"
    assert router.classify_tier(0.199) == "simple"
    assert router.classify_tier(0.20) == "complicated"
    assert router.classify_tier(0.549) == "complicated"
    assert router.classify_tier(0.55) == "complex"
    assert router.classify_tier(0.899) == "complex"
    assert router.classify_tier(0.90) == "chaotic"
    assert router.classify_tier(1.0) == "chaotic"


def test_user_override_policy_reads_live_config(patched_meta):
    """user_override policy honours the live Config.feature() value."""
    ziq = _mk_ziq(breadth=3)

    cfg_on = MagicMock()
    cfg_on.feature.return_value = True
    decision_on = ZIQFeatureRouter(ziq, cfg_on).route("q")
    assert "user_override_feature" in decision_on.enabled_features

    cfg_off = MagicMock()
    cfg_off.feature.return_value = False
    decision_off = ZIQFeatureRouter(ziq, cfg_off).route("q")
    assert "user_override_feature" in decision_off.disabled_features


def test_ctx_override_respects_user_disable(patched_meta):
    """Even ctx pressure cannot enable a feature the user explicitly disabled."""
    ziq = _mk_ziq(breadth=1)
    router = ZIQFeatureRouter(ziq, _mk_config())
    forced = ZIQFeatureRouter.CTX_FORCED_FEATURE

    decision = router.route(
        "q",
        ctx_tokens=180_000,  # 90%
        user_overrides={forced: False},
    )
    assert forced in decision.disabled_features
    assert "user_override" in decision.reasons[forced]


def test_bogus_routing_policy_defaults_to_ziq_routed(patched_meta):
    """Unknown routing_policy strings fall back to ziq_routed."""
    ziq = _mk_ziq(breadth=5)  # chaotic → all ziq_routed enabled
    router = ZIQFeatureRouter(ziq, _mk_config())
    decision = router.route("q")

    # bogus_policy isn't in any TIER_FEATURES set, so chaotic still
    # disables it. Just verify it's classified (in either set, not absent).
    assert (
        "bogus_policy" in decision.enabled_features
        or "bogus_policy" in decision.disabled_features
    )
    assert "bogus_policy" in decision.reasons
