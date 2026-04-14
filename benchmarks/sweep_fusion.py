"""Sweep RRF k-values and BM25/Dense weights on cached indices.

This runs ONLY the fusion + evaluation (no re-embedding).
Requires that SciFact BM25 and Dense indices are already built.

Usage:
    python benchmarks/sweep_fusion.py
"""

from __future__ import annotations

import json
import os
import time
from collections import defaultdict

from beir.datasets.data_loader import GenericDataLoader

from beir_runner import (
    DenseIndex,
    build_bm25_with_ids,
    ndcg_at_k,
    search_bm25_by_ids,
)

BASELINE_NDCG = 0.7267
SOTA_NDCG = 0.7370


def weighted_rrf(
    bm25_results: list[tuple[str, float]],
    dense_results: list[tuple[str, float]],
    k: int = 60,
    w_bm25: float = 1.0,
    w_dense: float = 1.0,
) -> list[tuple[str, float]]:
    """Weighted RRF: different weights for BM25 vs Dense."""
    scores: dict[str, float] = defaultdict(float)
    for rank, (doc_id, _) in enumerate(bm25_results):
        scores[doc_id] += w_bm25 / (k + rank + 1)
    for rank, (doc_id, _) in enumerate(dense_results):
        scores[doc_id] += w_dense / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def run_sweep() -> None:
    base_dir = os.path.join(os.path.dirname(__file__), "datasets")
    data_path = os.path.join(base_dir, "scifact")

    corpus, queries, qrels = GenericDataLoader(
        data_folder=data_path,
    ).load(split="test")

    print(f"Loaded: {len(corpus)} docs, {len(queries)} queries")

    # Build BM25
    print("Building BM25...")
    bm25, doc_ids = build_bm25_with_ids(corpus)

    # Load cached Dense
    print("Loading cached Dense (bge-m3)...")
    cache_dir = os.path.join(base_dir, ".cache_scifact")
    os.environ["BEIR_DEVICE"] = "cpu"
    dense = DenseIndex(cache_dir, model_name="BAAI/bge-m3")
    dense.build(corpus)

    # ── Sweep parameters ──
    k_values = [1, 5, 10, 20, 30, 40, 50, 60, 80, 100, 200]
    weight_combos = [
        (1.0, 1.0, "equal"),
        (1.5, 1.0, "bm25_heavy"),
        (1.0, 1.5, "dense_heavy"),
        (2.0, 1.0, "bm25_2x"),
        (1.0, 2.0, "dense_2x"),
        (1.0, 3.0, "dense_3x"),
        (3.0, 1.0, "bm25_3x"),
        (0.5, 1.0, "bm25_half"),
        (1.0, 0.5, "dense_half"),
    ]

    results: list[dict] = []

    # Phase 1: k-sweep with equal weights
    print("\n=== Phase 1: k-value sweep (equal weights) ===")
    for k in k_values:
        ndcgs = []
        for qid, query_text in queries.items():
            relevant = {
                did: rel
                for did, rel in qrels.get(qid, {}).items()
                if rel > 0
            }
            bm25_r = search_bm25_by_ids(bm25, doc_ids, query_text, top_k=100)
            dense_r = dense.search(query_text, top_k=100)
            fused = weighted_rrf(bm25_r, dense_r, k=k)
            ranked = [did for did, _ in fused[:100]]
            ndcgs.append(ndcg_at_k(ranked, relevant, k=10))

        avg = sum(ndcgs) / len(ndcgs)
        results.append({
            "k": k,
            "w_bm25": 1.0,
            "w_dense": 1.0,
            "label": f"k={k}",
            "ndcg10": round(avg, 4),
        })
        marker = " ***" if avg > BASELINE_NDCG else ""
        print(f"  k={k:>3d}  nDCG@10={avg:.4f}{marker}")

    # Find best k
    best_k_entry = max(results, key=lambda x: x["ndcg10"])
    best_k = best_k_entry["k"]
    print(f"\n  Best k={best_k} -> {best_k_entry['ndcg10']}")

    # Phase 2: weight sweep with best k
    print(f"\n=== Phase 2: weight sweep (k={best_k}) ===")
    for w_bm25, w_dense, label in weight_combos:
        ndcgs = []
        for qid, query_text in queries.items():
            relevant = {
                did: rel
                for did, rel in qrels.get(qid, {}).items()
                if rel > 0
            }
            bm25_r = search_bm25_by_ids(bm25, doc_ids, query_text, top_k=100)
            dense_r = dense.search(query_text, top_k=100)
            fused = weighted_rrf(
                bm25_r, dense_r, k=best_k,
                w_bm25=w_bm25, w_dense=w_dense,
            )
            ranked = [did for did, _ in fused[:100]]
            ndcgs.append(ndcg_at_k(ranked, relevant, k=10))

        avg = sum(ndcgs) / len(ndcgs)
        results.append({
            "k": best_k,
            "w_bm25": w_bm25,
            "w_dense": w_dense,
            "label": label,
            "ndcg10": round(avg, 4),
        })
        marker = " ***" if avg > BASELINE_NDCG else ""
        print(
            f"  {label:>12s} (bm25={w_bm25}, dense={w_dense})"
            f"  nDCG@10={avg:.4f}{marker}"
        )

    best_overall = max(results, key=lambda x: x["ndcg10"])
    print(
        f"\n=== Best overall: {best_overall['label']}"
        f" nDCG@10={best_overall['ndcg10']} ==="
    )

    # Save results
    out_path = os.path.join(
        os.path.dirname(__file__), "results",
        f"sweep_fusion_{time.strftime('%Y%m%d_%H%M')}.json",
    )
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved: {out_path}")

    # SOTA comparison
    sep = "=" * 60
    print(f"\n{sep}")
    print(f"  Current best (RRF k=60): {BASELINE_NDCG}")
    print(f"  Sweep best:              {best_overall['ndcg10']}")
    print(f"  SOTA (E5-PT_base):       {SOTA_NDCG}")
    print(f"  Gap to SOTA:             {SOTA_NDCG - best_overall['ndcg10']:.4f}")
    print(sep)


if __name__ == "__main__":
    run_sweep()
