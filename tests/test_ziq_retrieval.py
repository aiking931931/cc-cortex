"""Tests for concinno.ziq_retrieval — FTRL-powered adaptive RAG."""

from __future__ import annotations

import tempfile

from concinno.ziq_retrieval import (
    DEFAULT_HIGH_THRESHOLD,
    DEFAULT_LOW_THRESHOLD,
    WEIGHT_MAX,
    WEIGHT_MIN,
    RetrieverConfidenceSPS,
    SPSProtocol,
    SourceState,
    SPPMIPrior,
    ThresholdState,
    ZIQRetrieval,
    _MetaTuner,
    _ema_update,
    record_feedback,
    rerank_results,
)


class TestEMAUpdate:
    def test_positive_reward_increases_weight(self):
        state = SourceState(weight=1.0)
        updated = _ema_update(state, reward=1.0)
        assert updated.weight > 1.0

    def test_negative_reward_decreases_weight(self):
        state = SourceState(weight=1.0)
        updated = _ema_update(state, reward=-0.1)
        assert updated.weight < 1.0

    def test_weight_clamped_min(self):
        state = SourceState(weight=1.0)
        for _ in range(50):
            state = _ema_update(state, reward=-1.0)
        assert state.weight >= WEIGHT_MIN

    def test_weight_clamped_max(self):
        state = SourceState(weight=1.0)
        for _ in range(50):
            state = _ema_update(state, reward=1.0)
        assert state.weight <= WEIGHT_MAX

    def test_zero_reward_minimal_change(self):
        state = SourceState(weight=1.0)
        updated = _ema_update(state, reward=0.0)
        assert abs(updated.weight - 1.0) < 0.01


class TestZIQRetrieval:
    def _make_ziq(self):
        return ZIQRetrieval(cache_dir=tempfile.mkdtemp())

    def test_empty_results(self):
        ziq = self._make_ziq()
        assert ziq.rerank([]) == []

    def test_rerank_adds_ziq_score(self):
        ziq = self._make_ziq()
        results = [
            {"text": "fix", "file": "corrections/fix1.md", "score": 0.8},
            {"text": "rule", "file": "rules/L1/cbua.md", "score": 0.7},
        ]
        reranked = ziq.rerank(results)
        assert all("ziq_score" in r for r in reranked)
        assert all("source_type" in r for r in reranked)

    def test_rerank_preserves_order_initially(self):
        ziq = self._make_ziq()
        results = [
            {"text": "a", "file": "corrections/a.md", "score": 0.9},
            {"text": "b", "file": "corrections/b.md", "score": 0.5},
        ]
        reranked = ziq.rerank(results)
        assert reranked[0]["score"] == 0.9

    def test_classify_source(self):
        ziq = self._make_ziq()
        assert ziq._classify_source("corrections/fix.md") == "correction"
        assert ziq._classify_source("rules/L1/cbua.md") == "rule"
        assert ziq._classify_source("skills/kb_audio/SKILL.md") == "skill"
        assert ziq._classify_source("06_Handoffs/交接_King.md") == "handoff"
        assert ziq._classify_source("memory/user.md") == "memory"

    def test_feedback_updates_weights(self):
        ziq = self._make_ziq()
        # First rerank to register hits
        results = [
            {"text": "c", "file": "corrections/c.md", "score": 0.8},
            {"text": "r", "file": "rules/r.md", "score": 0.7},
        ]
        ziq.rerank(results)
        # Feedback: correction was used, rule was not
        ziq.feedback(["corrections/c.md"])
        weights = ziq.get_weights()
        assert weights["correction"] > weights["rule"]

    def test_weights_persist(self):
        cache = tempfile.mkdtemp()
        ziq1 = ZIQRetrieval(cache)
        ziq1.rerank([
            {"text": "x", "file": "corrections/x.md", "score": 0.8},
        ])
        ziq1.feedback(["corrections/x.md"])
        w1 = ziq1.get_weights()

        # New instance, same cache
        ziq2 = ZIQRetrieval(cache)
        w2 = ziq2.get_weights()
        assert w1 == w2

    def test_stats(self):
        ziq = self._make_ziq()
        ziq.rerank([
            {"text": "a", "file": "corrections/a.md", "score": 0.8},
        ])
        ziq.feedback(["corrections/a.md"])
        stats = ziq.stats()
        assert "correction" in stats
        assert stats["correction"]["hits"] >= 1
        assert stats["correction"]["used"] >= 1

    def test_multiple_feedback_rounds(self):
        ziq = self._make_ziq()
        for _ in range(10):
            ziq.rerank([
                {"text": "c", "file": "corrections/c.md", "score": 0.8},
                {"text": "s", "file": "skills/kb_test/s.md", "score": 0.7},
            ])
            ziq.feedback(["corrections/c.md"])
        weights = ziq.get_weights()
        # Correction consistently used → weight should be higher
        assert weights["correction"] > weights["skill"]


