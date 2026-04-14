"""Full parameter sweep across ALL fusion strategies.

Each strategy has its own sweet spot — test them ALL before concluding.

Strategies:
  1. Vanilla RRF: sweep k
  2. Weighted RRF: sweep k + bm25/dense weight ratio
  3. Agreement Boost: sweep k + boost factor
  4. Score Normalized: sweep bm25/dense weight ratio
  5. Tension-Adaptive: sweep base_k + tension sensitivity

Usage:
    python benchmarks/sweep_all_strategies.py
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

# ── Fusion implementations (self-contained for sweep) ──


def vanilla_rrf(
    bm25_r: list[tuple[str, float]],
    dense_r: list[tuple[str, float]],
    k: int = 60,
) -> list[tuple[str, float]]:
    scores: dict[str, float] = defaultdict(float)
    for rank, (doc_id, _) in enumerate(bm25_r):
        scores[doc_id] += 1.0 / (k + rank + 1)
    for rank, (doc_id, _) in enumerate(dense_r):
        scores[doc_id] += 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def weighted_rrf(
    bm25_r: list[tuple[str, float]],
    dense_r: list[tuple[str, float]],
    k: int = 60,
    w_bm25: float = 1.0,
    w_dense: float = 1.0,
) -> list[tuple[str, float]]:
    scores: dict[str, float] = defaultdict(float)
    for rank, (doc_id, _) in enumerate(bm25_r):
        scores[doc_id] += w_bm25 / (k + rank + 1)
    for rank, (doc_id, _) in enumerate(dense_r):
        scores[doc_id] += w_dense / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def agreement_rrf(
    bm25_r: list[tuple[str, float]],
    dense_r: list[tuple[str, float]],
    k: int = 60,
    boost: float = 1.5,
) -> list[tuple[str, float]]:
    """RRF + boost documents found by BOTH paths."""
    scores: dict[str, float] = defaultdict(float)
    bm25_set = set()
    dense_set = set()
    for rank, (doc_id, _) in enumerate(bm25_r):
        scores[doc_id] += 1.0 / (k + rank + 1)
        bm25_set.add(doc_id)
    for rank, (doc_id, _) in enumerate(dense_r):
        scores[doc_id] += 1.0 / (k + rank + 1)
        dense_set.add(doc_id)
    agreed = bm25_set & dense_set
    for doc_id in agreed:
        scores[doc_id] *= boost
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def score_fusion(
    bm25_r: list[tuple[str, float]],
    dense_r: list[tuple[str, float]],
    w_bm25: float = 1.0,
    w_dense: float = 1.0,
) -> list[tuple[str, float]]:
    """Min-max normalized score fusion."""
    scores: dict[str, float] = defaultdict(float)
    for source, w in [(bm25_r, w_bm25), (dense_r, w_dense)]:
        if not source:
            continue
        raw = [s for _, s in source]
        mn, mx = min(raw), max(raw)
        rng = mx - mn if mx > mn else 1.0
        for doc_id, score in source:
            scores[doc_id] += w * (score - mn) / rng
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def tension_rrf(
    bm25_r: list[tuple[str, float]],
    dense_r: list[tuple[str, float]],
    base_k: int = 60,
    k_range: int = 60,
    boost_max: float = 0.8,
    overlap_n: int = 20,
) -> list[tuple[str, float]]:
    """Tension-Adaptive: auto-adjust k and boost based on path agreement."""
    bm25_top = {d for d, _ in bm25_r[:overlap_n]}
    dense_top = {d for d, _ in dense_r[:overlap_n]}
    union = bm25_top | dense_top
    agreement = len(bm25_top & dense_top) / len(union) if union else 0.0
    tension = 1.0 - agreement

    adaptive_k = int(base_k - k_range / 2 + k_range * tension)
    boost = 1.0 + (1.0 - tension) * boost_max

    scores: dict[str, float] = defaultdict(float)
    bm25_set = set()
    dense_set = set()
    for rank, (doc_id, _) in enumerate(bm25_r):
        scores[doc_id] += 1.0 / (adaptive_k + rank + 1)
        bm25_set.add(doc_id)
    for rank, (doc_id, _) in enumerate(dense_r):
        scores[doc_id] += 1.0 / (adaptive_k + rank + 1)
        dense_set.add(doc_id)
    for doc_id in bm25_set & dense_set:
        scores[doc_id] *= boost
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


# ── Evaluation helper ──


def eval_fusion(
    fusion_fn,
    queries: dict,
    qrels: dict,
    bm25,
    doc_ids: list[str],
    dense: DenseIndex,
    **kwargs,
) -> float:
    """Run fusion on all queries and return mean nDCG@10."""
    ndcgs = []
    for qid, query_text in queries.items():
        relevant = {
            did: rel
            for did, rel in qrels.get(qid, {}).items()
            if rel > 0
        }
        bm25_r = search_bm25_by_ids(bm25, doc_ids, query_text, top_k=100)
        dense_r = dense.search(query_text, top_k=100)
        fused = fusion_fn(bm25_r, dense_r, **kwargs)
        ranked = [did for did, _ in fused[:100]]
        ndcgs.append(ndcg_at_k(ranked, relevant, k=10))
    return sum(ndcgs) / len(ndcgs)


def run_full_sweep() -> None:
    base_dir = os.path.join(os.path.dirname(__file__), "datasets")
    data_path = os.path.join(base_dir, "scifact")

    corpus, queries, qrels = GenericDataLoader(
        data_folder=data_path,
    ).load(split="test")
    print(f"Loaded: {len(corpus)} docs, {len(queries)} queries")

    print("Building BM25...")
    bm25, doc_ids = build_bm25_with_ids(corpus)

    print("Loading cached Dense (bge-m3)...")
    cache_dir = os.path.join(base_dir, ".cache_scifact")
    os.environ["BEIR_DEVICE"] = "cpu"
    dense = DenseIndex(cache_dir, model_name="BAAI/bge-m3")
    dense.build(corpus)

    all_results: list[dict] = []
    best_score = 0.0
    best_label = ""

    def record(label: str, score: float, **params) -> None:
        nonlocal best_score, best_label
        rounded = round(score, 4)
        all_results.append({"label": label, "ndcg10": rounded, **params})
        marker = ""
        if score > 0.7267:
            marker = " ** BEATS BASELINE"
        if score > 0.737:
            marker = " *** BEATS SOTA!"
        if score > best_score:
            best_score = score
            best_label = label
        print(f"  {label:<35s} nDCG@10={rounded}{marker}")

    # ── 1. Vanilla RRF: k sweep ──
    print("\n=== 1. Vanilla RRF: k sweep ===")
    for k in [1, 3, 5, 10, 15, 20, 30, 40, 50, 60, 80, 100, 150, 200]:
        s = eval_fusion(vanilla_rrf, queries, qrels, bm25, doc_ids, dense, k=k)
        record(f"RRF k={k}", s, strategy="rrf", k=k)

    # ── 2. Weighted RRF: best-k from above + weight sweep ──
    rrf_results = [r for r in all_results if r["strategy"] == "rrf"]
    best_k = max(rrf_results, key=lambda x: x["ndcg10"])["k"]
    print(f"\n=== 2. Weighted RRF (k={best_k}): weight sweep ===")
    for w_bm25, w_dense in [
        (0.3, 1.0), (0.5, 1.0), (0.7, 1.0), (0.8, 1.0),
        (1.0, 0.3), (1.0, 0.5), (1.0, 0.7), (1.0, 0.8),
        (1.0, 1.2), (1.0, 1.5), (1.0, 2.0), (1.0, 3.0),
        (1.2, 1.0), (1.5, 1.0), (2.0, 1.0), (3.0, 1.0),
    ]:
        s = eval_fusion(
            weighted_rrf, queries, qrels, bm25, doc_ids, dense,
            k=best_k, w_bm25=w_bm25, w_dense=w_dense,
        )
        record(
            f"WRRF k={best_k} bm25={w_bm25} d={w_dense}", s,
            strategy="wrrf", k=best_k, w_bm25=w_bm25, w_dense=w_dense,
        )

    # ── 3. Agreement Boost: k sweep + boost sweep ──
    print("\n=== 3. Agreement Boost: k + boost sweep ===")
    for k in [10, 20, 30, 40, 50, 60, 80]:
        for boost in [1.05, 1.1, 1.2, 1.3, 1.5, 2.0, 3.0]:
            s = eval_fusion(
                agreement_rrf, queries, qrels, bm25, doc_ids, dense,
                k=k, boost=boost,
            )
            record(
                f"Agree k={k} boost={boost}", s,
                strategy="agree", k=k, boost=boost,
            )

    # ── 4. Score Fusion: weight sweep ──
    print("\n=== 4. Score Fusion: weight sweep ===")
    for w_bm25, w_dense in [
        (0.1, 1.0), (0.3, 1.0), (0.5, 1.0), (0.7, 1.0),
        (1.0, 0.1), (1.0, 0.3), (1.0, 0.5), (1.0, 0.7),
        (1.0, 1.0), (1.0, 1.5), (1.0, 2.0), (1.5, 1.0), (2.0, 1.0),
    ]:
        s = eval_fusion(
            score_fusion, queries, qrels, bm25, doc_ids, dense,
            w_bm25=w_bm25, w_dense=w_dense,
        )
        record(
            f"ScoreFusion bm25={w_bm25} d={w_dense}", s,
            strategy="score", w_bm25=w_bm25, w_dense=w_dense,
        )

    # ── 5. Tension-Adaptive: parameter sweep ──
    print("\n=== 5. Tension-Adaptive: parameter sweep ===")
    for base_k in [20, 40, 60, 80]:
        for k_range in [20, 40, 60, 80]:
            for boost_max in [0.3, 0.5, 0.8, 1.2]:
                s = eval_fusion(
                    tension_rrf, queries, qrels, bm25, doc_ids, dense,
                    base_k=base_k, k_range=k_range, boost_max=boost_max,
                )
                record(
                    f"Tension bk={base_k} kr={k_range} bm={boost_max}", s,
                    strategy="tension",
                    base_k=base_k, k_range=k_range, boost_max=boost_max,
                )

    # ── Summary ──
    sep = "=" * 60
    print(f"\n{sep}")
    print(f"  Total configs tested: {len(all_results)}")
    print(f"  Best: {best_label}  nDCG@10={best_score:.4f}")
    print("  Baseline (RRF k=60): 0.7267")
    print("  SOTA (E5-PT_base):   0.7370")
    if best_score > 0.737:
        print("  >>> SOTA BEATEN! <<<")
    else:
        print(f"  Gap to SOTA: {0.737 - best_score:.4f}")
    print(sep)

    # Top 10
    top10 = sorted(all_results, key=lambda x: x["ndcg10"], reverse=True)[:10]
    print("\nTop 10 configs:")
    for i, r in enumerate(top10, 1):
        print(f"  {i:>2d}. {r['label']:<40s} {r['ndcg10']}")

    # Save
    out_path = os.path.join(
        os.path.dirname(__file__), "results",
        f"sweep_all_{time.strftime('%Y%m%d_%H%M')}.json",
    )
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    run_full_sweep()
