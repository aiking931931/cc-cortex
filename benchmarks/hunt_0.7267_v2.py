"""Hunt 0.7267: test alternate ChromaDB cache + query prefix variations."""
from __future__ import annotations

import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))
os.environ["BEIR_DEVICE"] = "cpu"

from beir.datasets.data_loader import GenericDataLoader
from beir_runner import DenseIndex, build_bm25_with_ids, ndcg_at_k, recall_at_k, search_bm25_by_ids


def rrf(bm25_r, dense_r, k=60):
    """Standard RRF with k=60 (beir_runner default)."""
    scores: dict[str, float] = defaultdict(float)
    for rank, (did, _) in enumerate(bm25_r):
        scores[did] += 1.0 / (k + rank + 1)
    for rank, (did, _) in enumerate(dense_r):
        scores[did] += 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def main():
    base = os.path.join(os.path.dirname(__file__), "datasets")
    corpus, queries, qrels = GenericDataLoader(
        os.path.join(base, "scifact")
    ).load(split="test")
    bm25, doc_ids = build_bm25_with_ids(corpus)
    print(f"BM25 ready ({len(doc_ids)} docs)", flush=True)

    # Test configurations
    cache_dirs = {
        "default (.cache_scifact)": os.path.join(base, ".cache_scifact"),
        "alt (.cache_scifact_BAAI_bge-m3)": os.path.join(
            base, ".cache_scifact_BAAI_bge-m3"
        ),
    }

    for cache_name, cache_dir in cache_dirs.items():
        if not os.path.exists(os.path.join(cache_dir, "dense_db")):
            print(f"\n⚠ {cache_name}: no dense_db, skipping", flush=True)
            continue

        print(f"\n{'='*60}", flush=True)
        print(f"Testing cache: {cache_name}", flush=True)
        print(f"{'='*60}", flush=True)

        dense = DenseIndex(cache_dir, model_name="BAAI/bge-m3")
        dense.build(corpus)

        # Test with different k values
        for k in [5, 10, 30, 60]:
            all_ndcg = []
            all_recall = []
            for qid, qt in queries.items():
                rel = {d: r for d, r in qrels.get(qid, {}).items() if r > 0}
                bm25_r = search_bm25_by_ids(bm25, doc_ids, qt, top_k=100)
                dense_r = dense.search(qt, top_k=100)
                fused = rrf(bm25_r, dense_r, k=k)
                ranked = [d for d, _ in fused[:100]]
                all_ndcg.append(ndcg_at_k(ranked, rel, k=10))
                all_recall.append(recall_at_k(ranked, rel, k=100))

            avg_n = sum(all_ndcg) / len(all_ndcg)
            avg_r = sum(all_recall) / len(all_recall)
            marker = " <<<" if abs(avg_n - 0.7267) < 0.001 else ""
            print(
                f"  RRF k={k:2d}: nDCG@10={avg_n:.4f} R@100={avg_r:.4f}{marker}",
                flush=True,
            )

        # Also test dense-only and BM25-only
        all_ndcg_d = []
        all_recall_d = []
        all_ndcg_b = []
        all_recall_b = []
        for qid, qt in queries.items():
            rel = {d: r for d, r in qrels.get(qid, {}).items() if r > 0}
            bm25_r = search_bm25_by_ids(bm25, doc_ids, qt, top_k=100)
            dense_r = dense.search(qt, top_k=100)

            ranked_d = [d for d, _ in dense_r[:100]]
            ranked_b = [d for d, _ in bm25_r[:100]]

            all_ndcg_d.append(ndcg_at_k(ranked_d, rel, k=10))
            all_recall_d.append(recall_at_k(ranked_d, rel, k=100))
            all_ndcg_b.append(ndcg_at_k(ranked_b, rel, k=10))
            all_recall_b.append(recall_at_k(ranked_b, rel, k=100))

        print(
            f"  Dense only: nDCG@10="
            f"{sum(all_ndcg_d)/len(all_ndcg_d):.4f} "
            f"R@100={sum(all_recall_d)/len(all_recall_d):.4f}",
            flush=True,
        )
        print(
            f"  BM25 only:  nDCG@10="
            f"{sum(all_ndcg_b)/len(all_ndcg_b):.4f} "
            f"R@100={sum(all_recall_b)/len(all_recall_b):.4f}",
            flush=True,
        )

    print("\nDone.", flush=True)


if __name__ == "__main__":
    main()
