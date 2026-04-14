"""Test bge-reranker-v2-m3 as 3rd stage on top of best fusion."""
from __future__ import annotations

import json
import os
import sys
import time

import numpy as np
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))
os.environ["BEIR_DEVICE"] = "cpu"

from beir_runner import (
    build_bm25_with_ids,
    ndcg_at_k,
    recall_at_k,
    search_bm25_by_ids,
)
from beir.datasets.data_loader import GenericDataLoader
from sentence_transformers import CrossEncoder, SentenceTransformer


def main():
    base = os.path.join(os.path.dirname(__file__), "datasets")
    corpus, queries, qrels = GenericDataLoader(
        os.path.join(base, "scifact")
    ).load(split="test")
    bm25, doc_ids = build_bm25_with_ids(corpus)

    # Dense: E5-PT_base
    model = SentenceTransformer("intfloat/e5-base-unsupervised", device="cpu")
    cache = np.load(os.path.join(base, ".cache_e5pt_base_embs.npz"))
    passage_embs = cache["embs"]
    doc_id_list = list(corpus.keys())

    def e5pt_search(qt, top_k=100):
        q = model.encode(["query: " + qt], normalize_embeddings=True)
        sims = (passage_embs @ q.T).flatten()
        idx = np.argsort(sims)[::-1][:top_k]
        return [(doc_id_list[i], float(sims[i])) for i in idx]

    # Reranker
    print("Loading reranker...", flush=True)
    reranker = CrossEncoder(
        "BAAI/bge-reranker-v2-m3", max_length=512, device="cpu",
    )
    print("Reranker loaded", flush=True)

    # Best RRF config
    def fuse_rrf(bm25_r, dense_r, k=5, bw=1.0, dw=1.2):
        scores: dict[str, float] = defaultdict(float)
        for rank, (did, _) in enumerate(bm25_r):
            scores[did] += bw / (k + rank + 1)
        for rank, (did, _) in enumerate(dense_r):
            scores[did] += dw / (k + rank + 1)
        return sorted(scores.items(), key=lambda x: x[1], reverse=True)

    # Run with reranker on top-N
    for rerank_top in [10, 20, 30, 50]:
        print(f"\n=== Reranker top-{rerank_top} ===", flush=True)
        all_ndcg = []
        all_recall = []
        t0 = time.time()

        for qi, (qid, qt) in enumerate(queries.items()):
            rel = {d: r for d, r in qrels.get(qid, {}).items() if r > 0}
            bm25_r = search_bm25_by_ids(bm25, doc_ids, qt, top_k=100)
            dense_r = e5pt_search(qt, top_k=100)
            fused = fuse_rrf(bm25_r, dense_r)

            # Rerank top candidates
            top_cands = fused[:rerank_top]
            rest = fused[rerank_top:]

            pairs = []
            valid_dids = []
            for did, _ in top_cands:
                if did in corpus:
                    doc = corpus[did]
                    text = doc.get("title", "") + " " + doc.get("text", "")
                    pairs.append([qt, text.strip()])
                    valid_dids.append(did)

            if pairs:
                ce_scores = reranker.predict(pairs).tolist()
                reranked = sorted(
                    zip(valid_dids, ce_scores),
                    key=lambda x: x[1],
                    reverse=True,
                )
                reranked_set = {d for d, _ in reranked}
                remaining = [(d, s) for d, s in rest if d not in reranked_set]
                final = [(d, float(s)) for d, s in reranked] + remaining
            else:
                final = fused

            ranked = [d for d, _ in final[:100]]
            all_ndcg.append(ndcg_at_k(ranked, rel, k=10))
            all_recall.append(recall_at_k(ranked, rel, k=100))

            if (qi + 1) % 50 == 0:
                elapsed = time.time() - t0
                avg_n = sum(all_ndcg) / len(all_ndcg)
                print(
                    f"  [{qi+1}/300] nDCG@10={avg_n:.4f} ({elapsed:.0f}s)",
                    flush=True,
                )

        avg_ndcg = sum(all_ndcg) / len(all_ndcg)
        avg_recall = sum(all_recall) / len(all_recall)
        total = time.time() - t0
        print(f"  RESULT: nDCG@10={avg_ndcg:.4f} R@100={avg_recall:.4f}", flush=True)
        print(f"  Time: {total:.0f}s", flush=True)
        print(f"  vs baseline 0.7541: {avg_ndcg - 0.7541:+.4f}", flush=True)

    print("\nDone.", flush=True)


if __name__ == "__main__":
    main()