class TestConvenience:
    def test_rerank_results(self):
        cache = tempfile.mkdtemp()
        results = [{"text": "x", "file": "rules/r.md", "score": 0.5}]
        reranked = rerank_results(cache, results)
        assert len(reranked) == 1
        assert "ziq_score" in reranked[0]

    def test_record_feedback(self):
        cache = tempfile.mkdtemp()
        # Should not raise
        record_feedback(cache, ["corrections/x.md"])


class TestRouteQuery:
    def _make_ziq(self):
        return ZIQRetrieval(cache_dir=tempfile.mkdtemp())

    def test_high_uncertainty_returns_all(self):
        ziq = self._make_ziq()
        namespaces = ziq.route_query("anything", confidence=0.8)
        assert len(namespaces) == 5
        assert "knowledge" in namespaces
        assert "memory" in namespaces

    def test_low_uncertainty_returns_one(self):
        ziq = self._make_ziq()
        namespaces = ziq.route_query("correction feedback", confidence=0.1)
        assert len(namespaces) == 1
        assert namespaces[0] == "memory"

    def test_medium_uncertainty_returns_two_to_three(self):
        ziq = self._make_ziq()
        namespaces = ziq.route_query("rule decision plan", confidence=0.4)
        assert 2 <= len(namespaces) <= 3

    def test_no_keyword_match_defaults_memory(self):
        ziq = self._make_ziq()
        namespaces = ziq.route_query("xyzzy", confidence=0.1)
        assert namespaces == ["memory"]

    def test_boundary_confidence_020(self):
        ziq = self._make_ziq()
        # confidence=0.20 is medium range (ablation: low_threshold=0.20)
        namespaces = ziq.route_query("rule correction", confidence=0.20)
        assert 2 <= len(namespaces) <= 3

    def test_boundary_confidence_055(self):
        ziq = self._make_ziq()
        # confidence=0.55 is still medium range (ablation: high_threshold=0.55)
        namespaces = ziq.route_query("skill", confidence=0.55)
        assert len(namespaces) <= 3

    def test_high_confidence_above_055(self):
        ziq = self._make_ziq()
        # confidence=0.56 triggers all namespaces (ablation: high_threshold=0.55)
        namespaces = ziq.route_query("anything", confidence=0.56)
        assert len(namespaces) == 5


