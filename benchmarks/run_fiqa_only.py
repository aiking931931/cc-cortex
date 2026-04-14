"""FiQA-only cross-dataset validation with checkpoint.

Minimal memory footprint: only loads FiQA, skips SciFact/NFCorpus (already done).
Checkpoint saves every 2000 docs so crash = resume, not restart.

Usage:
    python benchmarks/run_fiqa_only.py
"""
from __future__ import annotations

import gc
import json
import math
import os
import re
import sys
import time
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))

from beir import util
from beir.datasets.data_loader import GenericDataLoader
from rank_bm25 import BM25Okapi
from sentence_transformers import SentenceTransformer

# ── Parameters (identical to cross_dataset_validate.py) ──────────

FIXED_E5PT = {
    "k_low": 3, "k_high": 10, "top_n": 20,
    "boost_max": 1.2, "score_w": 0.5, "bw": 0.8, "dw": 1.4,
}

FIXED_UNIVERSAL = {
    "k_low": 2, "k_high": 5, "top_n": 20,
    "boost_max": 1.2, "score_w": 0.5, "bw": 0.8, "dw": 1.0,
}

BASELINE_RRF = {"k": 5, "bw": 1.0, "dw": 1.2}

# ── Fusion functions ─────────────────────────────────────────────


def simple_rrf(b, d, k=5, bw=1.0, dw=1.2):
    scores = defaultdict(float)
    for rank, (did, _) in enumerate(b):
        scores[did] += bw / (k + rank + 1)
    for rank, (did, _) in enumerate(d):
        scores[did] += dw / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def riverbed_tension(b, d, k_low=2, k_high=10, top_n=10,
                     boost_max=1.3, score_w=0.3, bw=1.0, dw=1.2):
    b_set = {did for did, _ in b[:top_n]}
    d_set = {did for did, _ in d[:top_n]}
    union = b_set | d_set
    agreement = len(b_set & d_set) / len(union) if union else 0.0
    tension = 1.0 - agreement
    adaptive_k = max(1, int(k_low + (k_high - k_low) * tension))
    boost = 1.0 + (boost_max - 1.0) * agreement

    rrf_scores = defaultdict(float)
    presence = defaultdict(int)
    for rank, (did, _) in enumerate(b):
        rrf_scores[did] += bw / (adaptive_k + rank + 1)
        presence[did] += 1
    for rank, (did, _) in enumerate(d):
        rrf_scores[did] += dw / (adaptive_k + rank + 1)
        presence[did] += 1
    for did in rrf_scores:
        if presence[did] >= 2:
            rrf_scores[did] *= boost

    def norm(results):
        if not results:
            return {}
        vals = [s for _, s in results]
        mn, mx = min(vals), max(vals)
        rng = mx - mn if mx > mn else 1.0
        return {did: (s - mn) / rng for did, s in results}

    b_n, d_n = norm(b), norm(d)
    rv = list(rrf_scores.values())
    r_mn, r_mx = min(rv), max(rv)
    r_rng = r_mx - r_mn if r_mx > r_mn else 1.0

    all_docs = set(rrf_scores) | set(b_n) | set(d_n)
    final = {}
    for did in all_docs:
        r = (rrf_scores.get(did, 0) - r_mn) / r_rng
        s = (bw * b_n.get(did, 0) + dw * d_n.get(did, 0)) / (bw + dw)
        final[did] = (1 - score_w) * r + score_w * s
    return sorted(final.items(), key=lambda x: x[1], reverse=True)


# ── Metrics ──────────────────────────────────────────────────────


def ndcg_at_k(ranked_ids, relevant, k=10):
    dcg = sum(relevant.get(did, 0) / math.log2(i + 2)
              for i, did in enumerate(ranked_ids[:k]))
    ideal = sorted(relevant.values(), reverse=True)[:k]
    idcg = sum(r / math.log2(i + 2) for i, r in enumerate(ideal))
    return dcg / idcg if idcg > 0 else 0.0


def recall_at_k(ranked_ids, relevant, k=100):
    if not relevant:
        return 0.0
    return sum(1 for did in ranked_ids[:k] if did in relevant) / len(relevant)


# ── Main ─────────────────────────────────────────────────────────


