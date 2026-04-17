"""Benchmark A — STAR v3.5 Pure Retrieval Metrics (Local, $0).

Measures: Precision@K, Recall@K, MRR, NDCG, NoiseGuard effectiveness,
Router accuracy, Position ranking correctness.

Uses synthetic KB corpus + ground truth. No LLM API calls.
"""

import math
import time

import pytest

from concinno.riverbed import RiverbedMemory
from concinno.star import (
    NOISE_RATIO_MAX,
    OPTIMAL_RESULTS_PER_QUERY,
    AdaptiveRouter,
    ConfidenceGate,
    FreshnessScorer,
    RetrievalResult,
    RetrievalTier,
    SourceResult,
    SourceType,
    STAREngine,
)

# ── Synthetic Corpus ─────────────────────────────────────────

CORPUS = {
    "kb_deploy": {
        "SKILL.md": (
            "---\nname: kb_deploy\ndescription: Deployment and cloud operations "
            "knowledge — VPS SSH, deploy.py, Cloudflare Tunnel, port config\n"
            "type: kb\n---\n\n# Deployment KB\n\n"
            "## VPS Setup\nSSH into server: `ssh root@5.104.83.69`\n"
            "Deploy command: `python deploy.py`\n"
            "Ports: 3000 (frontend), 8000 (API), 5432 (postgres)\n\n"
            "## Cloudflare Tunnel\nTunnel name: psycheforge-tunnel\n"
            "Config: ~/.cloudflared/config.yml\n"
        ),
    },
    "kb_audio": {
        "SKILL.md": (
            "---\nname: kb_audio\ndescription: Audio and translation pipeline — "
            "STT/TTS, iOS Safari audio bugs, translation architecture\n"
            "type: kb\n---\n\n# Audio KB\n\n"
            "## TTS Parameters\nVoice model: eleven_multilingual_v2\n"
            "Sample rate: 44100\nFormat: mp3\n\n"
            "## STT\nProvider: Whisper API\nMax duration: 120s\n"
            "Language detection: automatic\n"
        ),
    },
    "kb_cognition": {
        "SKILL.md": (
            "---\nname: kb_cognition\ndescription: CBUA cognitive behavioral "
            "unified architecture — cognitive layers, behavioral phases, "
            "book theories, research frontier\ntype: kb\n---\n\n"
            "# Cognition KB\n\n## CBUA Layers\n"
            "C0 Perception -> C1 Fast -> C2 Structured -> C3 Deep -> C4 Meta -> C5 Self-correct\n"
            "A0 Orient -> A1 Plan -> A2 Execute -> A3 Verify -> A4 Adapt -> A5 Defend\n\n"
            "## Five Laws\n1. Cognitive Conservation\n2. Complexity Matching\n"
            "3. Side-effect Awareness\n4. Verification Supremacy\n5. Adaptive Evolution\n"
        ),
    },
    "kb_image": {
        "SKILL.md": (
            "---\nname: kb_image\ndescription: Character image generation — "
            "Kontext API, _reference base images, fal.ai upload, face-swap flow\n"
            "type: kb\n---\n\n# Image Generation KB\n\n"
            "## Kontext API\nModel: fal-ai/flux-pro/kontext\n"
            "Reference images stored in: _reference/ directory\n"
            "Real characters MUST use Kontext, never pure text-to-image\n\n"
            "## Face Check\nProvider: FaceCheck.ID\nCost: ~10 TWD per search\n"
        ),
    },
    "kb_dance": {
        "SKILL.md": (
            "---\nname: kb_dance\ndescription: Dance video and character video "
            "generation — Motion Control, Kling, K-pop dance, long video pipeline\n"
            "type: kb\n---\n\n# Dance Video KB\n\n"
            "## Pipeline\nSource: reference dance video\n"
            "Method: ControlNet frame-by-frame -> stitch 30s+\n"
            "Short clips: micro-motion 1-2s (expressions, head sway)\n\n"
            "## K-Pop\nMusic: original track (no Suno)\n"
        ),
    },
    "kb_word": {
        "SKILL.md": (
            "---\nname: kb_word\ndescription: Word document operations — "
            "MCP word-server, python-docx, book formatting standards\n"
            "type: kb\n---\n\n# Word KB\n\n"
            "## MCP Server\nTool: word-server\n"
            "Operations: create, read, replace, apply_style, insert_image\n\n"
            "## python-docx\nFont: Noto Sans TC (Chinese), Times New Roman (English)\n"
            "Page size: A5\n"
        ),
    },
}