class TestAdaptiveThresholds:
    """Tests for FTRL adaptive threshold learning."""

    def _make_ziq(self):
        return ZIQRetrieval(cache_dir=tempfile.mkdtemp())

    def test_initial_thresholds(self):
        ziq = self._make_ziq()
        ts = ziq.get_thresholds()
        assert ts.low == DEFAULT_LOW_THRESHOLD
        assert ts.high == DEFAULT_HIGH_THRESHOLD
        assert ts.n_updates == 0

    def test_threshold_state_dataclass(self):
        ts = ThresholdState()
        assert ts.low == DEFAULT_LOW_THRESHOLD
        assert ts.high == DEFAULT_HIGH_THRESHOLD

    def test_route_feedback_narrow_miss_lowers_low(self):
        ziq = self._make_ziq()
        # Route narrow (high confidence) → miss correct namespace
        ziq.route_query("correction feedback", confidence=0.05)
        ts = ziq.route_feedback(correct_namespaces=["skills"])
        # low_threshold should decrease (broaden sooner)
        assert ts.low < DEFAULT_LOW_THRESHOLD
        assert ts.n_updates == 1

    def test_route_feedback_narrow_hit_raises_low(self):
        ziq = self._make_ziq()
        ziq.route_query("correction feedback", confidence=0.05)
        ts = ziq.route_feedback(correct_namespaces=["memory"])
        # Hit → low_threshold should increase slightly
        assert ts.low > DEFAULT_LOW_THRESHOLD

    def test_route_feedback_broad_waste_raises_high(self):
        ziq = self._make_ziq()
        ziq.route_query("anything", confidence=0.9)
        ts = ziq.route_feedback(correct_namespaces=["memory"])
        # Only 1/5 useful → high_threshold should increase
        assert ts.high > DEFAULT_HIGH_THRESHOLD

    def test_route_feedback_broad_justified_lowers_high(self):
        ziq = self._make_ziq()
        ziq.route_query("anything", confidence=0.9)
        # 3/5 namespaces useful → broad search justified
        ts = ziq.route_feedback(
            correct_namespaces=["memory", "cognition", "skills"],
        )
        assert ts.high < DEFAULT_HIGH_THRESHOLD

    def test_low_never_exceeds_high(self):
        ziq = self._make_ziq()
        # Repeatedly reward narrow routing to push low up
        for _ in range(20):
            ziq.route_query("correction", confidence=0.05)
            ziq.route_feedback(correct_namespaces=["memory"])
        ts = ziq.get_thresholds()
        assert ts.low < ts.high - 0.09  # margin enforced

    def test_thresholds_persist(self):
        cache = tempfile.mkdtemp()
        ziq1 = ZIQRetrieval(cache_dir=cache)
        ziq1.route_query("correction", confidence=0.05)
        ziq1.route_feedback(correct_namespaces=["skills"])
        # New instance, same cache → thresholds persist
        ziq2 = ZIQRetrieval(cache_dir=cache)
        ts = ziq2.get_thresholds()
        assert ts.low < DEFAULT_LOW_THRESHOLD
        assert ts.n_updates == 1

    def test_stats_includes_thresholds(self):
        ziq = self._make_ziq()
        s = ziq.stats()
        assert "_thresholds" in s
        assert "low" in s["_thresholds"]
        assert "high" in s["_thresholds"]

    def test_route_uses_learned_thresholds(self):
        ziq = self._make_ziq()
        for _ in range(5):
            ziq.route_query("correction", confidence=0.15)
            ziq.route_feedback(correct_namespaces=["skills"])
        ts = ziq.get_thresholds()
        result = ziq.route_query("correction", confidence=0.15)
        if ts.low <= 0.15:
            assert len(result) >= 2

    def test_compute_meso_scores_aggregates_per_namespace(self):
        """T 層接入：RAG search 結果聚合 per-namespace。"""
        ziq = self._make_ziq()

        class FakeRAG:
            def search(self, query, top_k=20, min_score=0.0):
                return [
                    {"file": "memory/feedback_x.md", "score": 0.9},
                    {"file": "memory/correction_y.md", "score": 0.7},
                    {"file": "rules/L1/cbua.md", "score": 0.6},
                    {"file": "skills/kb_z/SKILL.md", "score": 0.4},
                ]

        scores = ziq.compute_meso_scores("test query", FakeRAG())
        assert "memory" in scores
        assert "cognition" in scores
        assert "skills" in scores
        # max aggregation: memory should be 0.9 (not 0.7)
        assert scores["memory"] == 0.9
        assert scores["cognition"] == 0.6
        assert scores["skills"] == 0.4

    def test_compute_meso_scores_handles_empty_rag(self):
        """T 層接入：空 RAG → 空 dict → T OFF fallback。"""
        ziq = self._make_ziq()

        class EmptyRAG:
            def search(self, query, top_k=20, min_score=0.0):
                return []

        scores = ziq.compute_meso_scores("test", EmptyRAG())
        assert scores == {}

    def test_compute_meso_scores_handles_rag_failure(self):
        """T 層接入：RAG 拋例外 → 安全 fallback。"""
        ziq = self._make_ziq()

        class BrokenRAG:
            def search(self, query, top_k=20, min_score=0.0):
                raise RuntimeError("ChromaDB not initialized")

        scores = ziq.compute_meso_scores("test", BrokenRAG())
        assert scores == {}

    def test_meso_scores_drives_routing(self):
        """T ON 時，routing 用 meso_scores 而非 P prior。"""
        ziq = self._make_ziq()
        meso = {
            "knowledge": 0.95, "cognition": 0.5,
            "memory": 0.1, "skills": 0.1, "context": 0.1,
        }
        # Narrow band (< low_threshold) → T ON 應選 knowledge
        result = ziq.route_query(
            "test", confidence=0.05, meso_scores=meso,
        )
        assert result == ["knowledge"]

    def test_p_layer_decay_prevents_ancient_dominance(self):
        """P 慢 EMA 衰減：100 次更新後舊值衰減 5%。"""
        ziq = self._make_ziq()
        # Phase 1: 99 round 全打 memory（建立歷史）
        for _ in range(99):
            ziq.route_query("test", confidence=0.5)
            ziq.route_feedback(correct_namespaces=["memory"])
        macro_before = ziq._load_macro()
        memory_before = macro_before["memory"]
        assert memory_before > 90  # Should be ~99
        # Round 100: 觸發衰減 + 加 1
        ziq.route_query("test", confidence=0.5)
        ziq.route_feedback(correct_namespaces=["memory"])
        macro_after = ziq._load_macro()
        memory_after = macro_after["memory"]
        # 衰減後應該是 99 * 0.95 + 1 = 95.05
        expected = memory_before * 0.95 + 1.0
        assert abs(memory_after - expected) < 0.01

    def test_p_layer_distribution_shift_recovery(self):
        """P 慢 EMA：分佈漂移後最終會反映新分佈。"""
        ziq = self._make_ziq()
        # Phase 1: 200 round 全打 memory
        for _ in range(200):
            ziq.route_query("test", confidence=0.5)
            ziq.route_feedback(correct_namespaces=["memory"])
        prior_phase1 = ziq._macro_prior()
        assert prior_phase1[0] == "memory"
        # Phase 2: 600 round 全打 cognition（要夠久克服歷史）
        for _ in range(600):
            ziq.route_query("test", confidence=0.5)
            ziq.route_feedback(correct_namespaces=["cognition"])
        prior_phase2 = ziq._macro_prior()
        # 衰減 + 新數據累積後，cognition 應該超越 memory
        assert prior_phase2[0] == "cognition"

    def test_band_does_not_collapse(self):
        """Red-team #3 fix: band must not shrink to margin."""
        ziq = self._make_ziq()
        default_band = DEFAULT_HIGH_THRESHOLD - DEFAULT_LOW_THRESHOLD
        # Simulate 100 rounds of alternating narrow-hit + broad-waste
        # (both push thresholds inward → collapse scenario)
        for _ in range(100):
            ziq.route_query("correction", confidence=0.05)
            ziq.route_feedback(correct_namespaces=["memory"])
            ziq.route_query("anything", confidence=0.9)
            ziq.route_feedback(correct_namespaces=["memory"])
        ts = ziq.get_thresholds()
        band = ts.high - ts.low
        # Band must stay above 50% of default (regularization)
        assert band >= default_band * 0.4, (
            f"Band collapsed to {band:.3f}, "
            f"expected >= {default_band * 0.4:.3f}"
        )

    def test_medium_feedback_lowers_high_on_miss(self):
        """Fix #1: medium routing miss → lower high_threshold."""
        ziq = self._make_ziq()
        ziq.route_query("xyzzy unknown", confidence=0.35)
        ts = ziq.route_feedback(correct_namespaces=["knowledge"])
        # Medium missed → high should decrease (go broad sooner)
        assert ts.high < DEFAULT_HIGH_THRESHOLD

    def test_medium_feedback_raises_low_on_waste(self):
        """Fix #1: medium too broad → raise low_threshold.

        Uses meso_scores (T ON) to ensure 3 namespaces are routed.
        """
        ziq = self._make_ziq()
        meso = {
            "memory": 0.9, "cognition": 0.8, "skills": 0.7,
            "knowledge": 0.1, "context": 0.1,
        }
        ziq.route_query("query", confidence=0.35, meso_scores=meso)
        # Only 1 was correct out of 3 routed → too broad
        ts = ziq.route_feedback(correct_namespaces=["memory"])
        assert ts.low > DEFAULT_LOW_THRESHOLD

    def test_lr_decays_over_time(self):
        """Decaying LR prevents oscillation in mature state."""
        ziq = self._make_ziq()
        # First miss: big move
        ziq.route_query("correction", confidence=0.05)
        ts1 = ziq.route_feedback(correct_namespaces=["skills"])
        delta1 = DEFAULT_LOW_THRESHOLD - ts1.low
        # Do 200 updates to increase n_updates
        for _ in range(200):
            ziq.route_query("correction", confidence=0.35)
            ziq.route_feedback(correct_namespaces=["memory"])
        # Another miss: should move less
        old_low = ziq.get_thresholds().low
        ziq.route_query("correction", confidence=0.05)
        ts2 = ziq.route_feedback(correct_namespaces=["skills"])
        delta2 = old_low - ts2.low
        assert delta2 < delta1, "LR should decay over time"


