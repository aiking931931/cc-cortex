"""BEIR Benchmark Runner for STAR RAG Engine.

Team: AI King
System: CCC STAR v3.5 — Three-Mode Retrieval

Tests retrieval quality (nDCG@10, Recall@100) on standard IR datasets.
This is the RAG-specific benchmark — proves STAR engine's core value.

Usage:
    # SciFact (small, fast)
    python benchmarks/beir_runner.py --dataset scifact

    # With specific profile
    python benchmarks/beir_runner.py --dataset scifact --profile recall

    # Ablation: BM25 only
    python benchmarks/beir_runner.py --dataset scifact --bm25-only

    # Ablation: Dense only
    python benchmarks/beir_runner.py --dataset scifact --dense-only
"""

from __future__ import annotations

import json
import math
import os
import time
from collections import defaultdict

from beir import util
from beir.datasets.data_loader import GenericDataLoader

from concinno.star import (
    BM25Index,
    PROFILE_CONFIG,
    RetrievalProfile,
)

# ── Dataset URLs ──────────────────────────────────────────

DATASET_URLS = {
    "scifact": (
        "https://public.ukp.informatik.tu-darmstadt.de"
        "/thakur/BEIR/datasets/scifact.zip"
    ),
    "nfcorpus": (
        "https://public.ukp.informatik.tu-darmstadt.de"
        "/thakur/BEIR/datasets/nfcorpus.zip"
    ),
    "fiqa": (
        "https://public.ukp.informatik.tu-darmstadt.de"
        "/thakur/BEIR/datasets/fiqa.zip"
    ),
}

# ── Metrics ───────────────────────────────────────────────


def ndcg_at_k(
    ranked_ids: list[str],
    relevant: dict[str, int],
    k: int = 10,
) -> float:
    """Compute nDCG@k."""
    dcg = 0.0
    for i, doc_id in enumerate(ranked_ids[:k]):
        rel = relevant.get(doc_id, 0)
        dcg += rel / math.log2(i + 2)

    # Ideal DCG
    ideal_rels = sorted(relevant.values(), reverse=True)[:k]
    idcg = sum(r / math.log2(i + 2) for i, r in enumerate(ideal_rels))

    return dcg / idcg if idcg > 0 else 0.0


def recall_at_k(
    ranked_ids: list[str],
    relevant: dict[str, int],
    k: int = 100,
) -> float:
    """Compute Recall@k."""
    if not relevant:
        return 0.0
    retrieved_relevant = sum(
        1 for doc_id in ranked_ids[:k] if doc_id in relevant
    )
    return retrieved_relevant / len(relevant)


def precision_at_k(
    ranked_ids: list[str],
    relevant: dict[str, int],
    k: int = 10,
) -> float:
    """Compute Precision@k."""
    if k == 0:
        return 0.0
    retrieved_relevant = sum(
        1 for doc_id in ranked_ids[:k] if doc_id in relevant
    )
    return retrieved_relevant / k


def mrr(
    ranked_ids: list[str],
    relevant: dict[str, int],
) -> float:
    """Compute Mean Reciprocal Rank."""
    for i, doc_id in enumerate(ranked_ids):
        if doc_id in relevant:
            return 1.0 / (i + 1)
    return 0.0


# ── Build index ───────────────────────────────────────────


def build_bm25_from_corpus(
    corpus: dict[str, dict],
) -> tuple[BM25Index, dict[str, int]]:
    """Build BM25Index from BEIR corpus.

    Returns (index, doc_id_to_idx mapping).
    """
    bm25 = BM25Index()
    texts = []
    doc_ids = []
    for doc_id, doc in corpus.items():
        title = doc.get("title", "")
        text = doc.get("text", "")
        combined = f"{title} {text}".strip()
        texts.append(combined)
        doc_ids.append(doc_id)

    bm25.build(texts)
    # Store doc_ids for mapping back
    bm25._beir_doc_ids = doc_ids  # noqa: SLF001
    return bm25


