"""Test CPU-friendly reranker (ms-marco-MiniLM) on top of best fusion."""
from __future__ import annotations

import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
os.environ["BEIR_DEVICE"] = "cpu"

from beir.datasets.data_loader import GenericDataLoader
from beir_runner import build_bm25_with_ids, ndcg_at_k, recall_at_k, search_bm25_by_ids
from confluence_rag import fuse_fixed, fuse_simple_rrf
from sentence_transformers import CrossEncoder, SentenceTransformer


def main():
    base = os.path.join(os.path.dirname(__file__), "datasets")
    corpus, queries, qrels = GenericDataLoader(
        os.path.join(base, "scifact")
    ).load(split="test")
    bm25, doc_ids = build_bm25_with_ids(corpus)

    # E5-PT_base
    model = SentenceTransformer("intfloat/e5-base-unsupervised", device="cpu")
    cache = np.load(os.path.join(base, ".cache_e5pt_base_embs.npz"))
    passage_embs = cache["embs"]
    doc_id_list = list(corpus.keys())

    def e5pt_search(qt, top_k=100):
        q = model.encode(["query: " + qt], normalize_embeddings=True)
        sims = (passage_embs @ q.T).flatten()
        idx = np.argsort(sims)[::-1][:top_k]
        return [(doc_id_list[i], float(sims[i])) for i in idx]

    # CPU-friendly reranker
    print("Loading MiniLM reranker (CPU-friendly)...", flush=True)
    reranker = CrossEncoder(
        "cross-encoder/ms-marco-MiniLM-L-6-v2", max_length=512, device="cpu",
    )
    print("Reranker loaded!", flush=True)

    # Cache search results
    print("Caching queries...", flush=True)
    cached = {}
    for qid, qt in queries.items():
        rel = {d: r for d, r in qrels.get(qid, {}).items() if r > 0}
        cached[qid] = {
            "bm25": search_bm25_by_ids(bm25, doc_ids, qt, top_k=100),
            "dense": e5pt_search(qt, top_k=100),
            "rel": rel,
            "text": qt,
        }
    print(f"Cached {len(cached)} queries", flush=True)

    def rerank_top_n(fused, qt, n):
        """Rerank top-N candidates with cross-encoder."""
        top_cands = fused[:n]
        rest = fused[n:]
        pairs = []
        valid_dids = []
        for did, _ in top_cands:
            if did in corpus:
                doc = corpus[did]
                text = doc.get("title", "") + " " + doc.get("text", "")
                pairs.append([qt, text.strip()])
                valid_dids.append(did)
        if not pairs:
            return list(fused)
        ce_scores = reranker.predict(pairs).tolist()
        reranked = sorted(
            zip(valid_dids, ce_scores), key=lambda x: x[1], reverse=True,
        )
        reranked_set = {d for d, _ in reranked}
        remaining = [(d, s) for d, s in rest if d not in reranked_set]
        return [(d, float(s)) for d, s in reranked] + remaining

    # ── Test configurations ──
    configs = [
        ("Riverbed×Tension (no reranker)", None),
        ("RT + rerank top-10", 10),
        ("RT + rerank top-20", 20),
        ("RT + rerank top-30", 30),
        ("RT + rerank top-50", 50),
        ("Simple RRF + rerank top-20", "rrf_20"),
    ]

    results = {}
    for name, rerank_n in configs:
        t0 = time.time()
        ndcgs = []
        recalls = []

        for entry in cached.values():
            if name.startswith("Simple"):
                fused = fuse_simple_rrf(entry["bm25"], entry["dense"])
            else:
                fused = fuse_fixed(entry["bm25"], entry["dense"])

            if rerank_n == "rrf_20":
                final = rerank_top_n(fused, entry["text"], 20)
            elif rerank_n is not None:
                final = rerank_top_n(fused, entry["text"], rerank_n)
            else:
                final = fused

            ranked = [d for d, _ in final[:100]]
            ndcgs.append(ndcg_at_k(ranked, entry["rel"], k=10))
            recalls.append(recall_at_k(ranked, entry["rel"], k=100))

        avg_n = sum(ndcgs) / len(ndcgs)
        avg_r = sum(recalls) / len(recalls)
        elapsed = time.time() - t0
        results[name] = {
            "ndcg10": round(avg_n, 4),
            "recall100": round(avg_r, 4),
            "time_s": round(elapsed, 1),
        }
        delta = avg_n - 0.7578
        print(
            f"{name:40s}: nDCG@10={avg_n:.4f} R@100={avg_r:.4f} "
            f"({elapsed:.0f}s) delta={delta:+.4f}",
            flush=True,
        )

    # Summary
    print("\n" + "=" * 60, flush=True)
    print("SUMMARY", flush=True)
    print("=" * 60, flush=True)
    print("  Baseline RT (no reranker): 0.7578", flush=True)
    print("  SOTA (E5-PT dense only):   0.7371", flush=True)
    best = max(results.values(), key=lambda x: x["ndcg10"])
    best_name = [k for k, v in results.items() if v == best][0]
    print(f"  BEST: {best_name} = {best['ndcg10']:.4f}", flush=True)

    out = os.path.join(os.path.dirname(__file__), "results", "reranker_cpu_results.json")
    with open(out, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved to {out}", flush=True)


if __name__ == "__main__":
    main()
