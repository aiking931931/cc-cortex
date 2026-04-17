"""Tests for concinno.star — STAR (Stimulus-Triggered Agentic Retrieval) v3.5."""

from __future__ import annotations

import json
import textwrap
import time

from concinno.star import (
    MAX_WEB_RESULTS,
    NOISE_RATIO_MAX,
    OPTIMAL_RESULTS_PER_QUERY,
    PROFILE_CONFIG,
    TIER_CONFIG,
    AdaptiveForgetter,
    AdaptiveRouter,
    AssociativeIndex,
    BM25Index,
    ConfidenceGate,
    ConfidenceVerdict,
    ConfluencePath,
    ConfluenceRAG,
    ContextCompressor,
    CRAGAction,
    CRAGCorrector,
    DivergentResult,
    DivergentSearch,
    ForgetCandidate,
    FreshnessScorer,
    MultiSourceRetriever,
    QueryPlanner,
    Reranker,
    RetrievalProfile,
    RetrievalResult,
    RetrievalTier,
    SearchMode,
    SessionCache,
    SharedMemoryBus,
    SourceResult,
    SourceType,
    STAREngine,
    SubQuestion,
    create_star_engine,
)

# ── AdaptiveRouter ───────────────────────────────────────


class TestAdaptiveRouter:
    def test_forced_tier_bypasses_routing(self):
        router = AdaptiveRouter()
        assert router.route("anything", RetrievalTier.L0_INDEX) == RetrievalTier.L0_INDEX
        assert router.route("anything", RetrievalTier.L2_FULL) == RetrievalTier.L2_FULL

    def test_simple_query_routes_to_l0(self):
        router = AdaptiveRouter()
        assert router.route("what file has config") == RetrievalTier.L0_INDEX
        assert router.route("where file is deploy") == RetrievalTier.L0_INDEX

    def test_complex_query_routes_to_l2(self):
        router = AdaptiveRouter()
        assert router.route("refactor the entire hook system") == RetrievalTier.L2_FULL
        assert router.route("why does this test fail") == RetrievalTier.L2_FULL
        assert router.route("how should I architect the new API") == RetrievalTier.L2_FULL

    def test_destructive_always_l2(self):
        router = AdaptiveRouter()
        assert router.route("rm -rf old directory") == RetrievalTier.L2_FULL
        assert router.route("delete all test files") == RetrievalTier.L2_FULL
        assert router.route("force push to main") == RetrievalTier.L2_FULL

    def test_default_routes_to_l1(self):
        router = AdaptiveRouter()
        assert router.route("update the readme") == RetrievalTier.L1_SUMMARY
        assert router.route("add type hints to function") == RetrievalTier.L1_SUMMARY


# ── QueryPlanner ─────────────────────────────────────────


class TestQueryPlanner:
    def test_l0_single_location_question(self):
        planner = QueryPlanner()
        qs = planner.plan("find config", RetrievalTier.L0_INDEX)
        assert len(qs) == 1
        assert qs[0].category == "location"

    def test_l1_convention_check(self):
        planner = QueryPlanner()
        qs = planner.plan("add new feature", RetrievalTier.L1_SUMMARY)
        categories = {q.category for q in qs}
        assert "convention" in categories

    def test_l2_full_decomposition(self):
        planner = QueryPlanner()
        qs = planner.plan("refactor auth middleware", RetrievalTier.L2_FULL)
        categories = {q.category for q in qs}
        assert "convention" in categories
        assert "prerequisite" in categories
        assert "blocker" in categories
        assert "history" in categories  # refactor triggers history

    def test_destructive_adds_safety(self):
        planner = QueryPlanner()
        qs = planner.plan("rm -rf old files", RetrievalTier.L2_FULL)
        categories = {q.category for q in qs}
        assert "safety" in categories
        safety = [q for q in qs if q.category == "safety"]
        assert safety[0].priority == "critical"


# ── Reranker ─────────────────────────────────────────────


class TestReranker:
    def test_empty_candidates(self):
        r = Reranker()
        assert r.rerank("query", []) == []

    def test_keyword_rerank_improves_order(self):
        r = Reranker()
        candidates = [
            SourceResult(source=SourceType.RAG_VECTOR, text="unrelated stuff", score=0.8),
            SourceResult(source=SourceType.RAG_VECTOR, text="deploy config setup", score=0.5),
        ]
        result = r.rerank("deploy config", candidates)
        # "deploy config setup" should rank higher after rerank
        assert "deploy" in result[0].text

    def test_top_k_limits_output(self):
        r = Reranker()
        candidates = [
            SourceResult(source=SourceType.RAG_VECTOR, text=f"item {i}", score=0.5)
            for i in range(10)
        ]
        result = r.rerank("item", candidates, top_k=3)
        assert len(result) == 3

    def test_cross_encoder_semantic_rerank(self):
        """Cross-encoder should rank semantically similar text higher."""
        r = Reranker(use_cross_encoder=True)
        candidates = [
            SourceResult(
                source=SourceType.RAG_VECTOR,
                text="The cat sat on the mat",
                score=0.8,
            ),
            SourceResult(
                source=SourceType.RAG_VECTOR,
                text="How to deploy Python applications to production servers",
                score=0.5,
            ),
        ]
        result = r.rerank("deploying Python apps", candidates)
        # Cross-encoder should prefer semantic match over keyword-less high score
        assert "deploy" in result[0].text.lower() or "python" in result[0].text.lower()

    def test_cross_encoder_disabled_uses_keyword(self):
        """use_cross_encoder=False forces keyword fallback."""
        r = Reranker(use_cross_encoder=False)
        candidates = [
            SourceResult(source=SourceType.RAG_VECTOR, text="deploy config", score=0.7),
            SourceResult(source=SourceType.RAG_VECTOR, text="random noise", score=0.7),
        ]
        result = r.rerank("deploy", candidates)
        # Equal base score → keyword overlap breaks tie
        assert "deploy" in result[0].text


# ── ContextCompressor ────────────────────────────────────


class TestContextCompressor:
    def test_empty(self):
        c = ContextCompressor()
        assert c.compress([]) == []

    def test_fits_within_budget(self):
        c = ContextCompressor()
        candidates = [
            SourceResult(
                source=SourceType.KB_SKILL,
                text="Short text that fits easily within budget.",
                score=0.9,
            ),
        ]
        result = c.compress(candidates, max_tokens=100)
        assert len(result) == 1
        assert result[0].text == candidates[0].text

    def test_truncates_long_text(self):
        c = ContextCompressor()
        long_text = "This is a sentence. " * 100  # ~2000 chars
        candidates = [
            SourceResult(source=SourceType.RAG_VECTOR, text=long_text, score=0.8),
        ]
        result = c.compress(candidates, max_tokens=30)  # ~120 chars
        assert len(result) == 1
        assert len(result[0].text) < len(long_text)

    def test_multiple_candidates_budget(self):
        c = ContextCompressor()
        candidates = [
            SourceResult(source=SourceType.KB_SKILL, text="A" * 200, score=0.9),
            SourceResult(source=SourceType.RAG_VECTOR, text="B" * 200, score=0.8),
            SourceResult(source=SourceType.LEARNING, text="C" * 200, score=0.7),
        ]
        result = c.compress(candidates, max_tokens=50)  # ~200 chars
        total = sum(len(r.text) for r in result)
        assert total <= 220  # Some margin


# ── CRAGCorrector ────────────────────────────────────────


class TestCRAGCorrector:
    def test_no_candidates_incorrect(self):
        crag = CRAGCorrector()
        assert crag.evaluate("query", []) == CRAGAction.INCORRECT

    def test_high_relevance_correct(self):
        crag = CRAGCorrector()
        candidates = [
            SourceResult(
                source=SourceType.KB_SKILL,
                text="deploy config settings and hook conventions",
                score=0.9,
            ),
        ]
        assert crag.evaluate("deploy config hook", candidates) == CRAGAction.CORRECT

    def test_low_relevance_incorrect(self):
        crag = CRAGCorrector()
        candidates = [
            SourceResult(
                source=SourceType.RAG_VECTOR,
                text="quantum physics introduction chapter",
                score=0.15,
            ),
        ]
        assert crag.evaluate("deploy config", candidates) == CRAGAction.INCORRECT

    def test_medium_relevance_ambiguous(self):
        crag = CRAGCorrector()
        candidates = [
            SourceResult(
                source=SourceType.RAG_VECTOR,
                text="some config related stuff but not deploy",
                score=0.5,
            ),
        ]
        action = crag.evaluate("deploy config", candidates)
        assert action in (CRAGAction.AMBIGUOUS, CRAGAction.CORRECT)