def search_bm25(
    bm25: BM25Index,
    query: str,
    top_k: int = 100,
) -> list[tuple[str, float]]:
    """Search BM25 and return (doc_id, score) pairs."""
    results = bm25.query(query, top_k=top_k)
    doc_ids = bm25._beir_doc_ids  # noqa: SLF001
    out = []
    for r in results:
        # Map file index back to doc_id
        # BM25Index stores file as the text itself, need idx
        idx = r.metadata.get("idx", -1)
        if 0 <= idx < len(doc_ids):
            out.append((doc_ids[idx], r.score))
    return out


def build_bm25_with_ids(
    corpus: dict[str, dict],
) -> tuple[BM25Index, list[str]]:
    """Build BM25 with doc ID tracking via metadata."""
    bm25 = BM25Index()
    documents = []
    doc_ids = []
    for doc_id, doc in corpus.items():
        title = doc.get("title", "")
        text = doc.get("text", "")
        documents.append({
            "text": f"{title} {text}".strip(),
            "file": doc_id,
            "heading": title,
        })
        doc_ids.append(doc_id)

    bm25.build(documents)
    return bm25, doc_ids


def search_bm25_by_ids(
    bm25: BM25Index,
    doc_ids: list[str],
    query: str,
    top_k: int = 100,
) -> list[tuple[str, float]]:
    """BM25 search returning (doc_id, score)."""
    if not bm25.is_ready:
        return []
    results = bm25.query(query, top_k=top_k)
    return [
        (r.file, r.score) for r in results if r.file
    ]


# ── Dense search (ChromaDB + batch embed) ────────────────


class DenseIndex:
    """Direct ChromaDB dense index — bypasses RAGIndex file overhead."""

    def __init__(self, cache_dir: str, model_name: str = "BAAI/bge-m3"):
        self.cache_dir = cache_dir
        self.model_name = model_name
        self._model = None
        self._collection = None
        self._client = None
        self.doc_ids: list[str] = []

    def _get_model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            import torch
            _dev = os.environ.get("BEIR_DEVICE", "cpu")
            if _dev == "auto":
                _dev = "cuda" if torch.cuda.is_available() else "cpu"
            self._model = SentenceTransformer(self.model_name, device=_dev)
        return self._model

    def _get_client(self):
        if self._client is None:
            import chromadb
            db_path = os.path.join(self.cache_dir, "dense_db")
            os.makedirs(db_path, exist_ok=True)
            self._client = chromadb.PersistentClient(path=db_path)
        return self._client

    def build(self, corpus: dict[str, dict], force: bool = False):
        """Build dense index from BEIR corpus with batch embedding."""
        client = self._get_client()

        # Check if already built or partially built (supports resume)
        existing_ids: set[str] = set()
        try:
            col = client.get_collection("beir_dense")
            count = col.count()
            if count >= len(corpus) and not force:
                self._collection = col
                self.doc_ids = list(corpus.keys())
                print(f"  Dense index cached ({count} vectors)")
                return
            if force:
                client.delete_collection("beir_dense")
            else:
                # Resume from partial build
                self._collection = col
                existing_ids = set(
                    col.get(limit=count, include=[])["ids"]
                )
                print(f"  Resuming from {len(existing_ids)}/{len(corpus)}")
        except Exception:
            pass

        if self._collection is None:
            self._collection = client.create_collection(
                name="beir_dense",
                metadata={"hnsw:space": "cosine"},
            )

        model = self._get_model()
        self.doc_ids = []
        texts = []
        skip_ids: set[str] = set()
        for doc_id, doc in corpus.items():
            title = doc.get("title", "")
            text = doc.get("text", "")
            self.doc_ids.append(doc_id)
            if doc_id in existing_ids:
                skip_ids.add(doc_id)
                continue
            texts.append((doc_id, f"{title} {text}".strip()))

        if not texts:
            print(f"  All {len(corpus)} docs already indexed")
            return

        print(f"  Skipped {len(skip_ids)}, embedding {len(texts)} remaining")

        # Batch encode + insert
        batch_size = 128
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            batch_ids = [t[0] for t in batch]
            batch_texts = [t[1] for t in batch]
            embeddings = model.encode(
                batch_texts, show_progress_bar=False,
                batch_size=32,
            ).tolist()
            self._collection.add(
                ids=batch_ids,
                embeddings=embeddings,
                documents=batch_texts,
            )
            done = len(skip_ids) + min(i + batch_size, len(texts))
            print(f"  Embedded {done}/{len(corpus)}", flush=True)

        print(f"  Total: {self._collection.count()} vectors indexed")

    def search(self, query: str, top_k: int = 100) -> list[tuple[str, float]]:
        """Search returning (doc_id, score) sorted by relevance."""
        if self._collection is None:
            return []
        model = self._get_model()
        q_emb = model.encode([query], show_progress_bar=False).tolist()
        results = self._collection.query(
            query_embeddings=q_emb,
            n_results=min(top_k, self._collection.count()),
        )
        out = []
        if results and results["ids"]:
            ids = results["ids"][0]
            distances = results["distances"][0] if results.get("distances") else []
            for j, doc_id in enumerate(ids):
                # ChromaDB cosine distance → similarity
                score = 1.0 - distances[j] if j < len(distances) else 0.0
                out.append((doc_id, score))
        return out