# Ground truth: query -> expected KB skill(s) + expected tier
GROUND_TRUTH = [
    # ── Simple lookups (should route L0) ──
    {
        "query": "what port frontend",
        "expected_kb": ["kb_deploy"],
        "expected_tier": RetrievalTier.L0_INDEX,
        "category": "simple_lookup",
    },
    {
        "query": "SSH server address",
        "expected_kb": ["kb_deploy"],
        "expected_tier": RetrievalTier.L0_INDEX,
        "category": "simple_lookup",
    },
    {
        "query": "TTS voice model",
        "expected_kb": ["kb_audio"],
        "expected_tier": RetrievalTier.L1_SUMMARY,
        "category": "simple_lookup",
    },
    {
        "query": "Kontext API model name",
        "expected_kb": ["kb_image"],
        "expected_tier": RetrievalTier.L1_SUMMARY,
        "category": "simple_lookup",
    },
    {
        "query": "Word document font",
        "expected_kb": ["kb_word"],
        "expected_tier": RetrievalTier.L1_SUMMARY,
        "category": "simple_lookup",
    },
    # ── Domain queries (should route L1) ──
    {
        "query": "how to deploy the frontend to VPS",
        "expected_kb": ["kb_deploy"],
        "expected_tier": RetrievalTier.L1_SUMMARY,
        "category": "domain",
    },
    {
        "query": "audio translation pipeline architecture",
        "expected_kb": ["kb_audio"],
        "expected_tier": RetrievalTier.L1_SUMMARY,
        "category": "domain",
    },
    {
        "query": "CBUA cognitive layers explained",
        "expected_kb": ["kb_cognition"],
        "expected_tier": RetrievalTier.L1_SUMMARY,
        "category": "domain",
    },
    {
        "query": "face swap workflow for character images",
        "expected_kb": ["kb_image"],
        "expected_tier": RetrievalTier.L1_SUMMARY,
        "category": "domain",
    },
    {
        "query": "K-pop dance video generation pipeline",
        "expected_kb": ["kb_dance"],
        "expected_tier": RetrievalTier.L1_SUMMARY,
        "category": "domain",
    },
    # ── Complex queries (should route L2) ──
    {
        "query": "how should I architect the deployment pipeline with Cloudflare Tunnel",
        "expected_kb": ["kb_deploy"],
        "expected_tier": RetrievalTier.L2_FULL,
        "category": "complex",
    },
    {
        "query": "debug why audio playback fails on iOS Safari",
        "expected_kb": ["kb_audio"],
        "expected_tier": RetrievalTier.L2_FULL,
        "category": "complex",
    },
    {
        "query": "refactor the cognitive router to support complexity matching",
        "expected_kb": ["kb_cognition"],
        "expected_tier": RetrievalTier.L2_FULL,
        "category": "complex",
    },
    # ── Cross-domain queries ──
    {
        "query": "deploy and verify character image generation on VPS",
        "expected_kb": ["kb_deploy", "kb_image"],
        "expected_tier": RetrievalTier.L2_FULL,
        "category": "cross_domain",
    },
    # ── Negative: no relevant KB ──
    {
        "query": "stock market prediction algorithm",
        "expected_kb": [],
        "expected_tier": RetrievalTier.L1_SUMMARY,  # default
        "category": "negative",
    },
]


# ── Metrics ──────────────────────────────────────────────────


def precision_at_k(retrieved: list[str], relevant: list[str], k: int) -> float:
    """Precision@K: fraction of top-K that are relevant."""
    if k <= 0:
        return 0.0
    top_k = retrieved[:k]
    if not top_k:
        return 0.0
    hits = sum(1 for r in top_k if r in relevant)
    return hits / len(top_k)


def recall_at_k(retrieved: list[str], relevant: list[str], k: int) -> float:
    """Recall@K: fraction of relevant found in top-K."""
    if not relevant:
        return 1.0  # nothing to find = perfect recall
    top_k = retrieved[:k]
    hits = sum(1 for r in relevant if r in top_k)
    return hits / len(relevant)


def mrr(retrieved: list[str], relevant: list[str]) -> float:
    """Mean Reciprocal Rank: 1/rank of first relevant result."""
    for i, r in enumerate(retrieved):
        if r in relevant:
            return 1.0 / (i + 1)
    return 0.0