# ── ConfidenceGate ───────────────────────────────────────


class TestConfidenceGate:
    def test_empty_skip(self):
        gate = ConfidenceGate()
        v = gate.score("q", [])
        assert v.action == "skip"
        assert v.score == 0.0

    def test_l0_cap_at_60(self):
        gate = ConfidenceGate()
        candidates = [
            SourceResult(source=SourceType.KB_SKILL, text="rule", score=0.95),
        ]
        v = gate.score("q", candidates, RetrievalTier.L0_INDEX)
        assert v.score <= 0.60

    def test_l1_cap_at_85(self):
        gate = ConfidenceGate()
        candidates = [
            SourceResult(source=SourceType.KB_SKILL, text="rule", score=0.95),
            SourceResult(source=SourceType.RIVERBED, text="deep", score=0.9, depth=50.0),
        ]
        v = gate.score("q", candidates, RetrievalTier.L1_SUMMARY)
        assert v.score <= 0.85

    def test_l2_no_cap(self):
        gate = ConfidenceGate()
        candidates = [
            SourceResult(source=SourceType.KB_SKILL, text="a", score=1.0),
            SourceResult(
                source=SourceType.RIVERBED, text="b", score=1.0, depth=100.0
            ),
            SourceResult(
                source=SourceType.LEARNING, text="c", score=1.0,
                metadata={"count": 20},
            ),
        ]
        v = gate.score("q", candidates, RetrievalTier.L2_FULL)
        assert v.score > 0.85  # Can exceed L1 cap

    def test_multi_source_bonus(self):
        gate = ConfidenceGate()
        single = [SourceResult(source=SourceType.RAG_VECTOR, text="a", score=0.6)]
        multi = [
            SourceResult(source=SourceType.RAG_VECTOR, text="a", score=0.6),
            SourceResult(source=SourceType.KB_SKILL, text="a", score=0.6),
        ]
        v1 = gate.score("q", single, RetrievalTier.L2_FULL)
        v2 = gate.score("q", multi, RetrievalTier.L2_FULL)
        assert v2.score > v1.score

    def test_emotional_charge_boosts_confidence(self):
        """Emotionally charged riverbed results get higher confidence."""
        gate = ConfidenceGate()
        neutral = [
            SourceResult(
                source=SourceType.RIVERBED, text="fact", score=0.7,
                depth=5.0, emotional_charge=0.0,
            ),
        ]
        emotional = [
            SourceResult(
                source=SourceType.RIVERBED, text="fact", score=0.7,
                depth=5.0, emotional_charge=0.9,
            ),
        ]
        v_neutral = gate.score("q", neutral, RetrievalTier.L2_FULL)
        v_emotional = gate.score("q", emotional, RetrievalTier.L2_FULL)
        assert v_emotional.score > v_neutral.score

    def test_emotional_charge_capped_at_10_percent(self):
        """Emotion bonus should not exceed 10% (0.10)."""
        gate = ConfidenceGate()
        maxed = [
            SourceResult(
                source=SourceType.RIVERBED, text="trauma", score=0.7,
                depth=5.0, emotional_charge=1.0,
            ),
        ]
        v = gate.score("q", maxed, RetrievalTier.L2_FULL)
        # With charge=1.0, emotion_bonus = min(0.10, 0.10*1.0) = 0.10
        # Verify it's bounded — compare with charge=0
        no_charge = [
            SourceResult(
                source=SourceType.RIVERBED, text="trauma", score=0.7,
                depth=5.0, emotional_charge=0.0,
            ),
        ]
        v0 = gate.score("q", no_charge, RetrievalTier.L2_FULL)
        diff = v.score - v0.score
        assert diff <= 0.11  # emotion_bonus ≤ 0.10 × freshness_factor(≤1.0)


# ── MultiSourceRetriever ────────────────────────────────


