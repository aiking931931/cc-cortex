"""Tests for cognitive.router — CBUA C0 Perception + routing."""

from __future__ import annotations

from concinno.cognitive.router import (
    CognitiveLevel,
    CognitiveRoute,
    ComplexityDomain,
    ModelTier,
    classify_complexity,
    detect_asset_types,
    detect_model_tier,
    format_route_context,
    route,
)

# ── ComplexityDomain classification ───────────────────────


class TestClassifyComplexity:
    def test_simple_pattern_match(self):
        domain, signals = classify_complexity("修改一下檔名")
        assert domain == ComplexityDomain.SIMPLE
        assert signals["markers"] == "simple"

    def test_simple_english(self):
        domain, _ = classify_complexity("fix typo in README")
        assert domain == ComplexityDomain.SIMPLE

    def test_complicated_multi_step(self):
        domain, signals = classify_complexity(
            "先讀交接檔，然後修改 API，接著更新測試，最後部署"
        )
        assert domain == ComplexityDomain.COMPLICATED
        assert signals["estimated_steps"] >= 4

    def test_complex_markers(self):
        domain, signals = classify_complexity("探索一下這個新的架構，不確定該怎麼做")
        assert domain == ComplexityDomain.COMPLEX
        assert signals["markers"] == "complex"

    def test_chaotic_markers(self):
        domain, signals = classify_complexity("緊急！伺服器崩潰了，全掛")
        assert domain == ComplexityDomain.CHAOTIC
        assert signals["markers"] == "chaotic"

    def test_neutral_short_is_simple(self):
        domain, _ = classify_complexity("hello")
        assert domain == ComplexityDomain.SIMPLE

    def test_known_pattern_downgrades(self):
        """R2 fix: known pattern → downgrade even if message is long."""
        domain, _ = classify_complexity(
            "這是一個很長的訊息但其實很簡單因為有已知模式可以用",
            has_known_pattern=True,
        )
        assert domain == ComplexityDomain.SIMPLE

    def test_high_step_count_upgrades(self):
        msg = "1. 做A 2. 做B 3. 做C 4. 做D 5. 做E 6. 做F 7. 做G 8. 做H 9. 做I 10. 做J 11. 做K"
        domain, signals = classify_complexity(msg)
        assert signals["estimated_steps"] >= 10
        assert domain in (ComplexityDomain.COMPLICATED, ComplexityDomain.COMPLEX)

    def test_tool_count_override(self):
        domain, signals = classify_complexity("做個東西", tool_count_estimate=15)
        assert signals["estimated_steps"] == 15
        assert domain == ComplexityDomain.COMPLEX


# ── Model tier detection ──────────────────────────────────


class TestDetectModelTier:
    def test_opus(self):
        assert detect_model_tier("claude-opus-4-6") == ModelTier.T1_STRONG

    def test_sonnet(self):
        assert detect_model_tier("claude-sonnet-4-6") == ModelTier.T2_MEDIUM

    def test_haiku(self):
        assert detect_model_tier("claude-haiku-4-5") == ModelTier.T3_WEAK

    def test_unknown_defaults_t2(self):
        assert detect_model_tier("gpt-4o") == ModelTier.T2_MEDIUM

    def test_empty_defaults_t2(self):
        assert detect_model_tier("") == ModelTier.T2_MEDIUM

    def test_env_var(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_MODEL", "claude-opus-4-6")
        assert detect_model_tier() == ModelTier.T1_STRONG


# ── Asset type detection ──────────────────────────────────


class TestDetectAssetTypes:
    def test_code_default(self):
        assert detect_asset_types("hello world") == ["code"]

    def test_image(self):
        types = detect_asset_types("幫我生圖一張照片")
        assert "image" in types

    def test_video(self):
        types = detect_asset_types("做一段舞蹈影片")
        assert "video" in types

    def test_audio(self):
        types = detect_asset_types("產生語音音訊")
        assert "audio" in types

    def test_document(self):
        types = detect_asset_types("修改 word 文件")
        assert "document" in types

    def test_mixed(self):
        types = detect_asset_types("修改代碼然後生圖，再做影片")
        assert "code" in types
        assert "image" in types
        assert "video" in types


# ── Route function ────────────────────────────────────────


class TestRoute:
    def test_simple_opus(self):
        r = route("修改檔名", model_name="claude-opus-4-6")
        assert r.complexity == ComplexityDomain.SIMPLE
        assert r.tier == ModelTier.T1_STRONG
        assert r.entry_level == CognitiveLevel.C1_FAST
        assert r.scaffolding == "none"
        assert r.reasoning_budget_pct == 15
        assert r.action_budget_pct == 75

    def test_complicated_sonnet(self):
        r = route(
            "先讀交接檔，然後修改 API，接著更新測試，最後部署",
            model_name="claude-sonnet-4-6",
        )
        assert r.complexity == ComplexityDomain.COMPLICATED
        assert r.tier == ModelTier.T2_MEDIUM
        assert r.entry_level == CognitiveLevel.C2_STRUCTURED
        assert r.scaffolding == "standard"

    def test_complex_haiku(self):
        r = route("探索新架構，不確定方向", model_name="claude-haiku-4-5")
        assert r.complexity == ComplexityDomain.COMPLEX
        assert r.tier == ModelTier.T3_WEAK
        assert r.entry_level == CognitiveLevel.C3_DEEP
        assert r.scaffolding == "maximum"
        assert r.reasoning_budget_pct == 35
        assert r.meta_budget_pct == 25

    def test_chaotic(self):
        r = route("緊急崩潰了")
        assert r.complexity == ComplexityDomain.CHAOTIC
        assert r.entry_level == CognitiveLevel.C3_DEEP

    def test_asset_types_detected(self):
        r = route("修改代碼然後生圖")
        assert "code" in r.asset_types
        assert "image" in r.asset_types

    def test_signals_populated(self):
        r = route("修改檔名")
        assert "markers" in r.signals
        assert "estimated_steps" in r.signals
        assert "has_known_pattern" in r.signals

    def test_budget_sums_to_100(self):
        for msg, model in [
            ("fix typo", "opus"),
            ("先做A然後B接著C", "sonnet"),
            ("探索未知", "haiku"),
            ("緊急崩潰", ""),
        ]:
            r = route(msg, model_name=model)
            total = r.reasoning_budget_pct + r.action_budget_pct + r.meta_budget_pct
            assert total == 100, f"Budget doesn't sum to 100: {total} for {msg}"


# ── Format route context ──────────────────────────────────


class TestFormatRouteContext:
    def test_simple_returns_empty(self):
        r = route("修改檔名", model_name="opus")
        assert format_route_context(r) == ""

    def test_complicated_returns_context(self):
        r = route("先做A然後B接著C最後D", model_name="sonnet")
        ctx = format_route_context(r)
        assert "[CBUA]" in ctx
        assert "complicated" in ctx

    def test_complex_includes_budget(self):
        r = route("探索新方向，不確定", model_name="sonnet")
        ctx = format_route_context(r)
        assert "R35/A40/M25" in ctx

    def test_non_code_assets_shown(self):
        r = CognitiveRoute(
            complexity=ComplexityDomain.COMPLICATED,
            tier=ModelTier.T2_MEDIUM,
            entry_level=CognitiveLevel.C2_STRUCTURED,
            reasoning_budget_pct=30,
            action_budget_pct=50,
            meta_budget_pct=20,
            scaffolding="standard",
            asset_types=["code", "image"],
        )
        ctx = format_route_context(r)
        assert "image" in ctx