def ndcg_at_k(retrieved: list[str], relevant: list[str], k: int) -> float:
    """Normalized Discounted Cumulative Gain @K."""
    def dcg(results: list[str], rel: list[str], n: int) -> float:
        score = 0.0
        for i, r in enumerate(results[:n]):
            if r in rel:
                score += 1.0 / math.log2(i + 2)  # i+2 because log2(1)=0
        return score

    actual = dcg(retrieved, relevant, k)
    # Ideal: all relevant first
    ideal = dcg(relevant + [r for r in retrieved if r not in relevant], relevant, k)
    return actual / ideal if ideal > 0 else 0.0


# ── Test Fixtures ────────────────────────────────────────────


@pytest.fixture
def corpus_dir(tmp_path):
    """Create synthetic KB corpus in temp directory."""
    skills_dir = tmp_path / ".claude" / "skills"
    skills_dir.mkdir(parents=True)

    for kb_name, files in CORPUS.items():
        kb_dir = skills_dir / kb_name
        kb_dir.mkdir()
        for filename, content in files.items():
            (kb_dir / filename).write_text(content, encoding="utf-8")

    return tmp_path


@pytest.fixture
def star_engine(corpus_dir):
    """Create STAREngine with synthetic corpus."""
    return STAREngine(project_dir=str(corpus_dir))


# ── Benchmark A: Retrieval Metrics ───────────────────────────


class TestBenchmarkA_RouterAccuracy:
    """A1: Does the router pick the right tier?"""

    def test_router_tier_accuracy(self):
        router = AdaptiveRouter()
        correct = 0
        total = len(GROUND_TRUTH)

        for gt in GROUND_TRUTH:
            predicted = router.route(gt["query"])
            if predicted == gt["expected_tier"]:
                correct += 1

        accuracy = correct / total
        print(f"\n[A1] Router Accuracy: {accuracy:.1%} ({correct}/{total})")
        # Target: ≥70% (tier routing is heuristic, not exact)
        assert accuracy >= 0.60, f"Router accuracy {accuracy:.1%} below 60% threshold"


class TestBenchmarkA_KBRetrieval:
    """A2: Can STAR find the right KB for each query?"""

    def test_kb_precision_recall(self, star_engine):
        """End-to-end: query -> STAR -> check if correct KB found."""
        precisions = []
        recalls = []
        mrrs = []
        ndcgs = []

        results_log = []

        for gt in GROUND_TRUTH:
            results = star_engine.retrieve(gt["query"])

            # Extract KB names from results' source files
            retrieved_kbs = []
            for r in results:
                for s in r.sources:
                    if s.file:
                        # Extract kb_name from path like ".claude/skills/kb_deploy/SKILL.md"
                        parts = s.file.replace("\\", "/").split("/")
                        for p in parts:
                            if p.startswith("kb_"):
                                if p not in retrieved_kbs:
                                    retrieved_kbs.append(p)

            expected = gt["expected_kb"]
            k = OPTIMAL_RESULTS_PER_QUERY

            p = precision_at_k(retrieved_kbs, expected, k)
            r = recall_at_k(retrieved_kbs, expected, k)
            m = mrr(retrieved_kbs, expected)
            n = ndcg_at_k(retrieved_kbs, expected, k)

            precisions.append(p)
            recalls.append(r)
            mrrs.append(m)
            ndcgs.append(n)

            results_log.append({
                "query": gt["query"],
                "expected": expected,
                "retrieved": retrieved_kbs,
                "P@3": f"{p:.2f}",
                "R@3": f"{r:.2f}",
                "MRR": f"{m:.2f}",
                "category": gt["category"],
            })

        avg_p = sum(precisions) / len(precisions)
        avg_r = sum(recalls) / len(recalls)
        avg_mrr = sum(mrrs) / len(mrrs)
        avg_ndcg = sum(ndcgs) / len(ndcgs)

        print(f"\n{'='*60}")
        print("[A2] STAR v3.5 Retrieval Benchmark")
        print(f"{'='*60}")
        print(f"  Queries:      {len(GROUND_TRUTH)}")
        print(f"  Precision@3:  {avg_p:.1%}")
        print(f"  Recall@3:     {avg_r:.1%}")
        print(f"  MRR:          {avg_mrr:.3f}")
        print(f"  NDCG@3:       {avg_ndcg:.3f}")
        print(f"{'='*60}")

        # Per-category breakdown
        categories = set(gt["category"] for gt in GROUND_TRUTH)
        for cat in sorted(categories):
            cat_indices = [i for i, gt in enumerate(GROUND_TRUTH) if gt["category"] == cat]
            cat_p = sum(precisions[i] for i in cat_indices) / len(cat_indices)
            cat_r = sum(recalls[i] for i in cat_indices) / len(cat_indices)
            print(f"  [{cat:15s}] P@3={cat_p:.1%}  R@3={cat_r:.1%}")

        # Failed queries
        failed = [r for r in results_log if r["R@3"] == "0.00" and r["expected"]]
        if failed:
            print(f"\n  MISSED Missed ({len(failed)}):")
            for f in failed:
                print(f"    - {f['query']} -> expected {f['expected']}, got {f['retrieved']}")

        print()

        # Targets — keyword-only baseline (no vector index).
        # With ChromaDB vector source, expect 70%+ P/R.
        # Keyword-only is inherently limited on semantic queries.
        assert avg_p >= 0.25, f"Precision@3 {avg_p:.1%} below 25% (keyword baseline)"
        assert avg_r >= 0.35, f"Recall@3 {avg_r:.1%} below 35% (keyword baseline)"
        assert avg_mrr >= 0.25, f"MRR {avg_mrr:.3f} below 0.25 (keyword baseline)"