class TestMultiSourceRetriever:
    def test_no_sources_empty(self):
        r = MultiSourceRetriever(project_dir="/tmp/fake")
        sq = SubQuestion(text="test", category="convention")
        assert r.retrieve(sq, RetrievalTier.L1_SUMMARY) == []

    def test_kb_skill_l0_metadata_only(self, tmp_path):
        skill_dir = tmp_path / ".claude" / "skills" / "kb_deploy"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# Deploy\nRun deploy.py after changes.")

        r = MultiSourceRetriever(
            project_dir=str(tmp_path),
            kb_skills_dir=str(tmp_path / ".claude" / "skills"),
        )
        sq = SubQuestion(text="deploy", category="location", source_hint=SourceType.KB_SKILL)
        results = r.retrieve(sq, RetrievalTier.L0_INDEX)
        assert len(results) >= 1
        # L0 should only have metadata, not full content
        assert "Run deploy.py" not in results[0].text
        assert "kb_deploy" in results[0].text

    def test_kb_skill_l1_full_content(self, tmp_path):
        skill_dir = tmp_path / ".claude" / "skills" / "kb_hooks"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: hooks
            ---
            # Hook Convention

            Hook convention for reading files before modification.
            Convention: always verify after changes.
        """))

        r = MultiSourceRetriever(
            project_dir=str(tmp_path),
            kb_skills_dir=str(tmp_path / ".claude" / "skills"),
        )
        sq = SubQuestion(
            text="convention hook", category="convention",
            source_hint=SourceType.KB_SKILL,
        )
        results = r.retrieve(sq, RetrievalTier.L1_SUMMARY)
        assert len(results) >= 1
        assert "convention" in results[0].text.lower()

    def test_learnings_search(self, tmp_path):
        lp = tmp_path / "learnings.json"
        lp.write_text(json.dumps({
            "version": "1.0",
            "learnings": [{
                "id": "abc",
                "correction_text": "always verify changes before deploy",
                "context": "user corrected deploy without check",
                "pattern_key": "check-before-modify",
                "count": 5,
                "first_seen": "2026-01-01",
                "last_seen": "2026-03-25",
                "promoted": False,
            }],
        }))

        r = MultiSourceRetriever(
            project_dir=str(tmp_path), learnings_path=str(lp)
        )
        sq = SubQuestion(
            text="verify deploy changes", category="blocker",
            source_hint=SourceType.LEARNING,
        )
        results = r.retrieve(sq, RetrievalTier.L1_SUMMARY)
        assert len(results) >= 1
        assert "5" in results[0].text


# ── STAREngine Integration ───────────────────────────────


class TestSTAREngine:
    def test_l0_fast_path(self, tmp_path):
        skill_dir = tmp_path / ".claude" / "skills" / "kb_config"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# Config\nApp config in settings.json")

        engine = STAREngine(
            project_dir=str(tmp_path),
            kb_skills_dir=str(tmp_path / ".claude" / "skills"),
        )
        results = engine.retrieve("where config file", tier=RetrievalTier.L0_INDEX)
        # L0 should return fast with metadata
        for r in results:
            assert r.tier == RetrievalTier.L0_INDEX
            assert r.confidence <= 0.60

    def test_auto_routes_correctly(self):
        engine = STAREngine(project_dir="/tmp/fake")
        # Simple query should use L0
        results = engine.retrieve("what file has config")
        # No sources → empty, but should not crash
        assert isinstance(results, list)

    def test_l2_with_kb_and_learnings(self, tmp_path):
        # KB Skill
        skill_dir = tmp_path / ".claude" / "skills" / "kb_refactor"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(textwrap.dedent("""\
            # Refactor Convention

            Refactor convention: read before rewrite.
            Convention: always test after refactor.
            Auth system refactor must preserve backward compatibility.
            Past refactor issues: missed edge cases in entire system.
        """))

        # Learnings
        lp = tmp_path / "learnings.json"
        lp.write_text(json.dumps({
            "version": "1.0",
            "learnings": [{
                "id": "def",
                "correction_text": "refactor must preserve existing tests",
                "context": "refactored without testing",
                "pattern_key": "check-before-modify",
                "count": 4,
                "first_seen": "2026-01-01",
                "last_seen": "2026-03-25",
                "promoted": False,
            }],
        }))

        engine = STAREngine(
            project_dir=str(tmp_path),
            kb_skills_dir=str(tmp_path / ".claude" / "skills"),
            learnings_path=str(lp),
        )
        results = engine.retrieve(
            "refactor the entire auth system",
            tier=RetrievalTier.L2_FULL,
        )
        assert len(results) >= 1
        assert results[0].tier == RetrievalTier.L2_FULL

    def test_format_injection_empty(self):
        engine = STAREngine(project_dir="/tmp/fake")
        assert engine.format_injection([]) == ""

    def test_format_injection(self):
        engine = STAREngine(project_dir="/tmp/fake")
        results = [
            RetrievalResult(
                question="test?",
                answer="Always verify first",
                confidence=0.85,
                tier=RetrievalTier.L1_SUMMARY,
                sources=[SourceResult(source=SourceType.KB_SKILL, text="v", score=0.9)],
            ),
        ]
        inj = engine.format_injection(results)
        assert "STAR" in inj
        assert "85%" in inj

    def test_format_injection_skips_low(self):
        engine = STAREngine(project_dir="/tmp/fake")
        results = [
            RetrievalResult(question="?", answer="maybe", confidence=0.2),
        ]
        assert engine.format_injection(results) == ""

    def test_quick_recall(self, tmp_path):
        skill_dir = tmp_path / ".claude" / "skills" / "kb_deploy"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "# Deploy\n\nDeploy convention: run deploy.py after code changes."
        )
        engine = STAREngine(
            project_dir=str(tmp_path),
            kb_skills_dir=str(tmp_path / ".claude" / "skills"),
        )
        results = engine.quick_recall("deploy changes")
        assert isinstance(results, list)

    def test_stats(self):
        engine = STAREngine(project_dir="/tmp/fake")
        s = engine.stats()
        assert s["version"] == "3.5"
        assert "tiers" in s
        assert len(s["tiers"]) == 3
        assert s["features"]["crag_correction"] is True

    def test_riverbed_emotional_integration(self, tmp_path):
        """End-to-end: Riverbed emotional_charge flows through STAR pipeline."""
        from concinno.riverbed import RiverbedMemory, Stake

        rb_dir = tmp_path / ".concinno_cache" / "riverbed"
        rb_dir.mkdir(parents=True)

        mem = RiverbedMemory(project_dir=str(tmp_path), cache_dir=str(rb_dir))
        mem.add_stake(Stake(id="builder", label="I am a builder", mass=5.0))

        # Carve emotionally charged memory
        mem.experience(
            "Always verify deploy before push — last time broke production",
            emotional_charge=0.9,
        )
        # Carve neutral memory
        mem.experience("Config file is at settings.json", emotional_charge=0.0)
        mem.save()

        engine = STAREngine(
            project_dir=str(tmp_path),
            riverbed_mem=mem,
        )
        results = engine.retrieve("deploy verification", tier=RetrievalTier.L2_FULL)
        # Should get results (at least from riverbed)
        if results:
            # Emotional result should have higher confidence
            assert results[0].confidence > 0
            # Verify emotional_charge propagated to sources
            for r in results:
                for s in r.sources:
                    if s.source == SourceType.RIVERBED and "deploy" in s.text.lower():
                        assert s.emotional_charge != 0.0

    def test_synthesize_merges(self):
        from concinno.star import _synthesize
        result = _synthesize([
            SourceResult(
                source=SourceType.KB_SKILL,
                text="verify changes before commit", score=0.9,
            ),
            SourceResult(
                source=SourceType.LEARNING,
                text="multi-instance requires locking", score=0.8,
            ),
        ])
        assert "verify" in result
        assert "locking" in result


# ── Factory ──────────────────────────────────────────────


class TestFactory:
    def test_create_gracefully(self, tmp_path):
        engine = create_star_engine(project_dir=str(tmp_path))
        assert isinstance(engine, STAREngine)


# ── Data Structures ──────────────────────────────────────


class TestDataStructures:
    def test_tier_values(self):
        assert RetrievalTier.L0_INDEX.value == "l0_index"
        assert RetrievalTier.L2_FULL.value == "l2_full"

    def test_crag_actions(self):
        assert CRAGAction.CORRECT.value == "correct"
        assert CRAGAction.AMBIGUOUS.value == "ambiguous"
        assert CRAGAction.INCORRECT.value == "incorrect"

    def test_source_types(self):
        assert SourceType.KB_SKILL.value == "kb_skill"
        assert SourceType.RIVERBED.value == "riverbed"

    def test_retrieval_result_defaults(self):
        r = RetrievalResult(question="q", answer="a", confidence=0.8)
        assert r.tier == RetrievalTier.AUTO
        assert r.sources == []
        assert r.crag_action == CRAGAction.CORRECT
        assert r.tokens_used == 0

    def test_confidence_verdict(self):
        v = ConfidenceVerdict(score=0.85, action="accept", reason="good")
        assert v.tier == RetrievalTier.AUTO

    def test_source_result_v3_fields(self):
        """v3: SourceResult has timestamp and freshness."""
        r = SourceResult(
            source=SourceType.KB_SKILL, text="test", score=0.8,
            timestamp=1000000.0, freshness=0.9,
        )
        assert r.timestamp == 1000000.0
        assert r.freshness == 0.9

    def test_web_search_source_type(self):
        """v3: WEB_SEARCH is a valid source type."""
        assert SourceType.WEB_SEARCH.value == "web_search"


# ── FreshnessScorer (v3: Patent #9) ──────────────────────


class TestFreshnessScorer:
    def test_fresh_item_scores_high(self):
        scorer = FreshnessScorer()
        now = time.time()
        score = scorer.score(now - 3600, access_count=0, now=now)  # 1 hour ago
        assert score > 0.9

    def test_old_item_scores_low(self):
        scorer = FreshnessScorer()
        now = time.time()
        # 30 days ago
        score = scorer.score(now - 30 * 86400, access_count=0, now=now)
        assert score < 0.3

    def test_zero_timestamp_returns_min(self):
        scorer = FreshnessScorer()
        assert scorer.score(0) == 0.1  # FRESHNESS_MIN

    def test_invalid_timestamp_returns_min(self):
        scorer = FreshnessScorer()
        assert scorer.score("not-a-number") == 0.1

    def test_access_reinforcement_slows_decay(self):
        """SM-2 inspired: more accesses = slower decay."""
        scorer = FreshnessScorer()
        now = time.time()
        age = now - 14 * 86400  # 14 days ago
        score_no_access = scorer.score(age, access_count=0, now=now)
        score_many_access = scorer.score(age, access_count=10, now=now)
        assert score_many_access > score_no_access

    def test_apply_to_results(self):
        scorer = FreshnessScorer()
        now = time.time()
        candidates = [
            SourceResult(
                source=SourceType.KB_SKILL, text="a", score=0.8,
                timestamp=now - 3600,
            ),
            SourceResult(
                source=SourceType.RAG_VECTOR, text="b", score=0.6,
                timestamp=now - 30 * 86400,
            ),
        ]
        scorer.apply_to_results(candidates, now=now)
        assert candidates[0].freshness > candidates[1].freshness

    def test_future_timestamp_clamps_to_one(self):
        scorer = FreshnessScorer()
        now = time.time()
        score = scorer.score(now + 3600, now=now)  # Future
        assert score == 1.0


# ── AssociativeIndex (v3: Patent #11) ─────────────────────


class TestAssociativeIndex:
    def test_record_and_expand(self):
        idx = AssociativeIndex()
        r1 = SourceResult(
            source=SourceType.KB_SKILL, text="hook conventions",
            score=0.9, file="kb_hooks/SKILL.md", heading="kb_hooks",
        )
        r2 = SourceResult(
            source=SourceType.LEARNING, text="hook error patterns",
            score=0.7, file="learnings.json", heading="hooks",
        )
        # Record co-occurrence
        idx.record_cooccurrence([r1, r2])

        # Now query with only r1, should suggest r2
        all_known = {AssociativeIndex._key(r2): r2}
        expanded = idx.expand([r1], all_known)
        assert len(expanded) == 1
        assert expanded[0].text == "hook error patterns"

    def test_no_self_expansion(self):
        """Candidates themselves should not appear in expansion."""
        idx = AssociativeIndex()
        r1 = SourceResult(
            source=SourceType.KB_SKILL, text="a", score=0.9,
            file="a.md", heading="a",
        )
        r2 = SourceResult(
            source=SourceType.LEARNING, text="b", score=0.7,
            file="b.md", heading="b",
        )
        idx.record_cooccurrence([r1, r2])
        all_known = {
            AssociativeIndex._key(r1): r1,
            AssociativeIndex._key(r2): r2,
        }
        expanded = idx.expand([r1, r2], all_known)
        assert len(expanded) == 0  # Both already in candidates

    def test_decay_prunes_weak_links(self):
        idx = AssociativeIndex()
        r1 = SourceResult(
            source=SourceType.KB_SKILL, text="a", score=0.9,
            file="a.md", heading="a",
        )
        r2 = SourceResult(
            source=SourceType.LEARNING, text="b", score=0.7,
            file="b.md", heading="b",
        )
        idx.record_cooccurrence([r1, r2])
        assert idx.size() == 2

        # Decay many times until pruned
        for _ in range(50):
            idx.decay_all(factor=0.5)
        assert idx.size() == 0  # All pruned

    def test_serialization_roundtrip(self):
        idx = AssociativeIndex()
        r1 = SourceResult(
            source=SourceType.KB_SKILL, text="a", score=0.9,
            file="a.md", heading="a",
        )
        r2 = SourceResult(
            source=SourceType.LEARNING, text="b", score=0.7,
            file="b.md", heading="b",
        )
        idx.record_cooccurrence([r1, r2])
        data = idx.to_dict()

        idx2 = AssociativeIndex()
        idx2.load_dict(data)
        assert idx2.size() == idx.size()

    def test_empty_expand(self):
        idx = AssociativeIndex()
        expanded = idx.expand([], {})
        assert expanded == []


# ── SessionCache (v3: Patent #13) ────────────────────────


class TestSessionCache:
    def test_put_and_get(self):
        cache = SessionCache()
        r = RetrievalResult(
            question="how to deploy",
            answer="use deploy.py",
            confidence=0.85,
            tier=RetrievalTier.L1_SUMMARY,
        )
        cache.put(r)
        hit = cache.get("how to deploy", RetrievalTier.L1_SUMMARY)
        assert hit is not None
        assert hit[0].answer == "use deploy.py"

    def test_miss_returns_none(self):
        cache = SessionCache()
        assert cache.get("unknown query", RetrievalTier.L0_INDEX) is None

    def test_different_tier_is_different_key(self):
        cache = SessionCache()
        r = RetrievalResult(
            question="test", answer="a", confidence=0.8,
            tier=RetrievalTier.L0_INDEX,
        )
        cache.put(r)
        assert cache.get("test", RetrievalTier.L0_INDEX) is not None
        assert cache.get("test", RetrievalTier.L2_FULL) is None

    def test_eviction_at_max_size(self):
        cache = SessionCache(max_size=3)
        for i in range(5):
            cache.put(RetrievalResult(
                question=f"q{i}", answer=f"a{i}", confidence=0.8,
                tier=RetrievalTier.L1_SUMMARY,
            ))
        stats = cache.stats()
        assert stats["cached"] == 3  # Oldest evicted

    def test_stats(self):
        cache = SessionCache()
        cache.get("miss", RetrievalTier.L0_INDEX)
        r = RetrievalResult(
            question="hit", answer="a", confidence=0.8,
            tier=RetrievalTier.L1_SUMMARY,
        )
        cache.put(r)
        cache.get("hit", RetrievalTier.L1_SUMMARY)
        stats = cache.stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert stats["hit_rate"] == 0.5

    def test_clear(self):
        cache = SessionCache()
        cache.put(RetrievalResult(
            question="x", answer="y", confidence=0.8,
            tier=RetrievalTier.L1_SUMMARY,
        ))
        cache.clear()
        assert cache.stats()["cached"] == 0


# ── SharedMemoryBus (v3: Patent #12) ─────────────────────


class TestSharedMemoryBus:
    def test_publish_and_consume(self, tmp_path):
        bus = SharedMemoryBus(
            cache_dir=str(tmp_path), session_id="test_session",
        )
        results = [RetrievalResult(
            question="deploy steps",
            answer="run deploy script then verify",
            confidence=0.85,
            tier=RetrievalTier.L1_SUMMARY,
        )]
        written = bus.publish(results)
        assert written == 1

        # query="" to skip relevance filter, get all
        consumed = bus.consume(query="")
        assert len(consumed) >= 1
        assert "deploy" in consumed[0].text

    def test_low_confidence_not_shared(self, tmp_path):
        bus = SharedMemoryBus(
            cache_dir=str(tmp_path), session_id="test",
        )
        results = [RetrievalResult(
            question="maybe", answer="unsure",
            confidence=0.3, tier=RetrievalTier.L0_INDEX,
        )]
        written = bus.publish(results)
        assert written == 0  # Below CONFIDENCE_CAUTION

    def test_ttl_expiry(self, tmp_path):
        bus = SharedMemoryBus(
            cache_dir=str(tmp_path), session_id="test",
        )
        results = [RetrievalResult(
            question="old", answer="stale data",
            confidence=0.9, tier=RetrievalTier.L1_SUMMARY,
        )]
        bus.publish(results)
        # Consume with 0 max_age → everything expired
        consumed = bus.consume(query="old", max_age=0)
        assert len(consumed) == 0

    def test_cleanup(self, tmp_path):
        bus = SharedMemoryBus(
            cache_dir=str(tmp_path), session_id="test",
        )
        bus.publish([RetrievalResult(
            question="x", answer="y", confidence=0.8,
            tier=RetrievalTier.L1_SUMMARY,
        )])
        # Verify shared dir has file, then cleanup with -1 max_age
        import os
        shared_dir = os.path.join(str(tmp_path), "shared")
        assert len(os.listdir(shared_dir)) >= 1
        removed = bus.cleanup(max_age=-1)
        assert removed >= 1

    def test_no_cache_dir_noop(self):
        bus = SharedMemoryBus(cache_dir="", session_id="test")
        assert bus.publish([]) == 0
        assert bus.consume() == []


# ── STAREngine v3 Integration ─────────────────────────────


class TestSTAREngineV3:
    def test_session_cache_hit(self):
        """Second identical query should hit session cache."""
        engine = STAREngine(project_dir="/tmp/fake_cache_test")
        # Manually seed cache to test cache hit logic
        engine._session_cache.put(RetrievalResult(
            question="convention for hook",
            answer="use hook pattern",
            confidence=0.85,
            tier=RetrievalTier.L1_SUMMARY,
        ))
        # This call should hit cache
        results = engine.retrieve("convention for hook")
        stats = engine.stats()
        assert stats["session_cache"]["hits"] >= 1
        assert len(results) >= 1
        assert results[0].answer == "use hook pattern"

    def test_freshness_in_stats(self):
        engine = STAREngine(project_dir="/tmp/fake")
        s = engine.stats()
        assert s["features"]["temporal_freshness"] is True
        assert s["features"]["associative_expansion"] is True
        assert s["features"]["session_cache"] is True
        assert s["features"]["multi_agent_sharing"] is True

    def test_web_fallback_in_l2(self, tmp_path):
        """When internal sources fail, web fallback is attempted."""
        def mock_web(query):
            return [{"text": "web result for " + query, "score": 0.6}]

        engine = STAREngine(
            project_dir=str(tmp_path),
            web_search_fn=mock_web,
        )
        # L2 with no internal sources → should try web
        results = engine.retrieve(
            "why does deploy fail", tier=RetrievalTier.L2_FULL,
        )
        # Web results may or may not pass confidence gate,
        # but the pipeline shouldn't crash
        assert isinstance(results, list)

    def test_share_publishes_to_bus(self, tmp_path):
        """share() writes session cache to shared bus."""
        engine = STAREngine(
            project_dir=str(tmp_path),
            cache_dir=str(tmp_path / "cache"),
        )
        # Manually put something in session cache
        engine._session_cache.put(RetrievalResult(
            question="test q", answer="test a",
            confidence=0.85, tier=RetrievalTier.L1_SUMMARY,
        ))
        count = engine.share()
        assert count >= 1


# ── AdaptiveForgetter (v3: Patent #14 Adaptive Forgetting) ──────────


class TestAdaptiveForgetter:
    def test_crag_rejected_highest_priority(self):
        """CRAG-rejected items have highest forget score."""
        forgetter = AdaptiveForgetter()
        r1 = SourceResult(
            source=SourceType.RAG_VECTOR, text="bad result",
            score=0.5, file="bad.py", heading="bad",
        )
        key = AssociativeIndex._key(r1)
        forgetter.mark_rejected(key)
        candidates = forgetter.evaluate([r1])
        assert len(candidates) == 1
        assert candidates[0].forget_score == 0.95
        assert "crag_rejected" in candidates[0].reason

    def test_superseded_high_priority(self):
        forgetter = AdaptiveForgetter()
        r1 = SourceResult(
            source=SourceType.RAG_VECTOR, text="old info",
            score=0.6, file="old.py", heading="old",
        )
        key = AssociativeIndex._key(r1)
        forgetter.mark_superseded(key, "new_key_abc")
        candidates = forgetter.evaluate([r1])
        assert len(candidates) == 1
        assert candidates[0].forget_score == 0.85

    def test_stale_unused_forgotten(self):
        forgetter = AdaptiveForgetter(stale_days=7)
        now = time.time()
        r1 = SourceResult(
            source=SourceType.RAG_VECTOR, text="old unused",
            score=0.5, file="old.py", heading="old",
            timestamp=now - 30 * 86400,  # 30 days ago
            metadata={"access_count": 0},
        )
        candidates = forgetter.evaluate([r1], now=now)
        assert len(candidates) == 1
        assert "stale_unused" in candidates[0].reason
        assert candidates[0].forget_score > 0.3

    def test_low_confidence_forgotten(self):
        forgetter = AdaptiveForgetter()
        now = time.time()
        r1 = SourceResult(
            source=SourceType.RAG_VECTOR, text="uncertain",
            score=0.1, file="x.py", heading="x",
            timestamp=now - 3600,  # Recent but low score
        )
        candidates = forgetter.evaluate([r1], now=now)
        assert len(candidates) == 1
        assert candidates[0].forget_score == 0.5

    def test_kb_skill_protected(self):
        """High-quality KB Skills are never forgotten."""
        forgetter = AdaptiveForgetter()
        r1 = SourceResult(
            source=SourceType.KB_SKILL, text="important rule",
            score=0.9, file="kb_core/SKILL.md", heading="kb_core",
        )
        candidates = forgetter.evaluate([r1])
        assert len(candidates) == 0  # Protected

    def test_select_to_forget_respects_target(self):
        forgetter = AdaptiveForgetter(stale_days=1)
        now = time.time()
        items = [
            SourceResult(
                source=SourceType.RAG_VECTOR,
                text=f"item_{i}", score=0.3,
                file=f"f{i}.py", heading=f"h{i}",
                timestamp=now - 10 * 86400,
                metadata={"access_count": 0},
            )
            for i in range(10)
        ]
        keys = forgetter.select_to_forget(items, target_size=5, now=now)
        assert len(keys) == 5  # Remove 5 to reach target of 5

    def test_fresh_items_kept(self):
        """Recent items with decent scores should not be forgotten."""
        forgetter = AdaptiveForgetter()
        now = time.time()
        r1 = SourceResult(
            source=SourceType.RAG_VECTOR, text="fresh info",
            score=0.7, file="new.py", heading="new",
            timestamp=now - 3600,  # 1 hour ago
        )
        candidates = forgetter.evaluate([r1], now=now)
        assert len(candidates) == 0  # Keep

    def test_stats(self):
        forgetter = AdaptiveForgetter()
        forgetter.mark_rejected("key1")
        forgetter.mark_superseded("key2", "key3")
        s = forgetter.stats()
        assert s["rejected_keys"] == 1
        assert s["superseded_keys"] == 1

    def test_forget_candidate_dataclass(self):
        fc = ForgetCandidate(
            key="test_key", forget_score=0.75, reason="stale",
        )
        assert fc.key == "test_key"
        assert fc.forget_score == 0.75


# ── v3.5 Research-Hardened Limits (Liu 2023, Cuconasu 2024) ──


class TestResearchBackedConstants:
    """Verify research-backed constants are correctly set."""

    def test_optimal_results_is_3(self):
        """Liu 2023: 3 precise docs > 5 mixed. Sweet spot."""
        assert OPTIMAL_RESULTS_PER_QUERY == 3

    def test_max_web_results_capped(self):
        """Hard ceiling on external search results."""
        assert MAX_WEB_RESULTS == 3

    def test_noise_ratio_max(self):
        """Cuconasu 2024: >30% noise = actively harmful."""
        assert NOISE_RATIO_MAX == 0.30

    def test_l2_max_sources_is_3(self):
        """L2 full: 3 precise > 5 mixed (research-backed)."""
        config = TIER_CONFIG[RetrievalTier.L2_FULL]
        assert config["max_sources"] == 3

    def test_l1_max_sources_is_2(self):
        config = TIER_CONFIG[RetrievalTier.L1_SUMMARY]
        assert config["max_sources"] == 2

    def test_l0_max_sources_is_1(self):
        config = TIER_CONFIG[RetrievalTier.L0_INDEX]
        assert config["max_sources"] == 1


class TestNoiseGuard:
    """Test the noise ratio guard (Cuconasu 2024 hardening)."""

    def test_no_web_passes_through(self):
        """All internal sources pass unmodified."""
        candidates = [
            SourceResult(source=SourceType.KB_SKILL, text="a", score=0.9),
            SourceResult(source=SourceType.LEARNING, text="b", score=0.8),
            SourceResult(source=SourceType.RIVERBED, text="c", score=0.7),
        ]
        result = STAREngine._apply_noise_guard(candidates)
        assert len(result) == 3

    def test_web_within_ratio_passes(self):
        """Web results within 30% noise ratio pass."""
        candidates = [
            SourceResult(source=SourceType.KB_SKILL, text="a", score=0.9),
            SourceResult(source=SourceType.KB_SKILL, text="b", score=0.8),
            SourceResult(source=SourceType.KB_SKILL, text="c", score=0.7),
            SourceResult(source=SourceType.WEB_SEARCH, text="w", score=0.5),
        ]
        result = STAREngine._apply_noise_guard(candidates)
        # 1 web / 4 total = 25% < 30% → passes
        assert len(result) == 4

    def test_excess_web_trimmed(self):
        """Web results exceeding 30% noise ratio get trimmed."""
        candidates = [
            SourceResult(source=SourceType.KB_SKILL, text="a", score=0.9),
            SourceResult(source=SourceType.KB_SKILL, text="b", score=0.85),
            SourceResult(source=SourceType.KB_SKILL, text="c", score=0.8),
            SourceResult(source=SourceType.WEB_SEARCH, text="w1", score=0.6),
            SourceResult(source=SourceType.WEB_SEARCH, text="w2", score=0.5),
            SourceResult(source=SourceType.WEB_SEARCH, text="w3", score=0.4),
        ]
        result = STAREngine._apply_noise_guard(candidates)
        # 3 internal → max_web = floor(3*0.30/0.70) = 1. Best web kept.
        web_count = sum(1 for r in result if r.source == SourceType.WEB_SEARCH)
        assert web_count == 1
        web_result = [r for r in result if r.source == SourceType.WEB_SEARCH][0]
        assert web_result.score == 0.6

    def test_empty_candidates_safe(self):
        assert STAREngine._apply_noise_guard([]) == []

    def test_all_web_returns_empty(self):
        """100% web with 0 internal → returns empty (Cuconasu 2024: noise > nothing)."""
        candidates = [
            SourceResult(source=SourceType.WEB_SEARCH, text="w1", score=0.6),
            SourceResult(source=SourceType.WEB_SEARCH, text="w2", score=0.5),
        ]
        result = STAREngine._apply_noise_guard(candidates)
        assert len(result) == 0


class TestWebSearchHardLimit:
    """Test MAX_WEB_RESULTS hard ceiling in _query_web."""

    def test_web_results_capped(self):
        """Even if web_search_fn returns 10, we cap to MAX_WEB_RESULTS."""
        def mock_web(q):
            return [{"text": f"result {i}", "score": 0.5} for i in range(10)]

        retriever = MultiSourceRetriever(web_search_fn=mock_web)
        results = retriever._query_web("test", max_results=10)
        assert len(results) <= MAX_WEB_RESULTS

    def test_web_results_respect_lower_max(self):
        """If caller asks for fewer than MAX_WEB_RESULTS, respect that."""
        def mock_web(q):
            return [{"text": f"result {i}", "score": 0.5} for i in range(10)]

        retriever = MultiSourceRetriever(web_search_fn=mock_web)
        results = retriever._query_web("test", max_results=1)
        assert len(results) == 1


# ── DivergentSearch (v3.5: Patent #16 Map-Reduce) ────────


class TestSearchMode:
    def test_convergent_is_default(self):
        assert SearchMode.CONVERGENT == "convergent"

    def test_divergent_exists(self):
        assert SearchMode.DIVERGENT == "divergent"


class TestDivergentSearch:
    def test_decompose_facets_basic(self):
        engine = STAREngine(project_dir="/tmp/fake")
        ds = DivergentSearch(engine)
        facets = ds.decompose_facets("AI technologies overview")
        assert len(facets) >= 3
        assert all("AI technologies" in f for f in facets)

    def test_decompose_comprehensive_gets_more_facets(self):
        engine = STAREngine(project_dir="/tmp/fake")
        ds = DivergentSearch(engine)
        facets = ds.decompose_facets("list all AI techniques comprehensive")
        assert len(facets) >= 5  # Comprehensive trigger adds more

    def test_deduplicate_removes_overlapping(self):
        results = [
            RetrievalResult(
                question="a", answer="deep learning neural networks",
                confidence=0.9, tier=RetrievalTier.L2_FULL,
            ),
            RetrievalResult(
                question="b", answer="deep learning neural networks training",
                confidence=0.7, tier=RetrievalTier.L2_FULL,
            ),
            RetrievalResult(
                question="c", answer="robotics control systems",
                confidence=0.8, tier=RetrievalTier.L2_FULL,
            ),
        ]
        deduped = DivergentSearch._deduplicate(results)
        # First two overlap >60%, should merge to 2
        assert len(deduped) == 2

    def test_estimate_coverage(self):
        facets = [
            "AI — NLP and language models",
            "AI — computer vision",
            "AI — robotics",
        ]
        results = [
            RetrievalResult(
                question="nlp", answer="NLP language models transformers",
                confidence=0.8, tier=RetrievalTier.L2_FULL,
            ),
            RetrievalResult(
                question="cv", answer="computer vision object detection",
                confidence=0.8, tier=RetrievalTier.L2_FULL,
            ),
        ]
        coverage = DivergentSearch._estimate_coverage(facets, results)
        # 2/3 facets covered
        assert 0.5 <= coverage <= 0.8

    def test_detect_gaps(self):
        facets = [
            "AI — NLP",
            "AI — robotics",
        ]
        results = [
            RetrievalResult(
                question="nlp", answer="NLP language processing",
                confidence=0.8, tier=RetrievalTier.L2_FULL,
            ),
        ]
        gaps = DivergentSearch._detect_gaps(facets, results)
        assert len(gaps) >= 1
        assert any("robotics" in g.lower() for g in gaps)

    def test_divergent_search_runs_without_crash(self, tmp_path):
        """Integration: divergent search pipeline doesn't crash."""
        engine = STAREngine(project_dir=str(tmp_path))
        result = engine.divergent_search("all AI technologies")
        assert isinstance(result, DivergentResult)
        assert isinstance(result.merged, list)
        assert 0.0 <= result.coverage <= 1.0

    def test_empty_results_safe(self):
        assert DivergentSearch._deduplicate([]) == []
        assert DivergentSearch._estimate_coverage([], []) == 0.0
        assert DivergentSearch._detect_gaps([], []) == []