# ── Hybrid fusion ─────────────────────────────────────────

# Fusion strategies — selectable via --fusion flag
FUSION_STRATEGIES = [
    "rrf",           # Original: rank-only, ignores scores
    "rrf_agree",     # RRF + agreement boost (太極: paths confirm each other)
    "score_norm",    # Min-max normalized score fusion (preserves magnitude)
    "tension",       # Tension-Adaptive: R=T/M, high disagreement = explore more
]


def reciprocal_rank_fusion(
    result_lists: list[list[tuple[str, float]]],
    k: int = 60,
) -> list[tuple[str, float]]:
    """RRF: combine multiple ranked lists."""
    scores: dict[str, float] = defaultdict(float)
    for results in result_lists:
        for rank, (doc_id, _score) in enumerate(results):
            scores[doc_id] += 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def rrf_with_agreement_boost(
    result_lists: list[list[tuple[str, float]]],
    k: int = 60,
    boost: float = 1.5,
) -> list[tuple[str, float]]:
    """RRF + Agreement Boost.

    Insight (太極陰陽): When BM25 (keyword/陰) and Dense (semantic/陽)
    BOTH rank a document highly, that agreement is a strong signal.
    Documents only one path finds are weaker candidates.
    """
    scores: dict[str, float] = defaultdict(float)
    presence: dict[str, int] = defaultdict(int)

    for results in result_lists:
        seen_in_this = set()
        for rank, (doc_id, _score) in enumerate(results):
            scores[doc_id] += 1.0 / (k + rank + 1)
            if doc_id not in seen_in_this:
                presence[doc_id] += 1
                seen_in_this.add(doc_id)

    # Agreement boost: docs found by ALL paths get multiplied
    n_paths = len(result_lists)
    for doc_id in scores:
        if presence[doc_id] >= n_paths:
            scores[doc_id] *= boost

    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def score_normalized_fusion(
    result_lists: list[list[tuple[str, float]]],
    weights: list[float] | None = None,
) -> list[tuple[str, float]]:
    """Score-based fusion with min-max normalization.

    Unlike RRF which discards scores, this preserves HOW MUCH each path
    thinks a document is relevant. A BM25 score of 25.0 vs 3.0 matters.
    """
    if weights is None:
        weights = [1.0] * len(result_lists)

    scores: dict[str, float] = defaultdict(float)

    for w, results in zip(weights, result_lists):
        if not results:
            continue
        raw_scores = [s for _, s in results]
        mn, mx = min(raw_scores), max(raw_scores)
        rng = mx - mn if mx > mn else 1.0

        for doc_id, score in results:
            norm = (score - mn) / rng  # 0..1
            scores[doc_id] += w * norm

    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def tension_adaptive_fusion(
    result_lists: list[list[tuple[str, float]]],
    k: int = 60,
    tension_threshold: float = 0.3,
) -> list[tuple[str, float]]:
    """Tension-Adaptive Fusion (意識張力論 R=T/M).

    When paths AGREE (low tension) → trust them, be decisive (lower k).
    When paths DISAGREE (high tension) → be inclusive, explore more (higher k).

    This is dynamic balance (太極): the system self-adjusts per query.
    """
    if len(result_lists) < 2:
        return reciprocal_rank_fusion(result_lists, k=k)

    # Measure tension: overlap in top-20 between paths
    top_sets = []
    for results in result_lists:
        top_sets.append({doc_id for doc_id, _ in results[:20]})

    # Jaccard similarity of top-20
    intersection = top_sets[0]
    union = top_sets[0]
    for s in top_sets[1:]:
        intersection = intersection & s
        union = union | s
    agreement = len(intersection) / len(union) if union else 0.0
    tension = 1.0 - agreement  # high tension = high disagreement

    # Adaptive k: low tension → k=30 (decisive), high → k=90 (inclusive)
    adaptive_k = int(30 + 60 * tension)

    # Adaptive boost: high agreement → strong boost, low → no boost
    boost = 1.0 + (1.0 - tension) * 0.8  # 1.0..1.8

    scores: dict[str, float] = defaultdict(float)
    presence: dict[str, int] = defaultdict(int)

    for results in result_lists:
        seen = set()
        for rank, (doc_id, _score) in enumerate(results):
            scores[doc_id] += 1.0 / (adaptive_k + rank + 1)
            if doc_id not in seen:
                presence[doc_id] += 1
                seen.add(doc_id)

    n_paths = len(result_lists)
    for doc_id in scores:
        if presence[doc_id] >= n_paths:
            scores[doc_id] *= boost

    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def fuse(
    strategy: str,
    result_lists: list[list[tuple[str, float]]],
    **kwargs,
) -> list[tuple[str, float]]:
    """Dispatch to the selected fusion strategy."""
    if strategy == "rrf":
        return reciprocal_rank_fusion(result_lists, k=kwargs.get("k", 60))
    elif strategy == "rrf_agree":
        return rrf_with_agreement_boost(
            result_lists, k=kwargs.get("k", 60),
            boost=kwargs.get("boost", 1.5),
        )
    elif strategy == "score_norm":
        return score_normalized_fusion(
            result_lists, weights=kwargs.get("weights"),
        )
    elif strategy == "tension":
        return tension_adaptive_fusion(
            result_lists, k=kwargs.get("k", 60),
        )
    else:
        return reciprocal_rank_fusion(result_lists, k=kwargs.get("k", 60))


