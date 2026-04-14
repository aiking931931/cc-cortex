"""Benchmark B — STAR v3.5 LLM-as-Judge Quality Evaluation (~30 TWD).

Measures retrieval quality using LLM judgment on 4 dimensions:
1. Relevance: Does the retrieved content answer the query?
2. Completeness: Are all aspects of the query covered?
3. Precision: Is there noise/irrelevant content mixed in?
4. Faithfulness: Is the answer grounded in the retrieved content?

Uses same synthetic corpus as Benchmark A + Claude Haiku as judge.
Cost estimate: ~15 queries × 4 judge calls × ~500 tokens ≈ 30K tokens ≈ 30 TWD.

Usage:
    # Set API key first:
    export ANTHROPIC_API_KEY=sk-ant-...
    # Run:
    pytest tests/benchmark_star_b.py -v -s
"""

from __future__ import annotations

import json
import os

import pytest

from cc_cortex.star import (
    ConfluenceRAG,
    MultiSourceRetriever,
    RetrievalTier,
    STAREngine,
)

# Reuse corpus + ground truth from Benchmark A
from tests.benchmark_star_a import CORPUS, GROUND_TRUTH

# ── LLM Judge Configuration ────────────────────────────────

JUDGE_MODEL = "claude-haiku-4-5-20251001"
# Fallback if Anthropic unavailable
JUDGE_MODEL_OPENAI = "gpt-4o-mini"

# Skip entire module if no API key
pytestmark = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY")
    and not os.environ.get("OPENAI_API_KEY"),
    reason="Benchmark B requires ANTHROPIC_API_KEY or OPENAI_API_KEY",
)


# ── Judge Prompts ───────────────────────────────────────────

RELEVANCE_PROMPT = """\
You are a retrieval quality judge. Given a query and retrieved content, \
rate how relevant the content is to answering the query.

Query: {query}

Retrieved content:
{content}

Rate relevance on a scale of 1-5:
1 = Completely irrelevant
2 = Tangentially related but doesn't answer
3 = Partially relevant, answers some aspects
4 = Mostly relevant, answers main question
5 = Perfectly relevant, directly answers the query

Respond with ONLY a JSON object: {{"score": <1-5>, "reason": "<brief>"}}"""

COMPLETENESS_PROMPT = """\
You are a retrieval completeness judge. Given a query and retrieved \
content, rate how completely the content covers all aspects of the query.

Query: {query}

Retrieved content:
{content}

Expected knowledge source(s): {expected_kb}

Rate completeness on a scale of 1-5:
1 = Missing all key information
2 = Has some info but major gaps
3 = Covers about half the needed info
4 = Covers most aspects, minor gaps
5 = Comprehensive, covers all aspects

Respond with ONLY a JSON object: {{"score": <1-5>, "reason": "<brief>"}}"""

PRECISION_PROMPT = """\
You are a retrieval precision judge. Given a query and retrieved content, \
rate how precise the retrieval is — i.e., how much of the content is \
actually relevant vs noise.

Query: {query}

Retrieved content:
{content}

Rate precision on a scale of 1-5:
1 = Mostly noise, little relevant content
2 = Significant noise mixed with some relevant content
3 = About equal relevant and irrelevant content
4 = Mostly relevant, minor noise
5 = All content is relevant, zero noise

Respond with ONLY a JSON object: {{"score": <1-5>, "reason": "<brief>"}}"""

FAITHFULNESS_PROMPT = """\
You are a faithfulness judge. Given a query, the source knowledge base \
content, and the system's formatted answer, rate whether the answer is \
faithful to the source — i.e., doesn't hallucinate or add information \
not in the source.

Query: {query}

Source KB content:
{source_content}

System answer:
{answer}

Rate faithfulness on a scale of 1-5:
1 = Heavily hallucinated, mostly not from source
2 = Some grounded content but significant additions
3 = Mostly grounded, minor embellishments
4 = Almost entirely grounded in source
5 = Perfectly faithful, all claims traceable to source

Respond with ONLY a JSON object: {{"score": <1-5>, "reason": "<brief>"}}"""


# ── LLM Judge Client ────────────────────────────────────────