# ── Fix Verification Tests (Logic Self-Consistency) ──────────


class TestPositionAwareRanking:
    """#7: format_injection places strongest at primacy+recency positions."""

    def test_three_results_reordered(self, tmp_path):
        """Best→first, second-best→last, weakest→middle (Liu 2023)."""
        engine = STAREngine(project_dir=str(tmp_path))
        results = [
            RetrievalResult(
                question="q", answer="A-best", confidence=0.95,
                tier=RetrievalTier.L2_FULL,
            ),
            RetrievalResult(
                question="q", answer="B-second", confidence=0.85,
                tier=RetrievalTier.L2_FULL,
            ),
            RetrievalResult(
                question="q", answer="C-weakest", confidence=0.70,
                tier=RetrievalTier.L2_FULL,
            ),
        ]
        output = engine.format_injection(results)
        lines = output.strip().split("\n")
        # Line 0 = header, Lines 1-3 = results
        assert "A-best" in lines[1]      # primacy position
        assert "C-weakest" in lines[2]   # middle (worst position)
        assert "B-second" in lines[3]    # recency position

    def test_two_results_no_reorder(self, tmp_path):
        """< 3 results: no reordering needed, just sort by confidence."""
        engine = STAREngine(project_dir=str(tmp_path))
        results = [
            RetrievalResult(
                question="q", answer="low", confidence=0.60,
                tier=RetrievalTier.L1_SUMMARY,
            ),
            RetrievalResult(
                question="q", answer="high", confidence=0.90,
                tier=RetrievalTier.L1_SUMMARY,
            ),
        ]
        output = engine.format_injection(results)
        lines = output.strip().split("\n")
        assert "high" in lines[1]
        assert "low" in lines[2]