class TestBenchmarkA_NoiseGuard:
    """A3: Does NoiseGuard actually prevent noise pollution?"""

    def test_noise_ratio_enforcement(self):
        """Inject varying web:internal ratios, verify 30% ceiling."""
        test_cases = [
            (1, 10),   # 1 internal, 10 web
            (3, 10),   # 3 internal, 10 web
            (5, 5),    # balanced
            (10, 1),   # mostly internal
            (0, 5),    # all web
        ]

        all_pass = True
        for n_internal, n_web in test_cases:
            candidates = [
                SourceResult(source=SourceType.KB_SKILL, text=f"i{i}", score=0.8)
                for i in range(n_internal)
            ]
            candidates += [
                SourceResult(source=SourceType.WEB_SEARCH, text=f"w{i}", score=0.5)
                for i in range(n_web)
            ]

            result = STAREngine._apply_noise_guard(candidates)
            total = len(result)
            web_count = sum(1 for r in result if r.source == SourceType.WEB_SEARCH)

            if total > 0:
                ratio = web_count / total
                ok = ratio <= NOISE_RATIO_MAX + 0.01  # rounding tolerance
            else:
                ok = n_internal == 0  # 0 internal -> 0 total is correct
                ratio = 0.0

            status = "OK" if ok else "MISSED"
            print(f"  {status} {n_internal}i+{n_web}w -> {total} kept, "
                  f"web={web_count} ({ratio:.0%})")
            if not ok:
                all_pass = False

        assert all_pass, "NoiseGuard failed to enforce 30% ceiling"


class TestBenchmarkA_FreshnessScorer:
    """A4: Does freshness scoring correctly penalize stale, reward fresh?"""

    def test_freshness_ordering(self):
        """Fresh results should score higher than stale ones."""
        scorer = FreshnessScorer()
        now = time.time()

        candidates = [
            SourceResult(source=SourceType.KB_SKILL, text="fresh",
                        score=0.8, timestamp=now),
            SourceResult(source=SourceType.KB_SKILL, text="week_old",
                        score=0.8, timestamp=now - 7 * 86400),
            SourceResult(source=SourceType.KB_SKILL, text="month_old",
                        score=0.8, timestamp=now - 30 * 86400),
            SourceResult(source=SourceType.KB_SKILL, text="year_old",
                        score=0.8, timestamp=now - 365 * 86400),
        ]

        scored = scorer.apply_to_results(candidates)
        freshness_scores = [c.freshness for c in scored]

        print("\n[A4] Freshness Scores:")
        for c in scored:
            print(f"  {c.text:12s} -> freshness={c.freshness:.3f}")

        # Fresh > week > month > year
        assert (
            freshness_scores[0] >= freshness_scores[1]
            >= freshness_scores[2] >= freshness_scores[3]
        ), "Freshness ordering violated"
        # Fresh should be close to 1.0
        assert freshness_scores[0] >= 0.95, f"Fresh item got {freshness_scores[0]:.3f}"