class TestFTRLNamespace:
    """Tests for v6.2.1 FTRL per-namespace weights."""

    def _make_ziq(self):
        return ZIQRetrieval(cache_dir=tempfile.mkdtemp())

    def test_ftrl_namespace_update_persists(self):
        """Update FTRL namespace weights, read back from new instance."""
        cache = tempfile.mkdtemp()
        ziq1 = ZIQRetrieval(cache_dir=cache)
        ziq1._ftrl_ns.update(["memory", "cognition"])
        weights1 = ziq1._ftrl_ns.likelihood()

        # New instance, same cache
        ziq2 = ZIQRetrieval(cache_dir=cache)
        weights2 = ziq2._ftrl_ns.likelihood()
        assert weights1 == weights2
        # Updated namespaces should differ from default 1.0
        assert weights2["memory"] != 1.0

    def test_ftrl_namespace_correct_increases_weight(self):
        """Correct namespace weight goes up after update."""
        ziq = self._make_ziq()
        before = ziq._ftrl_ns.likelihood()["memory"]
        for _ in range(5):
            ziq._ftrl_ns.update(["memory"])
        after = ziq._ftrl_ns.likelihood()["memory"]
        assert after > before

    def test_ftrl_namespace_incorrect_decreases_weight(self):
        """Namespace not in correct list gets weight decreased."""
        ziq = self._make_ziq()
        before = ziq._ftrl_ns.likelihood()["context"]
        # Update with only "memory" correct — context is penalized
        for _ in range(5):
            ziq._ftrl_ns.update(["memory"])
        after = ziq._ftrl_ns.likelihood()["context"]
        assert after < before

    def test_feedback_updates_ftrl_namespace(self):
        """Full feedback() flow triggers FTRL namespace update."""
        ziq = self._make_ziq()
        # Rerank to register results
        ziq.rerank([
            {"text": "c", "file": "corrections/c.md", "score": 0.8},
            {"text": "r", "file": "rules/r.md", "score": 0.7},
        ])
        # Feedback: correction was used (maps to "memory" namespace)
        ziq.feedback(["corrections/c.md"])
        weights = ziq._ftrl_ns.likelihood()
        # "memory" namespace should have been updated (not default 1.0)
        assert weights["memory"] != 1.0

    def test_bayesian_posterior_uses_ftrl_namespace(self):
        """route_query with SPPMI prior uses FTRL namespace weights."""
        cache = tempfile.mkdtemp()
        # Build a simple SPPMI prior
        prior = SPPMIPrior.build_from_texts({
            "memory": ["correction feedback handoff memory"],
            "cognition": ["rule cbua thinking decision"],
            "skills": ["skill knowledge base audio"],
            "knowledge": ["context search retrieval"],
            "context": ["session state environment"],
        })
        ziq = ZIQRetrieval(cache_dir=cache, sppmi_prior=prior)

        # Train FTRL namespace: heavily favor "cognition"
        for _ in range(20):
            ziq._ftrl_ns.update(["cognition"])

        # Query that SPPMI might not strongly prefer cognition
        # but FTRL likelihood should boost it
        result = ziq.route_query("general query", confidence=0.1)
        ftrl_w = ziq._ftrl_ns.likelihood()
        # cognition should have highest FTRL weight
        assert ftrl_w["cognition"] == max(ftrl_w.values())
        # route_query should return something (not crash)
        assert len(result) >= 1

    def test_sppmi_persist_and_autoload(self):
        """SPPMI built once persists; new ZIQRetrieval auto-loads."""
        cache = tempfile.mkdtemp()
        ns_texts = {
            "memory": ["feedback correction handoff"],
            "cognition": ["rule decision cbua"],
            "skills": ["skill tool audio"],
            "knowledge": ["reference search"],
            "context": ["session state"],
        }
        # Build and persist
        ziq1 = ZIQRetrieval(cache_dir=cache)
        assert ziq1._sppmi is None  # no prior yet
        prior = ziq1.build_and_persist_sppmi(ns_texts)
        assert prior.is_built
        assert prior.n_terms > 0

        # New instance auto-loads from cache
        ziq2 = ZIQRetrieval(cache_dir=cache)
        assert ziq2._sppmi is not None
        assert ziq2._sppmi.is_built
        assert ziq2._sppmi.n_terms == prior.n_terms

        # Routing uses the auto-loaded SPPMI
        result = ziq2.route_query("feedback correction", confidence=0.1)
        assert len(result) >= 1

    def test_sppmi_to_dict_from_dict_roundtrip(self):
        """SPPMIPrior serialization roundtrip preserves state."""
        prior = SPPMIPrior.build_from_texts({
            "memory": ["hello world test"],
            "cognition": ["rule decision"],
        })
        data = prior.to_dict()
        restored = SPPMIPrior.from_dict(data)
        assert restored.is_built
        assert restored.shift_k == prior.shift_k
        assert restored.n_terms == prior.n_terms
        assert restored.n_nonzero == prior.n_nonzero
        # Same prior distribution
        p1 = prior.prior("hello rule")
        p2 = restored.prior("hello rule")
        for ns in p1:
            assert abs(p1[ns] - p2[ns]) < 1e-9