# ── Main runner ───────────────────────────────────────────


def run_beir(
    dataset_name: str = "scifact",
    profile: str = "precision",
    bm25_only: bool = False,
    dense_only: bool = False,
    top_k: int = 100,
    use_reranker: bool = True,
    use_noise_guard: bool = True,
    output_dir: str = "",
    reranker_model: str = "",
    dense_model: str = "",
    fusion_strategy: str = "rrf",
):
    """Run BEIR benchmark."""
    # Download dataset
    base_dir = os.path.join(
        os.path.dirname(__file__), "datasets"
    )
    if dataset_name in DATASET_URLS:
        data_path = util.download_and_unzip(
            DATASET_URLS[dataset_name], base_dir
        )
    else:
        data_path = os.path.join(base_dir, dataset_name)

    corpus, queries, qrels = GenericDataLoader(
        data_folder=data_path
    ).load(split="test")

    print(f"Dataset: {dataset_name}")
    print(f"  Corpus: {len(corpus)} docs")
    print(f"  Queries: {len(queries)}")
    print(f"  Profile: {profile}")
    mode = "hybrid"
    if bm25_only:
        mode = "bm25_only"
    elif dense_only:
        mode = "dense_only"
    print(f"  Mode: {mode}")

    # Build indices
    print("Building BM25 index...")
    t0 = time.time()
    bm25, doc_ids = build_bm25_with_ids(corpus)
    bm25_time = time.time() - t0
    print(f"  BM25 built in {bm25_time:.1f}s ({len(doc_ids)} docs)")

    dense = None
    if not bm25_only:
        actual_dense = dense_model or "BAAI/bge-m3"
        print(f"Building dense index ({actual_dense})...")
        t0 = time.time()
        if actual_dense == "BAAI/bge-m3":
            cache_dir = os.path.join(base_dir, f".cache_{dataset_name}")
        else:
            model_tag = actual_dense.replace("/", "_")
            cache_dir = os.path.join(base_dir, f".cache_{dataset_name}_{model_tag}")
        try:
            dense = DenseIndex(cache_dir, model_name=actual_dense)
            dense.build(corpus)
            dense_time = time.time() - t0
            print(f"  Dense built in {dense_time:.1f}s")
        except Exception as e:
            print(f"  Dense failed: {e}")
            dense = None
            if dense_only:
                print("ERROR: --dense-only but no dense index")
                return

    # Profile config
    prof_enum = RetrievalProfile(profile)
    _pcfg = PROFILE_CONFIG[prof_enum]

    # Init cross-encoder reranker
    ce_model = None
    if use_reranker:
        reranker_name = reranker_model or "cross-encoder/ms-marco-MiniLM-L-6-v2"
        print(f"Loading reranker: {reranker_name}...")
        try:
            from sentence_transformers import CrossEncoder
            import torch as _torch
            _dev = os.environ.get("BEIR_DEVICE", "cpu")
            if _dev == "auto":
                _dev = "cuda" if _torch.cuda.is_available() else "cpu"
            ce_model = CrossEncoder(reranker_name, max_length=512, device=_dev)
            print("  Reranker loaded")
        except Exception as e:
            print(f"  Reranker unavailable: {e}")

    # Run queries
    print(f"\nRunning {len(queries)} queries...")
    all_ndcg10 = []
    all_recall100 = []
    all_precision10 = []
    all_mrr = []
    total_time = 0

    for qi, (qid, query_text) in enumerate(queries.items()):
        relevant = {
            doc_id: rel
            for doc_id, rel in qrels.get(qid, {}).items()
            if rel > 0
        }

        t0 = time.time()

        # Search
        bm25_results = []
        dense_results = []

        if not dense_only:
            bm25_results = search_bm25_by_ids(
                bm25, doc_ids, query_text, top_k=top_k
            )
        if not bm25_only and dense:
            dense_results = dense.search(query_text, top_k=top_k)

        # Fusion
        if bm25_results and dense_results:
            fused = fuse(fusion_strategy, [bm25_results, dense_results])
        elif bm25_results:
            fused = bm25_results
        else:
            fused = dense_results

        # Cross-encoder rerank top candidates
        if ce_model is not None and fused:
            rerank_k = min(50, len(fused))
            top_cands = fused[:rerank_k]
            pairs = [
                (query_text, corpus[did].get("title", "")
                 + " " + corpus[did].get("text", ""))
                for did, _ in top_cands
                if did in corpus
            ]
            if pairs:
                ce_scores = ce_model.predict(pairs)
                reranked = sorted(
                    zip(
                        [did for did, _ in top_cands[:len(pairs)]],
                        ce_scores,
                    ),
                    key=lambda x: x[1],
                    reverse=True,
                )
                reranked_ids = {did for did, _ in reranked}
                rest = [
                    (did, s) for did, s in fused[rerank_k:]
                    if did not in reranked_ids
                ]
                fused = [(did, float(s)) for did, s in reranked] + rest

        query_time = time.time() - t0
        total_time += query_time

        # Extract ranked doc IDs
        ranked = [doc_id for doc_id, _score in fused[:top_k]]

        # Compute metrics
        n10 = ndcg_at_k(ranked, relevant, k=10)
        r100 = recall_at_k(ranked, relevant, k=100)
        p10 = precision_at_k(ranked, relevant, k=10)
        m = mrr(ranked, relevant)

        all_ndcg10.append(n10)
        all_recall100.append(r100)
        all_precision10.append(p10)
        all_mrr.append(m)

        if (qi + 1) % 50 == 0 or qi == 0:
            print(
                f"  [{qi+1}/{len(queries)}]"
                f" nDCG@10={n10:.3f}"
                f" R@100={r100:.3f}"
                f" ({query_time*1000:.0f}ms)"
            )

    # Aggregate
    avg_ndcg10 = sum(all_ndcg10) / len(all_ndcg10)
    avg_recall100 = sum(all_recall100) / len(all_recall100)
    avg_precision10 = sum(all_precision10) / len(all_precision10)
    avg_mrr = sum(all_mrr) / len(all_mrr)
    avg_latency = total_time / len(queries) * 1000

    # Output
    if not output_dir:
        output_dir = os.path.join(os.path.dirname(__file__), "results")
    os.makedirs(output_dir, exist_ok=True)

    timestamp = time.strftime("%Y%m%d_%H%M")
    tag = f"{dataset_name}_{profile}_{mode}"
    results_path = os.path.join(
        output_dir, f"beir_{tag}_{timestamp}.json"
    )
    summary_path = os.path.join(
        output_dir, f"beir_{tag}_{timestamp}_summary.md"
    )

    actual_dense_name = dense_model or "BAAI/bge-m3"
    actual_reranker_name = reranker_model or "cross-encoder/ms-marco-MiniLM-L-6-v2"

    result_data = {
        "dataset": dataset_name,
        "profile": profile,
        "mode": mode,
        "corpus_size": len(corpus),
        "num_queries": len(queries),
        "metrics": {
            "nDCG@10": round(avg_ndcg10, 4),
            "Recall@100": round(avg_recall100, 4),
            "Precision@10": round(avg_precision10, 4),
            "MRR": round(avg_mrr, 4),
        },
        "avg_latency_ms": round(avg_latency, 1),
        "dense_model": actual_dense_name if not bm25_only else None,
        "reranker_model": actual_reranker_name if use_reranker else None,
        "fusion_strategy": fusion_strategy,
        "use_reranker": use_reranker,
        "use_noise_guard": use_noise_guard,
        "timestamp": timestamp,
    }

    with open(results_path, "w") as f:
        json.dump(result_data, f, indent=2)

    summary_lines = [
        f"# BEIR {dataset_name} — {profile} ({mode})"
        f" | {time.strftime('%Y-%m-%d %H:%M')}",
        "",
        "## Team: AI King | System: CCC STAR v3.5",
        "",
        "| Metric | Score |",
        "|--------|-------|",
        f"| **nDCG@10** | **{avg_ndcg10:.4f}** |",
        f"| Recall@100 | {avg_recall100:.4f} |",
        f"| Precision@10 | {avg_precision10:.4f} |",
        f"| MRR | {avg_mrr:.4f} |",
        f"| Avg Latency | {avg_latency:.1f} ms |",
        "",
        "## Config",
        "",
        f"- Profile: {profile}",
        f"- Mode: {mode}",
        f"- Reranker: {use_reranker}",
        f"- Noise Guard: {use_noise_guard}",
        f"- Corpus: {len(corpus)} docs",
        f"- Queries: {len(queries)}",
    ]

    with open(summary_path, "w") as f:
        f.write("\n".join(summary_lines) + "\n")

    print(f"\n{'='*60}")
    print(f"BEIR {dataset_name} — {profile} ({mode})")
    print(f"{'='*60}")
    print(f"  nDCG@10:      {avg_ndcg10:.4f}")
    print(f"  Recall@100:   {avg_recall100:.4f}")
    print(f"  Precision@10: {avg_precision10:.4f}")
    print(f"  MRR:          {avg_mrr:.4f}")
    print(f"  Avg Latency:  {avg_latency:.1f} ms")
    print(f"\nResults: {results_path}")
    print(f"Summary: {summary_path}")

    return result_data


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="BEIR Runner")
    parser.add_argument(
        "--dataset", default="scifact",
        choices=["scifact", "nfcorpus", "fiqa"],
    )
    parser.add_argument(
        "--profile", default="precision",
        choices=["precision", "recall", "balanced"],
    )
    parser.add_argument("--bm25-only", action="store_true")
    parser.add_argument("--dense-only", action="store_true")
    parser.add_argument("--top-k", type=int, default=100)
    parser.add_argument("--no-reranker", action="store_true")
    parser.add_argument("--no-noise-guard", action="store_true")
    parser.add_argument("--reranker-model", default="")
    parser.add_argument("--dense-model", default="")
    parser.add_argument(
        "--fusion", default="rrf",
        choices=FUSION_STRATEGIES,
        help="Fusion strategy: rrf, rrf_agree, score_norm, tension",
    )
    args = parser.parse_args()

    run_beir(
        dataset_name=args.dataset,
        profile=args.profile,
        bm25_only=args.bm25_only,
        dense_only=args.dense_only,
        top_k=args.top_k,
        use_reranker=not args.no_reranker,
        use_noise_guard=not args.no_noise_guard,
        reranker_model=args.reranker_model,
        dense_model=args.dense_model,
        fusion_strategy=args.fusion,
    )