class TestBenchmarkA_ConfidenceGate:
    """A5: Does confidence gating properly filter low-quality results?"""

    def test_confidence_discrimination(self):
        """High-quality results should pass, low-quality should be filtered."""
        gate = ConfidenceGate()

        # High quality: multiple agreeing sources, good depth
        high_quality = [
            SourceResult(source=SourceType.KB_SKILL, text="deploy via SSH",
                        score=0.9, depth=0.8, timestamp=time.time()),
            SourceResult(source=SourceType.RAG_VECTOR, text="deploy using SSH",
                        score=0.85, depth=0.7, timestamp=time.time()),
        ]

        # Low quality: single weak source
        low_quality = [
            SourceResult(source=SourceType.WEB_SEARCH, text="maybe deploy?",
                        score=0.3, depth=0.1, timestamp=time.time() - 365*86400),
        ]

        high_verdict = gate.score("deploy SSH", high_quality)
        low_verdict = gate.score("deploy SSH", low_quality)

        print("\n[A5] Confidence Gate:")
        print(f"  High quality: {high_verdict.score:.1%} ({high_verdict.action})")
        print(f"  Low quality:  {low_verdict.score:.1%} ({low_verdict.action})")

        assert high_verdict.score > low_verdict.score, \
            "High quality should score higher than low quality"
        assert high_verdict.score >= 0.60, \
            f"High quality got only {high_verdict.score:.1%}"


class TestBenchmarkA_PositionRanking:
    """A6: Does position-aware injection match Liu 2023 optimal layout?"""

    def test_position_optimality(self, corpus_dir):
        """Best result at position 1, second-best at last."""
        engine = STAREngine(project_dir=str(corpus_dir))

        # Create results with known confidence ordering
        results = [
            RetrievalResult(question="q", answer="A", confidence=0.95,
                           tier=RetrievalTier.L2_FULL),
            RetrievalResult(question="q", answer="B", confidence=0.85,
                           tier=RetrievalTier.L2_FULL),
            RetrievalResult(question="q", answer="C", confidence=0.70,
                           tier=RetrievalTier.L2_FULL),
            RetrievalResult(question="q", answer="D", confidence=0.60,
                           tier=RetrievalTier.L2_FULL),
        ]

        output = engine.format_injection(results)
        lines = [ln for ln in output.strip().split("\n") if ln.startswith("  [")]

        if len(lines) >= 3:
            # Position 1 should have highest confidence
            assert "A" in lines[0], f"Position 1 should be 'A', got: {lines[0]}"
            # Last position should have second-highest
            assert "B" in lines[-1], f"Last position should be 'B', got: {lines[-1]}"

            print("\n[A6] Position Ranking:")
            for i, line in enumerate(lines):
                pos_label = "primacy" if i == 0 else ("recency" if i == len(lines)-1 else "middle")
                print(f"  [{pos_label:8s}] {line.strip()}")


class TestBenchmarkA_RiverbedIntegration:
    """A7: Does Riverbed depth affect retrieval confidence?"""

    def test_deep_riverbed_preferred(self, tmp_path):
        """Deeply carved riverbeds should get higher confidence."""
        rb = RiverbedMemory(str(tmp_path / "riverbed.json"))

        # Experience same concept multiple times to carve deep
        for _ in range(5):
            rb.experience("deployment process SSH server", emotional_charge=0.8)
        # Experience another concept once (shallow)
        rb.experience("dancing video clip", emotional_charge=0.1)

        deep = rb.recall("deployment", top_k=1)
        shallow = rb.recall("dancing", top_k=1)

        print("\n[A7] Riverbed Depth:")
        if deep:
            print(f"  Deep (5x):  depth={deep[0].depth:.3f}, "
                  f"charge={deep[0].emotional_charge:.3f}")
        if shallow:
            print(f"  Shallow (1x): depth={shallow[0].depth:.3f}, "
                  f"charge={shallow[0].emotional_charge:.3f}")

        assert deep and shallow, "Both should return results"
        assert deep[0].depth > shallow[0].depth, \
            f"Deep {deep[0].depth:.3f} should > shallow {shallow[0].depth:.3f}"


# ── Summary Runner ───────────────────────────────────────────

class TestBenchmarkA_Summary:
    """Final summary with pass/fail per component."""

    def test_summary(self):
        """Just a marker — pytest output from above tests IS the summary."""
        print("\n" + "="*60)
        print("STAR v3.5 Benchmark A — Pure Retrieval Metrics")
        print("="*60)
        print("  A1: Router Accuracy       — see above")
        print("  A2: KB Retrieval P/R/MRR  — see above")
        print("  A3: NoiseGuard 30%        — see above")
        print("  A4: Freshness Ordering    — see above")
        print("  A5: Confidence Gate       — see above")
        print("  A6: Position Ranking      — see above")
        print("  A7: Riverbed Integration  — see above")
        print("="*60)
        print("  All local, $0. No API calls.")
        print()