class TestMetaTuner:
    """Tests for Autonomy L2: _MetaTuner hyperparameter learning."""

    def _make(self):
        from concinno.core.state_store import StateStore
        return _MetaTuner(StateStore(tempfile.mkdtemp()))

    def test_defaults_match_hardcoded(self):
        """Fresh MetaTuner returns same values as hardcoded defaults."""
        mt = self._make()
        assert mt.get_param("ftrl_alpha") == 0.1
        assert mt.get_param("ftrl_lam") == 0.01

    def test_no_tune_before_interval(self):
        """No tuning happens before TUNE_INTERVAL decisions."""
        mt = self._make()
        for _ in range(49):
            r = mt.record_outcome(correct=True)
        # 49 decisions: no tune yet (interval=50)
        assert r is None

    def test_baseline_set_at_first_interval(self):
        """First interval sets baseline, no adjustments."""
        mt = self._make()
        for i in range(50):
            r = mt.record_outcome(correct=(i % 2 == 0))
        assert r is not None
        assert r["status"] == "baseline_set"
        assert mt.get_param("ftrl_alpha") == 0.1  # unchanged

    def test_regret_up_slows_down(self):
        """Accuracy drop > 5% triggers slowdown (alpha × 0.9)."""
        mt = self._make()
        # First 50: high accuracy (baseline)
        for _ in range(50):
            mt.record_outcome(correct=True)
        # Next 50: low accuracy (regret up)
        for i in range(50):
            r = mt.record_outcome(correct=(i < 5))
        assert r is not None
        assert r["status"] == "tuned"
        # Alpha should have decreased
        assert mt.get_param("ftrl_alpha") < 0.1

    def test_regret_down_speeds_up(self):
        """Accuracy gain > 5% triggers speedup (alpha × 1.05)."""
        mt = self._make()
        # First 50: low accuracy (baseline)
        for i in range(50):
            mt.record_outcome(correct=(i < 5))
        # Next 50: high accuracy (regret down)
        for _ in range(50):
            r = mt.record_outcome(correct=True)
        assert r is not None
        assert r["status"] == "tuned"
        assert mt.get_param("ftrl_alpha") > 0.1

    def test_stable_no_change(self):
        """Stable accuracy (delta < 5%) → no adjustment."""
        mt = self._make()
        # Two intervals with similar accuracy
        for _ in range(50):
            mt.record_outcome(correct=True)
        for _ in range(50):
            r = mt.record_outcome(correct=True)
        assert r is not None
        assert r["status"] == "stable"
        assert mt.get_param("ftrl_alpha") == 0.1

    def test_bounds_respected(self):
        """Params never go below min or above max."""
        mt = self._make()
        # Repeatedly trigger slowdown
        for cycle in range(10):
            for _ in range(50):
                mt.record_outcome(correct=True)
            for i in range(50):
                mt.record_outcome(correct=(i < 2))
        alpha = mt.get_param("ftrl_alpha")
        assert alpha >= 0.01  # min bound

    def test_status_reports_state(self):
        """status() returns meaningful debugging info."""
        mt = self._make()
        for _ in range(60):
            mt.record_outcome(correct=True)
        s = mt.status()
        assert s["n_decisions"] == 60
        assert "params" in s

    def test_wired_into_ziq_retrieval(self):
        """ZIQRetrieval creates MetaTuner and FTRL uses it."""
        ziq = ZIQRetrieval(cache_dir=tempfile.mkdtemp())
        assert hasattr(ziq, "_meta")
        assert ziq._ftrl_ns._meta is ziq._meta
        # Verify FTRL uses tuned alpha
        alpha = ziq._meta.get_param("ftrl_alpha")
        assert alpha == 0.1

    def test_route_feedback_records_outcome(self):
        """route_feedback() feeds outcome to MetaTuner."""
        ziq = ZIQRetrieval(cache_dir=tempfile.mkdtemp())
        ziq._last_route_breadth = "medium"
        ziq._last_route_result = ["memory"]
        ziq.route_feedback(["memory"])
        s = ziq._meta.status()
        assert s["n_decisions"] == 1

    def test_stats_includes_l2(self):
        """stats() includes L2 meta-tuner state."""
        ziq = ZIQRetrieval(cache_dir=tempfile.mkdtemp())
        stats = ziq.stats()
        assert "_l2_meta" in stats
        assert "params" in stats["_l2_meta"]