class TestNoiseGuardStrict:
    """#2: Strict 30% noise enforcement."""

    def test_mixed_respects_30_percent(self):
        """3 internal + 3 web → max 1 web (int(6*0.30)=1)."""
        candidates = [
            SourceResult(source=SourceType.KB_SKILL, text="i1", score=0.9),
            SourceResult(source=SourceType.KB_SKILL, text="i2", score=0.8),
            SourceResult(source=SourceType.KB_SKILL, text="i3", score=0.7),
            SourceResult(source=SourceType.WEB_SEARCH, text="w1", score=0.85),
            SourceResult(source=SourceType.WEB_SEARCH, text="w2", score=0.6),
            SourceResult(source=SourceType.WEB_SEARCH, text="w3", score=0.5),
        ]
        result = STAREngine._apply_noise_guard(candidates)
        web_count = sum(1 for r in result if r.source == SourceType.WEB_SEARCH)
        assert web_count <= 1


class TestRouterCrossCheck:
    """#6: Router doesn't misclassify complex queries as L0."""

    def test_what_file_not_l0(self):
        """'what file handles authentication' is complex despite 'what' prefix."""
        router = AdaptiveRouter()
        tier = router.route("what file handles the authentication middleware")
        assert tier != RetrievalTier.L0_INDEX

    def test_short_what_file_stays_l0(self):
        """'what file config' (3 words) is genuinely simple → L0."""
        router = AdaptiveRouter()
        tier = router.route("what file config")
        assert tier == RetrievalTier.L0_INDEX

    def test_boundary_five_words_l0(self):
        """Exactly 5 words → still L0 (boundary)."""
        router = AdaptiveRouter()
        tier = router.route("what file has the config")
        assert tier == RetrievalTier.L0_INDEX