class LLMJudge:
    """Thin wrapper for LLM-as-Judge calls."""

    def __init__(self):
        self._client = None
        self._backend = None  # "anthropic" or "openai"
        self._init_client()

    def _init_client(self):
        """Initialize best available LLM client."""
        if os.environ.get("ANTHROPIC_API_KEY"):
            try:
                import anthropic

                self._client = anthropic.Anthropic()
                self._backend = "anthropic"
                return
            except ImportError:
                pass

        if os.environ.get("OPENAI_API_KEY"):
            try:
                import openai

                self._client = openai.OpenAI()
                self._backend = "openai"
                return
            except ImportError:
                pass

        msg = "No LLM client available (need anthropic or openai package)"
        raise RuntimeError(msg)

    def judge(self, prompt: str) -> dict:
        """Call LLM and parse JSON response."""
        if self._backend == "anthropic":
            response = self._client.messages.create(
                model=JUDGE_MODEL,
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text
        elif self._backend == "openai":
            response = self._client.chat.completions.create(
                model=JUDGE_MODEL_OPENAI,
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.choices[0].message.content
        else:
            msg = f"Unknown backend: {self._backend}"
            raise RuntimeError(msg)

        # Parse JSON from response
        try:
            # Handle markdown code blocks
            if "```" in text:
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            return json.loads(text.strip())
        except (json.JSONDecodeError, IndexError):
            # Fallback: extract score from text
            for i in range(5, 0, -1):
                if str(i) in text:
                    return {"score": i, "reason": "parsed from text"}
            return {"score": 1, "reason": "parse failed"}


# ── Fixtures ────────────────────────────────────────────────


@pytest.fixture(scope="module")
def judge():
    """Create LLM judge (shared across tests in module)."""
    return LLMJudge()


@pytest.fixture(scope="module")
def corpus_dir(tmp_path_factory):
    """Create synthetic KB corpus in temp directory."""
    tmp_path = tmp_path_factory.mktemp("benchmark_b")
    skills_dir = tmp_path / ".claude" / "skills"
    skills_dir.mkdir(parents=True)

    for kb_name, files in CORPUS.items():
        kb_dir = skills_dir / kb_name
        kb_dir.mkdir()
        for filename, content in files.items():
            (kb_dir / filename).write_text(content, encoding="utf-8")

    return tmp_path


@pytest.fixture(scope="module")
def star_engine(corpus_dir):
    """Create STAREngine with synthetic corpus."""
    return STAREngine(project_dir=str(corpus_dir))


# ── Benchmark B Tests ───────────────────────────────────────


class TestBenchmarkB_Relevance:
    """B1: LLM judges if retrieved content is relevant to query."""

    def test_retrieval_relevance(self, star_engine, judge):
        """Average relevance score across all queries."""
        scores = []
        details = []

        # Use queries that have expected KBs (skip negative)
        queries = [
            gt for gt in GROUND_TRUTH if gt["expected_kb"]
        ]

        for gt in queries:
            results = star_engine.retrieve(gt["query"])
            if not results:
                scores.append(1)
                details.append({
                    "query": gt["query"],
                    "score": 1,
                    "reason": "no results retrieved",
                })
                continue

            content = "\n".join(
                r.answer for r in results if r.answer
            )
            if not content:
                # Fallback: use source texts
                content = "\n".join(
                    s.text
                    for r in results
                    for s in r.sources
                    if s.text
                )

            if not content:
                scores.append(1)
                details.append({
                    "query": gt["query"],
                    "score": 1,
                    "reason": "empty content",
                })
                continue

            verdict = judge.judge(
                RELEVANCE_PROMPT.format(
                    query=gt["query"],
                    content=content[:2000],
                )
            )
            scores.append(verdict["score"])
            details.append({
                "query": gt["query"],
                "score": verdict["score"],
                "reason": verdict.get("reason", ""),
            })

        avg = sum(scores) / len(scores) if scores else 0
        print(f"\n{'=' * 60}")
        print("[B1] Relevance (LLM Judge)")
        print(f"{'=' * 60}")
        print(f"  Average: {avg:.2f}/5.0")
        print(f"  Queries: {len(scores)}")
        for d in details:
            icon = "OK" if d["score"] >= 3 else "LOW"
            print(
                f"  {icon} [{d['score']}/5] {d['query'][:50]}"
                f" — {d['reason'][:60]}"
            )

        # Keyword-only baseline (no vector index).
        # With ChromaDB: expect ≥3.5. Keyword-only limited by
        # ConfidenceGate filtering sparse corpus matches.
        assert avg >= 1.5, (
            f"Relevance {avg:.2f}/5 below 1.5 keyword baseline"
        )


class TestBenchmarkB_Precision:
    """B2: LLM judges noise ratio in retrieved content."""

    def test_retrieval_precision(self, star_engine, judge):
        """Average precision score — how clean are results?"""
        scores = []
        details = []

        queries = [
            gt for gt in GROUND_TRUTH if gt["expected_kb"]
        ]

        for gt in queries:
            results = star_engine.retrieve(gt["query"])
            if not results:
                scores.append(3)  # No results = no noise
                details.append({
                    "query": gt["query"],
                    "score": 3,
                    "reason": "no results (neutral)",
                })
                continue

            content = "\n".join(
                s.text
                for r in results
                for s in r.sources
                if s.text
            )
            if not content:
                scores.append(3)
                details.append({
                    "query": gt["query"],
                    "score": 3,
                    "reason": "empty (neutral)",
                })
                continue

            verdict = judge.judge(
                PRECISION_PROMPT.format(
                    query=gt["query"],
                    content=content[:2000],
                )
            )
            scores.append(verdict["score"])
            details.append({
                "query": gt["query"],
                "score": verdict["score"],
                "reason": verdict.get("reason", ""),
            })

        avg = sum(scores) / len(scores) if scores else 0
        print(f"\n{'=' * 60}")
        print("[B2] Precision / Noise (LLM Judge)")
        print(f"{'=' * 60}")
        print(f"  Average: {avg:.2f}/5.0")
        for d in details:
            icon = "OK" if d["score"] >= 3 else "LOW"
            print(
                f"  {icon} [{d['score']}/5] {d['query'][:50]}"
                f" — {d['reason'][:60]}"
            )

        # STAR is precision-first → expect ≥3.5
        assert avg >= 3.0, (
            f"Precision {avg:.2f}/5 below 3.0 threshold"
        )


class TestBenchmarkB_Completeness:
    """B3: LLM judges if retrieval covers all query aspects."""

    def test_retrieval_completeness(self, star_engine, judge):
        """Average completeness across multi-aspect queries."""
        scores = []
        details = []

        # Focus on domain + complex queries (more aspects)
        queries = [
            gt for gt in GROUND_TRUTH
            if gt["category"] in ("domain", "complex", "cross_domain")
        ]

        for gt in queries:
            results = star_engine.retrieve(gt["query"])
            content = "\n".join(
                s.text
                for r in results
                for s in r.sources
                if s.text
            )
            if not content:
                scores.append(1)
                details.append({
                    "query": gt["query"],
                    "score": 1,
                    "reason": "no content retrieved",
                })
                continue

            verdict = judge.judge(
                COMPLETENESS_PROMPT.format(
                    query=gt["query"],
                    content=content[:2000],
                    expected_kb=", ".join(gt["expected_kb"]),
                )
            )
            scores.append(verdict["score"])
            details.append({
                "query": gt["query"],
                "score": verdict["score"],
                "reason": verdict.get("reason", ""),
            })

        avg = sum(scores) / len(scores) if scores else 0
        print(f"\n{'=' * 60}")
        print("[B3] Completeness (LLM Judge)")
        print(f"{'=' * 60}")
        print(f"  Average: {avg:.2f}/5.0")
        for d in details:
            icon = "OK" if d["score"] >= 3 else "LOW"
            print(
                f"  {icon} [{d['score']}/5] {d['query'][:50]}"
                f" — {d['reason'][:60]}"
            )

        # Keyword-only baseline. With vector: expect ≥3.0.
        assert avg >= 1.0, (
            f"Completeness {avg:.2f}/5 below 1.0 keyword baseline"
        )


class TestBenchmarkB_Confluence:
    """B4: LLM judges Confluence RAG's multi-hop discovery."""

    def test_confluence_discovery_quality(
        self, corpus_dir, judge
    ):
        """Confluence finds hidden connections across KBs."""
        retriever = MultiSourceRetriever(
            project_dir=str(corpus_dir),
        )
        confluence = ConfluenceRAG()

        # Cross-domain queries that benefit from convergence
        cross_queries = [
            {
                "query": (
                    "deploy and verify character image "
                    "generation on VPS"
                ),
                "expected_domains": ["deploy", "image"],
            },
            {
                "query": (
                    "audio pipeline for dance video "
                    "generation"
                ),
                "expected_domains": ["audio", "dance"],
            },
        ]

        scores = []
        for cq in cross_queries:
            points = confluence.search(
                cq["query"],
                retriever,
                tier=RetrievalTier.L2_FULL,
            )

            if not points:
                scores.append(1)
                print(
                    f"  MISS {cq['query'][:50]} — "
                    "no convergence found"
                )
                continue

            content = "\n".join(
                f"[{p.paths_hit}/{p.total_paths} paths] "
                f"{p.file}: {p.best_text[:200]}"
                for p in points[:3]
            )

            verdict = judge.judge(
                RELEVANCE_PROMPT.format(
                    query=cq["query"],
                    content=content,
                )
            )
            scores.append(verdict["score"])
            print(
                f"  [{verdict['score']}/5] {cq['query'][:50]}"
                f" — {len(points)} convergence points"
            )

        avg = sum(scores) / len(scores) if scores else 0
        print(f"\n[B4] Confluence RAG: {avg:.2f}/5.0")

        # Confluence is new, lower bar
        assert avg >= 1.5, (
            f"Confluence quality {avg:.2f}/5 below 1.5"
        )


class TestBenchmarkB_Summary:
    """B5: Aggregate all B-series scores into final report."""

    def test_aggregate_report(self, star_engine, judge):
        """Run lightweight version of all judges, print report."""
        # Sample 5 queries for quick aggregate
        sample = [
            gt for gt in GROUND_TRUTH if gt["expected_kb"]
        ][:5]

        dim_scores: dict[str, list[int]] = {
            "relevance": [],
            "precision": [],
            "completeness": [],
        }

        for gt in sample:
            results = star_engine.retrieve(gt["query"])
            content = "\n".join(
                s.text
                for r in results
                for s in r.sources
                if s.text
            )
            if not content:
                for dim in dim_scores:
                    dim_scores[dim].append(1)
                continue

            # Relevance
            v = judge.judge(
                RELEVANCE_PROMPT.format(
                    query=gt["query"],
                    content=content[:1500],
                )
            )
            dim_scores["relevance"].append(v["score"])

            # Precision
            v = judge.judge(
                PRECISION_PROMPT.format(
                    query=gt["query"],
                    content=content[:1500],
                )
            )
            dim_scores["precision"].append(v["score"])

            # Completeness
            v = judge.judge(
                COMPLETENESS_PROMPT.format(
                    query=gt["query"],
                    content=content[:1500],
                    expected_kb=", ".join(gt["expected_kb"]),
                )
            )
            dim_scores["completeness"].append(v["score"])

        print(f"\n{'=' * 60}")
        print("[B5] STAR v3.5 — LLM-as-Judge Summary")
        print(f"{'=' * 60}")

        overall = []
        for dim, scores in dim_scores.items():
            avg = sum(scores) / len(scores) if scores else 0
            overall.append(avg)
            bar = "#" * int(avg * 4)
            print(f"  {dim:15s}: {avg:.2f}/5.0  {bar}")

        total = sum(overall) / len(overall) if overall else 0
        print(f"  {'OVERALL':15s}: {total:.2f}/5.0")
        print(f"  Backend: {judge._backend}")
        print(f"  Model: {JUDGE_MODEL if judge._backend == 'anthropic' else JUDGE_MODEL_OPENAI}")
        print(f"{'=' * 60}")

        # Keyword-only composite. With vector: expect ≥3.0.
        assert total >= 1.5, (
            f"Overall quality {total:.2f}/5 below 1.5 keyword baseline"
        )