def main():
    base_dir = os.path.join(os.path.dirname(__file__), "datasets")
    fiqa_url = ("https://public.ukp.informatik.tu-darmstadt.de"
                "/thakur/BEIR/datasets/fiqa.zip")

    print("=" * 60, flush=True)
    print("FiQA Cross-Dataset Validation (CPU + Checkpoint)", flush=True)
    print("=" * 60, flush=True)

    # Download if needed
    data_path = os.path.join(base_dir, "fiqa")
    if not os.path.isdir(data_path):
        print("Downloading FiQA...", flush=True)
        data_path = util.download_and_unzip(fiqa_url, base_dir)

    corpus, queries, qrels = GenericDataLoader(data_path).load(split="test")
    print(f"Corpus: {len(corpus)} docs | Queries: {len(queries)}", flush=True)

    # Load model
    print("Loading E5-PT base (CPU)...", flush=True)
    model = SentenceTransformer("intfloat/e5-base-unsupervised", device="cpu")
    print("Model loaded.", flush=True)

    # ── Encode with checkpoint ───────────────────────────────
    cache_path = os.path.join(base_dir, ".cache_fiqa_e5pt_base_embs.npz")
    ckpt_path = cache_path + ".ckpt.npz"
    doc_id_list = list(corpus.keys())

    if os.path.exists(cache_path):
        print(f"Loading cached embeddings: {cache_path}", flush=True)
        passage_embs = np.load(cache_path)["embs"]
    else:
        texts = []
        for doc_id in doc_id_list:
            doc = corpus[doc_id]
            texts.append(f"passage: {doc.get('title', '')} {doc.get('text', '')}".strip())

        start_idx = 0
        all_embs = []
        if os.path.exists(ckpt_path):
            ckpt = np.load(ckpt_path)
            start_idx = int(ckpt["done"])
            all_embs = [ckpt["embs"]]
            print(f"Resuming from checkpoint: {start_idx}/{len(texts)}", flush=True)
        else:
            print(f"Encoding {len(texts)} passages from scratch...", flush=True)

        batch_size = 32  # Small batch for CPU memory safety
        t0 = time.time()
        for i in range(start_idx, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            embs = model.encode(batch, normalize_embeddings=True,
                                show_progress_bar=False, batch_size=32)
            all_embs.append(embs)
            done = min(i + batch_size, len(texts))
            # Checkpoint every 2000 docs
            if done % 2000 < batch_size or done == len(texts):
                partial = np.vstack(all_embs)
                np.savez_compressed(ckpt_path, embs=partial, done=done)
                elapsed = time.time() - t0
                speed = (done - start_idx) / elapsed if elapsed > 0 else 0
                eta = (len(texts) - done) / speed if speed > 0 else 0
                print(f"  {done}/{len(texts)} "
                      f"({elapsed:.0f}s, {speed:.1f} docs/s, ETA {eta:.0f}s)",
                      flush=True)

        passage_embs = np.vstack(all_embs)
        np.savez_compressed(cache_path, embs=passage_embs)
        if os.path.exists(ckpt_path):
            os.remove(ckpt_path)
        total_time = time.time() - t0
        print(f"Encoding done: {passage_embs.shape} in {total_time:.1f}s", flush=True)

    # Free texts from memory
    gc.collect()

    # ── BM25 ─────────────────────────────────────────────────
    print("Building BM25 index...", flush=True)
    t0 = time.time()
    bm25_ids = list(corpus.keys())
    tokenized = [
        re.findall(r'\w+', f"{corpus[d].get('title', '')} "
                   f"{corpus[d].get('text', '')}".lower())
        for d in bm25_ids
    ]
    bm25 = BM25Okapi(tokenized)
    del tokenized
    gc.collect()
    print(f"BM25 built in {time.time() - t0:.1f}s", flush=True)

    def search_bm25(query, top_k=100):
        scores = bm25.get_scores(re.findall(r'\w+', query.lower()))
        top_idx = scores.argsort()[-top_k:][::-1]
        return [(bm25_ids[i], float(scores[i])) for i in top_idx if scores[i] > 0]

    def dense_search(query_text, top_k=100):
        q = model.encode(["query: " + query_text], normalize_embeddings=True)
        sims = (passage_embs @ q.T).flatten()
        idx = np.argsort(sims)[::-1][:top_k]
        return [(doc_id_list[i], float(sims[i])) for i in idx]

    # ── Cache queries ────────────────────────────────────────
    print(f"Caching {len(queries)} query results...", flush=True)
    cached = {}
    for qi, (qid, qt) in enumerate(queries.items()):
        rel = {d: r for d, r in qrels.get(qid, {}).items() if r > 0}
        cached[qid] = {
            "bm25": search_bm25(qt, top_k=100),
            "dense": dense_search(qt, top_k=100),
            "rel": rel,
        }
        if (qi + 1) % 100 == 0:
            print(f"  {qi+1}/{len(queries)}", flush=True)
    print(f"Cached {len(cached)} queries", flush=True)

    # ── Evaluate ─────────────────────────────────────────────
    def eval_strategy(fusion_fn, **kw):
        ndcgs, recalls = [], []
        for e in cached.values():
            fused = fusion_fn(e["bm25"], e["dense"], **kw)
            ranked = [d for d, _ in fused[:100]]
            ndcgs.append(ndcg_at_k(ranked, e["rel"], k=10))
            recalls.append(recall_at_k(ranked, e["rel"], k=100))
        return round(sum(ndcgs) / len(ndcgs), 4), round(sum(recalls) / len(recalls), 4)

    print("\n" + "=" * 60, flush=True)
    print("FiQA RESULTS", flush=True)
    print("=" * 60, flush=True)

    dense_ndcg, dense_recall = eval_strategy(lambda b, d: d)
    print(f"Dense only:      nDCG@10={dense_ndcg:.4f}  R@100={dense_recall:.4f}", flush=True)

    rrf_ndcg, rrf_recall = eval_strategy(simple_rrf, **BASELINE_RRF)
    print(f"Simple RRF:      nDCG@10={rrf_ndcg:.4f}  R@100={rrf_recall:.4f}", flush=True)

    rt_ndcg, rt_recall = eval_strategy(riverbed_tension, **FIXED_E5PT)
    print(f"RT (E5PT):       nDCG@10={rt_ndcg:.4f}  R@100={rt_recall:.4f}", flush=True)

    rtu_ndcg, rtu_recall = eval_strategy(riverbed_tension, **FIXED_UNIVERSAL)
    print(f"RT (Universal):  nDCG@10={rtu_ndcg:.4f}  R@100={rtu_recall:.4f}", flush=True)

    print(f"\nRT(E5PT) vs Dense:     {rt_ndcg - dense_ndcg:+.4f}", flush=True)
    print(f"RT(Universal) vs Dense: {rtu_ndcg - dense_ndcg:+.4f}", flush=True)
    print(f"RRF vs Dense:           {rrf_ndcg - dense_ndcg:+.4f}", flush=True)
    print(f"RT(E5PT) vs RRF:        {rt_ndcg - rrf_ndcg:+.4f}", flush=True)
    print(f"RT(Universal) vs RRF:   {rtu_ndcg - rrf_ndcg:+.4f}", flush=True)

    # ── Save ─────────────────────────────────────────────────
    out_dir = os.path.join(os.path.dirname(__file__), "results")
    os.makedirs(out_dir, exist_ok=True)

    output = {
        "dataset": "fiqa",
        "corpus_size": len(corpus),
        "num_queries": len(queries),
        "model": "intfloat/e5-base-unsupervised",
        "device": "cpu",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "params": {
            "e5pt": FIXED_E5PT,
            "universal": FIXED_UNIVERSAL,
            "rrf_baseline": BASELINE_RRF,
        },
        "results": {
            "dense_only": {"ndcg@10": dense_ndcg, "recall@100": dense_recall},
            "simple_rrf": {"ndcg@10": rrf_ndcg, "recall@100": rrf_recall},
            "rt_e5pt": {"ndcg@10": rt_ndcg, "recall@100": rt_recall},
            "rt_universal": {"ndcg@10": rtu_ndcg, "recall@100": rtu_recall},
        },
        "deltas": {
            "rt_e5pt_vs_dense": round(rt_ndcg - dense_ndcg, 4),
            "rt_universal_vs_dense": round(rtu_ndcg - dense_ndcg, 4),
            "rrf_vs_dense": round(rrf_ndcg - dense_ndcg, 4),
            "rt_e5pt_vs_rrf": round(rt_ndcg - rrf_ndcg, 4),
            "rt_universal_vs_rrf": round(rtu_ndcg - rrf_ndcg, 4),
        },
        "cross_dataset_combined": {
            "scifact": {"rt": 0.7578, "rrf": 0.7541, "dense": 0.7371},
            "nfcorpus": {"rt": 0.3680, "rrf": 0.3631, "dense": 0.3594},
            "fiqa": {
                "rt_e5pt": rt_ndcg, "rt_universal": rtu_ndcg,
                "rrf": rrf_ndcg, "dense": dense_ndcg,
            },
        },
    }

    out_path = os.path.join(out_dir, "cross_dataset_fiqa.json")
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nSaved: {out_path}", flush=True)
    print("DONE.", flush=True)


if __name__ == "__main__":
    main()