class TestTopResultsBoundary:
    """#1 R2: DivergentResult.top_results boundary cases."""

    def test_zero_results(self):
        dr = DivergentResult(branches=[], merged=[], coverage=0.0, gaps=[])
        assert dr.top_results == []

    def test_one_result(self):
        r = RetrievalResult(question="q", answer="a", confidence=0.8,
                            tier=RetrievalTier.L1_SUMMARY)
        dr = DivergentResult(branches=[], merged=[r], coverage=0.5, gaps=[])
        assert len(dr.top_results) == 1

    def test_many_results_capped(self):
        results = [
            RetrievalResult(question="q", answer=f"a{i}", confidence=0.5 + i*0.01,
                            tier=RetrievalTier.L2_FULL)
            for i in range(20)
        ]
        dr = DivergentResult(branches=[], merged=results, coverage=0.8, gaps=[])
        assert len(dr.top_results) == OPTIMAL_RESULTS_PER_QUERY  # 3


class TestNoiseGuardBoundary:
    """#2 R2: NoiseGuard boundary stress."""

    def test_one_internal_many_web(self):
        """1 internal + 99 web → max_web = floor(1*0.30/0.70) = 0."""
        candidates = [SourceResult(source=SourceType.KB_SKILL, text="i", score=0.9)]
        candidates += [
            SourceResult(source=SourceType.WEB_SEARCH, text=f"w{i}", score=0.5)
            for i in range(99)
        ]
        result = STAREngine._apply_noise_guard(candidates)
        web_count = sum(1 for r in result if r.source == SourceType.WEB_SEARCH)
        # 1 internal can support 0 web (30% of 1 internal ≈ 0)
        assert web_count == 0
        assert len(result) == 1  # only the internal result

    def test_three_internal_allows_one_web(self):
        """3 internal → max_web = floor(3*0.30/0.70) = 1."""
        candidates = [
            SourceResult(source=SourceType.KB_SKILL, text=f"i{i}", score=0.8)
            for i in range(3)
        ]
        candidates += [
            SourceResult(source=SourceType.WEB_SEARCH, text=f"w{i}", score=0.7)
            for i in range(5)
        ]
        result = STAREngine._apply_noise_guard(candidates)
        web_count = sum(1 for r in result if r.source == SourceType.WEB_SEARCH)
        total = len(result)
        assert web_count <= 1
        assert web_count / total <= 0.30 + 0.01  # strict 30%

    def test_all_internal_no_web(self):
        """Pure internal → all kept, no web trimming needed."""
        candidates = [
            SourceResult(source=SourceType.KB_SKILL, text=f"i{i}", score=0.8)
            for i in range(5)
        ]
        result = STAREngine._apply_noise_guard(candidates)
        assert len(result) == 5


