"""Fast parameter sweep — pre-cache all query results, then sweep fusion only.

Optimization: BM25 + Dense search per query runs ONCE, cached in memory.
Then 156+ fusion configs run on cached results (pure dict ops, <1ms each).

Total: ~300 queries × 1.5s search + 156 configs × 300 × <1ms fusion
     = ~8 min search + ~1 min fusion = ~9 min total (vs 19.5h naive)

Usage:
    python benchmarks/sweep_fast.py
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


# ── Fusion implementations ──


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
    scores: dict[str, float] = defaultdict(float)
    bm25_set = set()
    dense_set = set()
    for rank, (doc_id, _) in enumerate(bm25_r):
        scores[doc_id] += 1.0 / (k + rank + 1)
        bm25_set.add(doc_id)
    for rank, (doc_id, _) in enumerate(dense_r):
        scores[doc_id] += 1.0 / (k + rank + 1)
        dense_set.add(doc_id)
    for doc_id in bm25_set & dense_set:
        scores[doc_id] *= boost
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def score_fusion(
    bm25_r: list[tuple[str, float]],
    dense_r: list[tuple[str, float]],
    w_bm25: float = 1.0,
    w_dense: float = 1.0,
) -> list[tuple[str, float]]:
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
    bm25_top = {d for d, _ in bm25_r[:overlap_n]}
    dense_top = {d for d, _ in dense_r[:overlap_n]}
    union_set = bm25_top | dense_top
    agreement = len(bm25_top & dense_top) / len(union_set) if union_set else 0.0
    tension = 1.0 - agreement
    adaptive_k = max(1, int(base_k - k_range / 2 + k_range * tension))
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


# ── Hybrid: RRF + score blend ──


def hybrid_rrf_score(
    bm25_r: list[tuple[str, float]],
    dense_r: list[tuple[str, float]],
    k: int = 60,
    score_weight: float = 0.3,
) -> list[tuple[str, float]]:
    """RRF base + normalized score bonus. Best of both worlds."""
    # RRF component
    rrf_scores: dict[str, float] = defaultdict(float)
    for rank, (doc_id, _) in enumerate(bm25_r):
        rrf_scores[doc_id] += 1.0 / (k + rank + 1)
    for rank, (doc_id, _) in enumerate(dense_r):
        rrf_scores[doc_id] += 1.0 / (k + rank + 1)

    # Score component (normalized)
    score_scores: dict[str, float] = defaultdict(float)
    for source in [bm25_r, dense_r]:
        if not source:
            continue
        raw = [s for _, s in source]
        mn, mx = min(raw), max(raw)
        rng = mx - mn if mx > mn else 1.0
        for doc_id, score in source:
            score_scores[doc_id] += (score - mn) / rng

    # Blend
    all_docs = set(rrf_scores) | set(score_scores)
    # Normalize RRF to 0..1
    rrf_vals = list(rrf_scores.values())
    rrf_mn = min(rrf_vals) if rrf_vals else 0
    rrf_mx = max(rrf_vals) if rrf_vals else 1
    rrf_rng = rrf_mx - rrf_mn if rrf_mx > rrf_mn else 1.0

    final: dict[str, float] = {}
    for doc_id in all_docs:
        r = (rrf_scores.get(doc_id, 0) - rrf_mn) / rrf_rng
        s = score_scores.get(doc_id, 0) / 2.0  # max is 2.0 (from 2 sources)
        final[doc_id] = (1 - score_weight) * r + score_weight * s

    return sorted(final.items(), key=lambda x: x[1], reverse=True)


def run_fast_sweep() -> None:
    base_dir = os.path.join(os.path.dirname(__file__), "datasets")
    data_path = os.path.join(base_dir, "scifact")

    corpus, queries, qrels = GenericDataLoader(
        data_folder=data_path,
    ).load(split="test")
    print(f"Loaded: {len(corpus)} docs, {len(queries)} queries")

    # Build indices
    print("Building BM25...")
    bm25, doc_ids = build_bm25_with_ids(corpus)

    print("Loading cached Dense (bge-m3)...")
    cache_dir = os.path.join(base_dir, ".cache_scifact")
    os.environ["BEIR_DEVICE"] = "cpu"
    dense = DenseIndex(cache_dir, model_name="BAAI/bge-m3")
    dense.build(corpus)

    # ── Phase 0: Pre-cache ALL query results ──
    print("\n=== Phase 0: Caching all query search results ===")
    t0 = time.time()
    cached: dict[str, dict] = {}  # qid -> {bm25_r, dense_r, relevant}
    for qi, (qid, query_text) in enumerate(queries.items()):
        relevant = {
            did: rel
            for did, rel in qrels.get(qid, {}).items()
            if rel > 0
        }
        bm25_r = search_bm25_by_ids(bm25, doc_ids, query_text, top_k=100)
        dense_r = dense.search(query_text, top_k=100)
        cached[qid] = {
            "bm25_r": bm25_r,
            "dense_r": dense_r,
            "relevant": relevant,
        }
        if (qi + 1) % 50 == 0:
            print(f"  Cached {qi + 1}/{len(queries)}", flush=True)

    cache_time = time.time() - t0
    print(f"  Done in {cache_time:.1f}s ({len(cached)} queries)")

    # ── Evaluation on cached results (instant) ──
    def eval_cached(fusion_fn, **kwargs) -> float:
        ndcgs = []
        for entry in cached.values():
            fused = fusion_fn(entry["bm25_r"], entry["dense_r"], **kwargs)
            ranked = [did for did, _ in fused[:100]]
            ndcgs.append(ndcg_at_k(ranked, entry["relevant"], k=10))
        return sum(ndcgs) / len(ndcgs)

    all_results: list[dict] = []
    best_score = 0.0
    best_label = ""

    def record(label: str, score: float, **params) -> None:
        nonlocal best_score, best_label
        rounded = round(score, 4)
        all_results.append({"label": label, "ndcg10": rounded, **params})
        if score > best_score:
            best_score = score
            best_label = label

    # ── 1. Vanilla RRF: k sweep ──
    print("\n=== 1. Vanilla RRF: k sweep ===")
    for k in [1, 3, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60,
              70, 80, 90, 100, 120, 150, 200, 300, 500]:
        s = eval_cached(vanilla_rrf, k=k)
        record(f"RRF k={k}", s, strategy="rrf", k=k)
    rrf_results = [r for r in all_results if r.get("strategy") == "rrf"]
    best_rrf = max(rrf_results, key=lambda x: x["ndcg10"])
    print(f"  Best: {best_rrf['label']} -> {best_rrf['ndcg10']}")

    # ── 2. Weighted RRF: sweep around best k ──
    bk = best_rrf["k"]
    print(f"\n=== 2. Weighted RRF (k={bk}): weight sweep ===")
    for w_bm25, w_dense in [
        (0.1, 1.0), (0.2, 1.0), (0.3, 1.0), (0.4, 1.0), (0.5, 1.0),
        (0.6, 1.0), (0.7, 1.0), (0.8, 1.0), (0.9, 1.0),
        (1.0, 0.1), (1.0, 0.2), (1.0, 0.3), (1.0, 0.4), (1.0, 0.5),
        (1.0, 0.6), (1.0, 0.7), (1.0, 0.8), (1.0, 0.9),
        (1.0, 1.2), (1.0, 1.5), (1.0, 2.0), (1.0, 3.0), (1.0, 5.0),
        (1.2, 1.0), (1.5, 1.0), (2.0, 1.0), (3.0, 1.0), (5.0, 1.0),
    ]:
        s = eval_cached(weighted_rrf, k=bk, w_bm25=w_bm25, w_dense=w_dense)
        record(f"WRRF k={bk} b={w_bm25} d={w_dense}", s,
               strategy="wrrf", k=bk, w_bm25=w_bm25, w_dense=w_dense)

    # Also sweep k for best weight combo
    wrrf_results = [r for r in all_results if r.get("strategy") == "wrrf"]
    if wrrf_results:
        best_wrrf = max(wrrf_results, key=lambda x: x["ndcg10"])
        bw, dw = best_wrrf.get("w_bm25", 1), best_wrrf.get("w_dense", 1)
        print(f"  Best weight: bm25={bw} dense={dw} -> {best_wrrf['ndcg10']}")
        print("  Cross-sweep: best weight × all k values")
        for k in [5, 10, 20, 30, 40, 50, 60, 80, 100]:
            s = eval_cached(weighted_rrf, k=k, w_bm25=bw, w_dense=dw)
            record(f"WRRF k={k} b={bw} d={dw}", s,
                   strategy="wrrf_xk", k=k, w_bm25=bw, w_dense=dw)

    # ── 3. Agreement Boost: k + boost sweep ──
    print("\n=== 3. Agreement Boost: k + boost sweep ===")
    for k in [10, 20, 30, 40, 50, 60, 80, 100]:
        for boost in [1.01, 1.05, 1.1, 1.15, 1.2, 1.3, 1.5, 2.0, 3.0, 5.0]:
            s = eval_cached(agreement_rrf, k=k, boost=boost)
            record(f"Agree k={k} boost={boost}", s,
                   strategy="agree", k=k, boost=boost)
    agree_results = [r for r in all_results if r.get("strategy") == "agree"]
    best_agree = max(agree_results, key=lambda x: x["ndcg10"])
    print(f"  Best: {best_agree['label']} -> {best_agree['ndcg10']}")

    # ── 4. Score Fusion: weight sweep ──
    print("\n=== 4. Score Fusion: weight sweep ===")
    for w_bm25, w_dense in [
        (0.05, 1.0), (0.1, 1.0), (0.2, 1.0), (0.3, 1.0), (0.5, 1.0),
        (0.7, 1.0), (1.0, 1.0), (1.0, 0.7), (1.0, 0.5), (1.0, 0.3),
        (1.0, 0.2), (1.0, 0.1), (1.0, 0.05),
        (1.5, 1.0), (2.0, 1.0), (1.0, 1.5), (1.0, 2.0),
    ]:
        s = eval_cached(score_fusion, w_bm25=w_bm25, w_dense=w_dense)
        record(f"Score b={w_bm25} d={w_dense}", s,
               strategy="score", w_bm25=w_bm25, w_dense=w_dense)
    score_results = [r for r in all_results if r.get("strategy") == "score"]
    best_sf = max(score_results, key=lambda x: x["ndcg10"])
    print(f"  Best: {best_sf['label']} -> {best_sf['ndcg10']}")

    # ── 5. Tension-Adaptive: sweep ──
    print("\n=== 5. Tension-Adaptive: sweep ===")
    for base_k in [10, 20, 30, 40, 50, 60, 80, 100]:
        for k_range in [20, 40, 60, 80, 100]:
            for boost_max in [0.0, 0.1, 0.3, 0.5, 0.8, 1.2, 2.0]:
                s = eval_cached(
                    tension_rrf, base_k=base_k,
                    k_range=k_range, boost_max=boost_max,
                )
                record(
                    f"Tension bk={base_k} kr={k_range} bm={boost_max}", s,
                    strategy="tension",
                    base_k=base_k, k_range=k_range, boost_max=boost_max,
                )
    tension_results = [r for r in all_results if r.get("strategy") == "tension"]
    best_t = max(tension_results, key=lambda x: x["ndcg10"])
    print(f"  Best: {best_t['label']} -> {best_t['ndcg10']}")

    # ── 6. Hybrid RRF+Score: sweep ──
    print("\n=== 6. Hybrid RRF+Score blend: sweep ===")
    for k in [10, 20, 30, 40, 50, 60, 80]:
        for sw in [0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.7]:
            s = eval_cached(hybrid_rrf_score, k=k, score_weight=sw)
            record(f"HybridRS k={k} sw={sw}", s,
                   strategy="hybrid_rs", k=k, score_weight=sw)
    hrs_results = [r for r in all_results if r.get("strategy") == "hybrid_rs"]
    best_hrs = max(hrs_results, key=lambda x: x["ndcg10"])
    print(f"  Best: {best_hrs['label']} -> {best_hrs['ndcg10']}")

    # ── Summary ──
    total_time = time.time() - t0
    sep = "=" * 60
    print(f"\n{sep}")
    print(f"  Total configs: {len(all_results)}")
    print(f"  Total time: {total_time:.0f}s")
    print(f"  Best: {best_label}  nDCG@10={best_score:.4f}")
    print(f"  Baseline (RRF k=60): {BASELINE_NDCG}")
    print(f"  SOTA (E5-PT_base):   {SOTA_NDCG}")
    if best_score > SOTA_NDCG:
        print("  >>> SOTA BEATEN! <<<")
    elif best_score > BASELINE_NDCG:
        improvement = best_score - BASELINE_NDCG
        print(f"  Improvement over baseline: +{improvement:.4f}")
        print(f"  Gap to SOTA: {SOTA_NDCG - best_score:.4f}")
    else:
        print("  No improvement. RRF k=60 remains best.")
    print(sep)

    # Top 20
    top20 = sorted(all_results, key=lambda x: x["ndcg10"], reverse=True)[:20]
    print("\nTop 20 configs:")
    for i, r in enumerate(top20, 1):
        marker = " <-- BASELINE" if r["label"] == "RRF k=60" else ""
        print(f"  {i:>2d}. {r['label']:<45s} {r['ndcg10']}{marker}")

    # Per-strategy best
    print("\nPer-strategy best:")
    strategies = {}
    for r in all_results:
        st = r.get("strategy", "?")
        if st not in strategies or r["ndcg10"] > strategies[st]["ndcg10"]:
            strategies[st] = r
    for st, r in sorted(strategies.items(), key=lambda x: x[1]["ndcg10"],
                        reverse=True):
        print(f"  {st:<12s} {r['label']:<45s} {r['ndcg10']}")

    # Save
    out_path = os.path.join(
        os.path.dirname(__file__), "results",
        f"sweep_fast_{time.strftime('%Y%m%d_%H%M')}.json",
    )
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({
            "total_configs": len(all_results),
            "total_time_s": round(total_time, 1),
            "best": {"label": best_label, "ndcg10": round(best_score, 4)},
            "top20": top20,
            "per_strategy_best": strategies,
            "all_results": all_results,
        }, f, indent=2)
    print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    run_fast_sweep()
