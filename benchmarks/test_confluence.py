"""Test Confluence RAG fusion modes on SciFact."""
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
from confluence_rag import (
    fuse,
    fuse_auto,
    fuse_fixed,
    fuse_simple_rrf,
)
from sentence_transformers import SentenceTransformer


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

    # ── Test all modes ──
    modes = {
        "Simple RRF (baseline)": lambda e: fuse_simple_rrf(e["bm25"], e["dense"]),
        "Fixed (world #1 params)": lambda e: fuse_fixed(e["bm25"], e["dense"]),
        "Auto (semantic tuning)": lambda e: fuse_auto(
            e["bm25"], e["dense"], e["text"],
        ),
    }

    results = {}
    for name, fn in modes.items():
        t0 = time.time()
        ndcgs = []
        recalls = []
        for entry in cached.values():
            ranked_raw = fn(entry)
            ranked = [d for d, _ in ranked_raw[:100]]
            ndcgs.append(ndcg_at_k(ranked, entry["rel"], k=10))
            recalls.append(recall_at_k(ranked, entry["rel"], k=100))

        avg_n = sum(ndcgs) / len(ndcgs)
        avg_r = sum(recalls) / len(recalls)
        elapsed = time.time() - t0
        results[name] = {
            "ndcg10": round(avg_n, 4),
            "recall100": round(avg_r, 4),
            "time_s": round(elapsed, 2),
        }
        print(
            f"{name:30s}: nDCG@10={avg_n:.4f} R@100={avg_r:.4f} ({elapsed:.1f}s)",
            flush=True,
        )

    # ── Diagnostic: Auto-tune analysis distribution ──
    print("\n=== Auto-Tune Diagnostic ===", flush=True)
    tensions = []
    specificities = []
    for entry in cached.values():
        result = fuse(entry["bm25"], entry["dense"], entry["text"])
        tensions.append(result.analysis.tension)
        specificities.append(result.analysis.query_specificity)

    print(
        f"  Tension: mean={np.mean(tensions):.3f} "
        f"std={np.std(tensions):.3f} "
        f"min={min(tensions):.3f} max={max(tensions):.3f}",
        flush=True,
    )
    print(
        f"  Specificity: mean={np.mean(specificities):.3f} "
        f"std={np.std(specificities):.3f}",
        flush=True,
    )

    # ── Summary ──
    print("\n" + "=" * 60, flush=True)
    print("COMPARISON", flush=True)
    print("=" * 60, flush=True)
    print("  E5-PT dense only (SOTA):  0.7371", flush=True)
    for name, r in results.items():
        delta = r["ndcg10"] - 0.7371
        print(f"  {name:30s}: {r['ndcg10']:.4f} ({delta:+.4f} vs SOTA)", flush=True)

    out = os.path.join(os.path.dirname(__file__), "results", "confluence_test.json")
    with open(out, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved to {out}", flush=True)


if __name__ == "__main__":
    main()