# ── BM25 Hybrid Search ─────────────────────────────────


class TestBM25Index:
    """BM25 sparse retrieval index — lexical matching complement to dense vectors."""

    def test_build_and_query(self):
        """Basic build + query returns relevant results."""
        idx = BM25Index()
        docs = _bm25_corpus()
        idx.build(docs)
        assert idx.is_ready

        results = idx.query("deploy nginx config", top_k=3)
        assert len(results) >= 1
        assert results[0].source == SourceType.BM25
        assert results[0].score > 0
        assert "deploy" in results[0].file

    def test_empty_index_returns_empty(self):
        """No docs → no results."""
        idx = BM25Index()
        assert not idx.is_ready
        assert idx.query("anything") == []

    def test_no_match_returns_empty(self):
        """Query with zero lexical overlap → empty results."""
        idx = BM25Index()
        idx.build([{"text": "alpha beta gamma", "file": "a.md", "heading": "a"}])
        results = idx.query("xylophone zebra")
        assert results == []

    def test_score_normalization(self):
        """Scores normalized to 0-1 range."""
        idx = BM25Index()
        docs = [
            {"text": "python deploy script automation", "file": "d.md", "heading": "d"},
            {"text": "javascript react frontend component", "file": "f.md", "heading": "f"},
        ]
        idx.build(docs)
        results = idx.query("python deploy", top_k=2)
        for r in results:
            assert 0 <= r.score <= 1.0

    def test_top_k_limit(self):
        """top_k caps result count."""
        idx = BM25Index()
        docs = [
            {"text": f"document number {i} about testing", "file": f"{i}.md", "heading": f"d{i}"}
            for i in range(10)
        ]
        idx.build(docs)
        results = idx.query("document testing", top_k=3)
        assert len(results) <= 3

    def test_text_truncated_to_500(self):
        """Long documents truncated to 500 chars in results."""
        idx = BM25Index()
        # BM25 IDF needs multiple docs with distinct vocab
        long_text = "deploy nginx " + "extra " * 200
        assert len(long_text) > 500
        idx.build([
            {
                "text": long_text,
                "file": "long.md",
                "heading": "long",
            },
            {
                "text": "audio stt tts voice parameters",
                "file": "audio.md",
                "heading": "audio",
            },
            {
                "text": "cognition cbua framework routing",
                "file": "cog.md",
                "heading": "cog",
            },
        ])
        results = idx.query("deploy nginx")
        assert len(results) >= 1
        assert len(results[0].text) <= 500


def _bm25_corpus():
    """Multi-doc corpus for BM25 (IDF needs >=2 docs)."""
    return [
        {
            "text": "deploy nginx reverse proxy config server",
            "file": "deploy.md",
            "heading": "deploy",
        },
        {
            "text": "audio parameters stt tts voice recognition",
            "file": "audio.md",
            "heading": "audio",
        },
        {
            "text": "cognition cbua thinking framework routing",
            "file": "cog.md",
            "heading": "cog",
        },
        {
            "text": "authentication oauth2 token refresh flow",
            "file": "auth.md",
            "heading": "auth",
        },
    ]


class TestBM25InRetriever:
    """BM25 wired into MultiSourceRetriever dispatch."""

    def test_bm25_dispatch(self):
        """_query_source dispatches BM25 correctly."""
        idx = BM25Index()
        idx.build(_bm25_corpus())
        retriever = MultiSourceRetriever(bm25_index=idx)
        results = retriever._query_source(
            SourceType.BM25,
            "deploy nginx",
            3,
            RetrievalTier.L1_SUMMARY,
        )
        assert len(results) >= 1
        assert results[0].source == SourceType.BM25

    def test_bm25_none_returns_empty(self):
        """No BM25 index -> empty results."""
        retriever = MultiSourceRetriever(bm25_index=None)
        results = retriever._query_source(
            SourceType.BM25,
            "anything",
            3,
            RetrievalTier.L1_SUMMARY,
        )
        assert results == []

    def test_bm25_l0_metadata_only(self):
        """L0 tier -> text becomes file + heading metadata."""
        idx = BM25Index()
        idx.build(_bm25_corpus())
        retriever = MultiSourceRetriever(bm25_index=idx)
        results = retriever._query_source(
            SourceType.BM25,
            "deploy config",
            3,
            RetrievalTier.L0_INDEX,
        )
        assert len(results) >= 1
        assert "§" in results[0].text
        assert "deploy.md" in results[0].text

    def test_bm25_strips_planner_prefix(self):
        """Planner prefix stripped before BM25 query."""
        idx = BM25Index()
        idx.build(_bm25_corpus())
        retriever = MultiSourceRetriever(bm25_index=idx)
        results = retriever._query_source(
            SourceType.BM25,
            "Conventions and patterns for: authentication oauth2",
            3,
            RetrievalTier.L1_SUMMARY,
        )
        assert len(results) >= 1
        assert "auth" in results[0].file


# ── Confluence RAG ──────────────────────────────────────


class TestConfluenceDecompose:
    """Query decomposition into independent retrieval angles."""

    def test_simple_query_gets_multiple_angles(self):
        c = ConfluenceRAG()
        angles = c.decompose("how does auth affect deploy")
        assert len(angles) >= 2
        # Original query always included
        assert "how does auth affect deploy" in angles

    def test_connector_split(self):
        """Connectors like 'and' split into facets."""
        c = ConfluenceRAG()
        angles = c.decompose(
            "auth tokens and deploy pipeline"
        )
        assert len(angles) >= 2

    def test_short_query_pads_with_keywords(self):
        """Short queries get keyword probes."""
        c = ConfluenceRAG()
        angles = c.decompose("nginx deploy")
        assert len(angles) >= 2

    def test_max_5_angles(self):
        c = ConfluenceRAG()
        long_q = " and ".join(f"topic{i}" for i in range(10))
        angles = c.decompose(long_q)
        assert len(angles) <= 5

    def test_dedup_preserves_order(self):
        c = ConfluenceRAG()
        angles = c.decompose("deploy deploy deploy")
        # Should not have duplicates
        assert len(angles) == len(set(a.lower() for a in angles))


class TestConfluenceFindConvergence:
    """Convergence detection from independent paths."""

    def _make_result(self, f, h, score=0.8):
        return SourceResult(
            source=SourceType.KB_SKILL,
            text=f"content of {f}",
            score=score,
            file=f,
            heading=h,
        )

    def test_two_paths_converge(self):
        """Two paths hitting same doc = convergence."""
        c = ConfluenceRAG()
        paths = [
            ConfluencePath(
                query="auth tokens",
                results=[
                    self._make_result("shared.md", "shared", 0.9),
                    self._make_result("auth.md", "auth", 0.7),
                ],
            ),
            ConfluencePath(
                query="deploy config",
                results=[
                    self._make_result("shared.md", "shared", 0.8),
                    self._make_result("deploy.md", "deploy", 0.6),
                ],
            ),
        ]
        points = c.find_convergence(paths)
        assert len(points) >= 1
        assert points[0].file == "shared.md"
        assert points[0].paths_hit == 2
        assert points[0].confluence_score > 0

    def test_no_convergence(self):
        """Disjoint paths = no convergence points."""
        c = ConfluenceRAG()
        paths = [
            ConfluencePath(
                query="auth",
                results=[self._make_result("a.md", "a")],
            ),
            ConfluencePath(
                query="deploy",
                results=[self._make_result("b.md", "b")],
            ),
        ]
        points = c.find_convergence(paths)
        assert points == []

    def test_three_path_convergence_scores_higher(self):
        """More paths converging = higher score."""
        c = ConfluenceRAG()
        shared = lambda s: self._make_result(  # noqa: E731
            "hub.md", "hub", s
        )
        paths = [
            ConfluencePath("q1", [shared(0.9)]),
            ConfluencePath("q2", [shared(0.8)]),
            ConfluencePath("q3", [shared(0.7)]),
        ]
        points = c.find_convergence(paths)
        assert len(points) == 1
        assert points[0].paths_hit == 3
        # 3/3 convergence ratio = 1.0
        assert points[0].confluence_score > 0.5

    def test_single_path_no_convergence(self):
        """Min convergence = 2, single path hit ignored."""
        c = ConfluenceRAG()
        paths = [
            ConfluencePath(
                "q1",
                [self._make_result("x.md", "x")],
            ),
            ConfluencePath("q2", []),
        ]
        points = c.find_convergence(paths)
        assert points == []

    def test_sorted_by_score(self):
        """Points sorted by confluence_score descending."""
        c = ConfluenceRAG()
        r1 = self._make_result("low.md", "low", 0.3)
        r2 = self._make_result("high.md", "high", 0.9)
        paths = [
            ConfluencePath("q1", [r1, r2]),
            ConfluencePath("q2", [r1, r2]),
        ]
        points = c.find_convergence(paths)
        assert len(points) == 2
        assert points[0].confluence_score >= points[1].confluence_score