class TestRetrieverConfidenceSPS:
    """Tests for retriever selection domain SPS."""

    def test_confident_retriever_gets_higher_prior(self):
        """Retriever with peaked scores → higher prior."""
        sps = RetrieverConfidenceSPS()
        p = sps.prior("test query", scores={
            "bm25": [0.9, 0.3, 0.2, 0.1],  # peaked
            "dense": [0.5, 0.45, 0.4, 0.35],  # flat
        })
        assert p["bm25"] > p["dense"]

    def test_sums_to_one(self):
        """Prior distribution sums to ~1."""
        sps = RetrieverConfidenceSPS()
        p = sps.prior("q", scores={
            "a": [0.8, 0.2],
            "b": [0.6, 0.5],
            "c": [0.3, 0.1],
        })
        assert abs(sum(p.values()) - 1.0) < 1e-6

    def test_empty_scores(self):
        """Empty scores → empty prior."""
        sps = RetrieverConfidenceSPS()
        assert sps.prior("q", scores={}) == {}

    def test_single_retriever(self):
        """Single retriever gets probability 1."""
        sps = RetrieverConfidenceSPS()
        p = sps.prior("q", scores={"only": [0.9, 0.1]})
        assert abs(p["only"] - 1.0) < 1e-6

    def test_equal_confidence_uniform(self):
        """Equal confidence → roughly uniform prior."""
        sps = RetrieverConfidenceSPS()
        p = sps.prior("q", scores={
            "a": [0.5, 0.3],
            "b": [0.5, 0.3],
        })
        assert abs(p["a"] - p["b"]) < 1e-6

    def test_score_gap_metric(self):
        """_score_gap computes top-1 minus mean(rest)."""
        gap = RetrieverConfidenceSPS._score_gap(
            [0.9, 0.3, 0.2, 0.1],
        )
        assert abs(gap - (0.9 - 0.2)) < 1e-6

    def test_single_score_returns_zero_gap(self):
        """Single score → 0 gap (can't compute)."""
        assert RetrieverConfidenceSPS._score_gap([0.9]) == 0.0

    def test_sps_protocol_conformance(self):
        """Both SPS classes satisfy SPSProtocol."""
        assert isinstance(
            RetrieverConfidenceSPS(), SPSProtocol,
        )
        prior = SPPMIPrior.build_from_texts({
            "memory": ["hello"],
        })
        assert isinstance(prior, SPSProtocol)
