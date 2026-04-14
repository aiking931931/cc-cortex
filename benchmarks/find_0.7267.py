"""Hunt for the lost 0.7267 config: sweep all fusion strategies × k values."""
from __future__ import annotations

import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))
os.environ["BEIR_DEVICE"] = "cpu"

from beir.datasets.data_loader import GenericDataLoader
from beir_runner import build_bm25_with_ids, ndcg_at_k, recall_at_k, search_bm25_by_ids


def main():
    base = os.path.join(os.path.dirname(__file__), "datasets")
    corpus, queries, qrels = GenericDataLoader(
        os.path.join(base, "scifact")
    ).load(split="test")
    bm25, doc_ids = build_bm25_with_ids(corpus)

    # Use ChromaDB dense index (same as beir_runner.py)
    from beir_runner import DenseIndex

    cache_dir = os.path.join(base, ".cache_scifact")
    dense = DenseIndex(cache_dir, model_name="BAAI/bge-m3")
    dense.build(corpus)
    print("Dense index ready", flush=True)

    # Cache all query results
    print("Caching queries...", flush=True)
    cached = {}
    for qi, (qid, qt) in enumerate(queries.items()):
        rel = {d: r for d, r in qrels.get(qid, {}).items() if r > 0}
        bm25_r = search_bm25_by_ids(bm25, doc_ids, qt, top_k=100)
        dense_r = dense.search(qt, top_k=100)
        cached[qid] = {"bm25": bm25_r, "dense": dense_r, "rel": rel}
        if (qi + 1) % 50 == 0:
            print(f"  Cached {qi + 1}/300", flush=True)
    print(f"Cached {len(cached)} queries", flush=True)

    def eval_fn(fusion_fn, **kw):
        ndcgs = []
        r100s = []
        for e in cached.values():
            fused = fusion_fn(e["bm25"], e["dense"], **kw)
            ranked = [d for d, _ in fused[:100]]
            ndcgs.append(ndcg_at_k(ranked, e["rel"], k=10))
            r100s.append(recall_at_k(ranked, e["rel"], k=100))
        return sum(ndcgs) / len(ndcgs), sum(r100s) / len(r100s)

    # 1. Plain RRF sweep
    print("\n=== RRF k sweep ===", flush=True)
    for k in [5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]:
        def rrf(b, d, k=k):
            scores = defaultdict(float)
            for rank, (did, _) in enumerate(b):
                scores[did] += 1.0 / (k + rank + 1)
            for rank, (did, _) in enumerate(d):
                scores[did] += 1.0 / (k + rank + 1)
            return sorted(scores.items(), key=lambda x: x[1], reverse=True)

        n, r = eval_fn(rrf)
        marker = " <<<" if abs(n - 0.7267) < 0.001 else ""
        print(f"  k={k:3d}: nDCG@10={n:.4f} R@100={r:.4f}{marker}", flush=True)

    # 2. RRF + Agreement boost sweep
    print("\n=== RRF + Agreement Boost ===", flush=True)
    for k in [5, 10, 30, 60]:
        for boost in [1.2, 1.5, 1.8, 2.0]:
            def rrf_agree(b, d, k=k, boost=boost):
                scores = defaultdict(float)
                presence = defaultdict(int)
                for rank, (did, _) in enumerate(b):
                    scores[did] += 1.0 / (k + rank + 1)
                    presence[did] += 1
                for rank, (did, _) in enumerate(d):
                    scores[did] += 1.0 / (k + rank + 1)
                    presence[did] += 1
                for did in scores:
                    if presence[did] >= 2:
                        scores[did] *= boost
                return sorted(scores.items(), key=lambda x: x[1], reverse=True)

            n, r = eval_fn(rrf_agree)
            marker = " <<<" if abs(n - 0.7267) < 0.001 else ""
            print(
                f"  k={k:2d} boost={boost}: nDCG@10={n:.4f} R@100={r:.4f}{marker}",
                flush=True,
            )

    # 3. Score normalized fusion
    print("\n=== Score Normalized ===", flush=True)
    for bw in [0.8, 1.0, 1.2]:
        for dw in [0.8, 1.0, 1.2, 1.5]:
            def score_norm(b, d, bw=bw, dw=dw):
                def norm(results):
                    if not results:
                        return {}
                    vals = [s for _, s in results]
                    mn, mx = min(vals), max(vals)
                    rng = mx - mn if mx > mn else 1.0
                    return {did: (s - mn) / rng for did, s in results}

                b_n = norm(b)
                d_n = norm(d)
                all_docs = set(b_n) | set(d_n)
                scores = {}
                for did in all_docs:
                    scores[did] = bw * b_n.get(did, 0) + dw * d_n.get(did, 0)
                return sorted(scores.items(), key=lambda x: x[1], reverse=True)

            n, r = eval_fn(score_norm)
            marker = " <<<" if abs(n - 0.7267) < 0.001 else ""
            print(
                f"  bw={bw} dw={dw}: nDCG@10={n:.4f} R@100={r:.4f}{marker}",
                flush=True,
            )

    # 4. Tension adaptive
    print("\n=== Tension Adaptive ===", flush=True)
    for k_base in [5, 10, 30, 60]:
        for k_range in [20, 40, 60]:
            def tension(b, d, k_base=k_base, k_range=k_range):
                b_set = {did for did, _ in b[:20]}
                d_set = {did for did, _ in d[:20]}
                union = b_set | d_set
                agreement = len(b_set & d_set) / len(union) if union else 0.0
                tension_val = 1.0 - agreement
                adaptive_k = int(k_base + k_range * tension_val)
                boost = 1.0 + agreement * 0.8

                scores = defaultdict(float)
                presence = defaultdict(int)
                for rank, (did, _) in enumerate(b):
                    scores[did] += 1.0 / (adaptive_k + rank + 1)
                    presence[did] += 1
                for rank, (did, _) in enumerate(d):
                    scores[did] += 1.0 / (adaptive_k + rank + 1)
                    presence[did] += 1
                for did in scores:
                    if presence[did] >= 2:
                        scores[did] *= boost
                return sorted(scores.items(), key=lambda x: x[1], reverse=True)

            n, r = eval_fn(tension)
            marker = " <<<" if abs(n - 0.7267) < 0.001 else ""
            print(
                f"  k_base={k_base:2d} k_range={k_range:2d}: "
                f"nDCG@10={n:.4f} R@100={r:.4f}{marker}",
                flush=True,
            )

    print("\nDone.", flush=True)


if __name__ == "__main__":
    main()