# ── RetrievalProfile (Three-Mode System) ─────────────────


class TestRetrievalProfileEnum:
    """Profile enum basics."""

    def test_three_profiles_exist(self):
        assert RetrievalProfile.PRECISION == "precision"
        assert RetrievalProfile.RECALL == "recall"
        assert RetrievalProfile.BALANCED == "balanced"

    def test_all_profiles_have_config(self):
        for p in RetrievalProfile:
            assert p in PROFILE_CONFIG
            cfg = PROFILE_CONFIG[p]
            for key in (
                "noise_ratio_max",
                "confidence_accept",
                "confidence_caution",
                "optimal_results",
                "max_web",
                "crag_on_incorrect",
                "web_policy",
                "position_aware",
                "tier_source_scale",
            ):
                assert key in cfg, f"{p.value} missing {key}"

    def test_precision_strictest(self):
        p = PROFILE_CONFIG[RetrievalProfile.PRECISION]
        r = PROFILE_CONFIG[RetrievalProfile.RECALL]
        assert p["noise_ratio_max"] < r["noise_ratio_max"]
        assert p["confidence_accept"] > r["confidence_accept"]
        assert p["optimal_results"] < r["optimal_results"]

    def test_balanced_in_between(self):
        p = PROFILE_CONFIG[RetrievalProfile.PRECISION]
        b = PROFILE_CONFIG[RetrievalProfile.BALANCED]
        r = PROFILE_CONFIG[RetrievalProfile.RECALL]
        assert p["noise_ratio_max"] < b["noise_ratio_max"] < r["noise_ratio_max"]
        assert p["optimal_results"] < b["optimal_results"] < r["optimal_results"]


class TestProfileNoiseGuard:
    """_apply_noise_guard respects profile ratio_max for web sources."""

    @staticmethod
    def _make_candidates(n_internal: int, n_web: int):
        internal = [
            SourceResult(
                source=SourceType.KB_SKILL,
                file=f"k{i}.md", heading="h", text="t", score=0.9,
            )
            for i in range(n_internal)
        ]
        web = [
            SourceResult(
                source=SourceType.WEB_SEARCH,
                file=f"w{i}", heading="", text="web", score=0.5,
            )
            for i in range(n_web)
        ]
        return internal + web

    def test_precision_rejects_more_web(self):
        """30% noise limit → keeps fewer web results."""
        cands = self._make_candidates(3, 7)
        result = STAREngine._apply_noise_guard(
            cands,
            ratio_max=PROFILE_CONFIG[RetrievalProfile.PRECISION][
                "noise_ratio_max"
            ],
        )
        web_count = sum(
            1 for r in result if r.source == SourceType.WEB_SEARCH
        )
        # 3 internal, 30% max → max_web = floor(3*0.3/0.7) = 1
        assert web_count <= 1

    def test_recall_allows_more_web(self):
        """60% noise limit → keeps more web results."""
        cands = self._make_candidates(3, 7)
        result_r = STAREngine._apply_noise_guard(
            cands,
            ratio_max=PROFILE_CONFIG[RetrievalProfile.RECALL][
                "noise_ratio_max"
            ],
        )
        result_p = STAREngine._apply_noise_guard(
            self._make_candidates(3, 7),
            ratio_max=PROFILE_CONFIG[RetrievalProfile.PRECISION][
                "noise_ratio_max"
            ],
        )
        web_r = sum(
            1 for r in result_r if r.source == SourceType.WEB_SEARCH
        )
        web_p = sum(
            1 for r in result_p if r.source == SourceType.WEB_SEARCH
        )
        assert web_r > web_p

    def test_balanced_between(self):
        """40% balanced between precision 30% and recall 60%."""
        cands = self._make_candidates(3, 7)
        result = STAREngine._apply_noise_guard(
            cands,
            ratio_max=PROFILE_CONFIG[RetrievalProfile.BALANCED][
                "noise_ratio_max"
            ],
        )
        web_b = sum(
            1 for r in result if r.source == SourceType.WEB_SEARCH
        )
        # 3 internal, 40% max → max_web = floor(3*0.4/0.6) = 2
        assert 1 < web_b <= 2


class TestProfileConfidenceGate:
    """ConfidenceGate behaves differently per profile thresholds."""

    def test_precision_higher_bar(self):
        gate = ConfidenceGate()
        candidates = [
            SourceResult(
                source=SourceType.KB_SKILL,
                file="a.md", heading="h",
                text="answer about deploy", score=0.55,
            ),
        ]
        # Precision: accept=0.70 → 0.55 won't reach "accept"
        v_prec = gate.score(
            "deploy",
            candidates,
            accept_threshold=PROFILE_CONFIG[RetrievalProfile.PRECISION]["confidence_accept"],
            caution_threshold=PROFILE_CONFIG[RetrievalProfile.PRECISION]["confidence_caution"],
        )
        # Recall: accept=0.35 → 0.55 should "accept"
        v_recall = gate.score(
            "deploy",
            candidates,
            accept_threshold=PROFILE_CONFIG[RetrievalProfile.RECALL]["confidence_accept"],
            caution_threshold=PROFILE_CONFIG[RetrievalProfile.RECALL]["confidence_caution"],
        )
        # Precision is stricter
        assert v_prec.action in ("use_with_caution", "skip")
        assert v_recall.action == "accept"


class TestProfileEngineInit:
    """STAREngine correctly stores profile config."""

    def test_default_is_precision(self):
        engine = STAREngine(project_dir="/tmp/test_star_prof")
        assert engine.profile == RetrievalProfile.PRECISION
        assert engine._profile_cfg["noise_ratio_max"] == 0.30

    def test_recall_profile(self):
        engine = STAREngine(
            project_dir="/tmp/test_star_prof",
            profile=RetrievalProfile.RECALL,
        )
        assert engine.profile == RetrievalProfile.RECALL
        assert engine._profile_cfg["optimal_results"] == 10
        assert engine._profile_cfg["web_policy"] == "always"

    def test_balanced_profile(self):
        engine = STAREngine(
            project_dir="/tmp/test_star_prof",
            profile=RetrievalProfile.BALANCED,
        )
        assert engine.profile == RetrievalProfile.BALANCED
        assert engine._profile_cfg["crag_on_incorrect"] == "keep_low"

    def test_create_star_engine_accepts_profile(self):
        engine = create_star_engine(
            project_dir="/tmp/test_star_prof",
            profile=RetrievalProfile.RECALL,
        )
        assert engine.profile == RetrievalProfile.RECALL


class TestProfileCRAGBehavior:
    """CRAG action differs per profile."""

    def test_precision_rejects(self):
        cfg = PROFILE_CONFIG[RetrievalProfile.PRECISION]
        assert cfg["crag_on_incorrect"] == "reject"

    def test_recall_downgrades(self):
        cfg = PROFILE_CONFIG[RetrievalProfile.RECALL]
        assert cfg["crag_on_incorrect"] == "downgrade"

    def test_balanced_keeps_low(self):
        cfg = PROFILE_CONFIG[RetrievalProfile.BALANCED]
        assert cfg["crag_on_incorrect"] == "keep_low"

    def test_web_policy_gradient(self):
        assert PROFILE_CONFIG[RetrievalProfile.PRECISION]["web_policy"] == "last_resort"
        assert PROFILE_CONFIG[RetrievalProfile.BALANCED]["web_policy"] == "supplement"
        assert PROFILE_CONFIG[RetrievalProfile.RECALL]["web_policy"] == "always"
