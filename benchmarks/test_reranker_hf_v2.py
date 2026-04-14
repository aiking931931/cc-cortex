"""Test bge-reranker-v2-m3 via HuggingFace Router API (GPU)."""
from __future__ import annotations

import json
import os
import sys
import time

import numpy as np
import requests

sys.path.insert(0, os.path.dirname(__file__))
os.environ["BEIR_DEVICE"] = "cpu"

from beir.datasets.data_loader import GenericDataLoader
from beir_runner import build_bm25_with_ids, ndcg_at_k, recall_at_k, search_bm25_by_ids
from confluence_rag import fuse_fixed
from sentence_transformers import SentenceTransformer

HF_TOKEN = __import__("os").environ.get("HF_TOKEN", "")
RERANKER_URL = (
    "https://router.huggingface.co/hf-inference/models/BAAI/bge-reranker-v2-m3"
)


def hf_rerank(query: str, texts: list[str]) -> list[float]:
    """Call HF Router API for reranking scores."""
    headers = {"Authorization": f"Bearer {HF_TOKEN}"}
    payload = {
        "inputs": [
            {"text": query, "text_pair": t[:512]} for t in texts
        ],
    }
    for attempt in range(3):
        try:
            resp = requests.post(
                RERANKER_URL, headers=headers, json=payload, timeout=60,
            )
            if resp.status_code == 503:
                print(f"    Model loading, retry {attempt+1}...", flush=True)
                time.sleep(20)
                continue
            if resp.status_code == 429:
                time.sleep(5)
                continue
            resp.raise_for_status()
            data = resp.json()
            # Response format: [[{label, score}, {label, score}, ...]]
            # Outer list has 1 element, inner list has N scores
            if isinstance(data, list) and len(data) == 1 and isinstance(data[0], list):
                return [item["score"] for item in data[0]]
            # Fallback: flat list of dicts
            if isinstance(data, list) and all(isinstance(d, dict) for d in data):
                return [d["score"] for d in data]
            return [float(x) for x in data]
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2)
    return []


def main():
    base = os.path.join(os.path.dirname(__file__), "datasets")
    corpus, queries, qrels = GenericDataLoader(
        os.path.join(base, "scifact")
    ).load(split="test")
    bm25, doc_ids = build_bm25_with_ids(corpus)

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

    # Verify API
    print("Testing API...", flush=True)
    test = hf_rerank("test", ["relevant doc", "irrelevant noise"])
    print(f"  API OK: {test}", flush=True)

    results = {}
    for rerank_top in [10, 20, 30]:
        print(f"\n=== BGE Reranker top-{rerank_top} (GPU) ===", flush=True)
        all_ndcg = []
        all_recall = []
        t0 = time.time()
        errors = 0

        for qi, (qid, entry) in enumerate(cached.items()):
            fused = fuse_fixed(entry["bm25"], entry["dense"])
            top_cands = fused[:rerank_top]
            rest = fused[rerank_top:]

            texts = []
            valid_dids = []
            for did, _ in top_cands:
                if did in corpus:
                    doc = corpus[did]
                    text = doc.get("title", "") + " " + doc.get("text", "")
                    texts.append(text.strip())
                    valid_dids.append(did)

            if texts:
                try:
                    ce_scores = hf_rerank(entry["text"], texts)
                    if len(ce_scores) == len(valid_dids):
                        reranked = sorted(
                            zip(valid_dids, ce_scores),
                            key=lambda x: x[1],
                            reverse=True,
                        )
                        reranked_set = {d for d, _ in reranked}
                        remaining = [
                            (d, s) for d, s in rest if d not in reranked_set
                        ]
                        final = [
                            (d, float(s)) for d, s in reranked
                        ] + remaining
                    else:
                        final = list(fused)
                        errors += 1
                except Exception as e:
                    if qi < 3:
                        print(f"    Error q{qi}: {e}", flush=True)
                    final = list(fused)
                    errors += 1
            else:
                final = list(fused)

            ranked = [d for d, _ in final[:100]]
            all_ndcg.append(ndcg_at_k(ranked, entry["rel"], k=10))
            all_recall.append(recall_at_k(ranked, entry["rel"], k=100))

            if (qi + 1) % 30 == 0:
                elapsed = time.time() - t0
                avg_n = sum(all_ndcg) / len(all_ndcg)
                print(
                    f"  [{qi+1}/300] nDCG@10={avg_n:.4f} "
                    f"err={errors} ({elapsed:.0f}s)",
                    flush=True,
                )

            # Rate limit
            time.sleep(0.15)

        avg_ndcg = sum(all_ndcg) / len(all_ndcg)
        avg_recall = sum(all_recall) / len(all_recall)
        total = time.time() - t0
        delta = avg_ndcg - 0.7578
        results[f"bge_top_{rerank_top}"] = {
            "ndcg10": round(avg_ndcg, 4),
            "recall100": round(avg_recall, 4),
            "time_s": round(total, 1),
            "errors": errors,
            "delta": round(delta, 4),
        }
        print(
            f"  RESULT: nDCG@10={avg_ndcg:.4f} "
            f"delta={delta:+.4f} err={errors} ({total:.0f}s)",
            flush=True,
        )

    print("\n" + "=" * 60, flush=True)
    print("SUMMARY", flush=True)
    print("=" * 60, flush=True)
    print("  Baseline RT (no reranker): 0.7578", flush=True)
    for name, r in results.items():
        print(
            f"  {name}: {r['ndcg10']:.4f} ({r['delta']:+.4f})",
            flush=True,
        )

    out = os.path.join(
        os.path.dirname(__file__), "results", "bge_reranker_gpu_results.json",
    )
    with open(out, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved to {out}", flush=True)


if __name__ == "__main__":
    main()
