"""Cross-dataset validation: Riverbed×Tension with FIXED parameters.

Proves the fusion algorithm generalises across domains, not just overfits scifact.
Datasets: scifact (science), nfcorpus (medical), fiqa (finance).
All use the SAME parameters — no per-dataset tuning.

Usage:
    python benchmarks/cross_dataset_validate.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
os.environ["BEIR_DEVICE"] = "cpu"

from beir import util  # noqa: E402
from beir.datasets.data_loader import GenericDataLoader  # noqa: E402
from beir_runner import (  # noqa: E402
    build_bm25_with_ids,
    ndcg_at_k,
    recall_at_k,
    search_bm25_by_ids,
)
from sentence_transformers import SentenceTransformer  # noqa: E402

# ── Dataset URLs ─────────────────────────────────────────────

DATASET_URLS = {
    "scifact": (
        "https://public.ukp.informatik.tu-darmstadt.de"
        "/thakur/BEIR/datasets/scifact.zip"
    ),
    "nfcorpus": (
        "https://public.ukp.informatik.tu-darmstadt.de"
        "/thakur/BEIR/datasets/nfcorpus.zip"
    ),
    "fiqa": (
        "https://public.ukp.informatik.tu-darmstadt.de"
        "/thakur/BEIR/datasets/fiqa.zip"
    ),
}

# ── Fixed parameters (same for ALL datasets) ────────────────

FIXED_PARAMS = {
    "k_low": 3,
    "k_high": 10,
    "top_n": 20,
    "boost_max": 1.2,
    "score_w": 0.5,
    "bw": 0.8,
    "dw": 1.4,
}

BASELINE_RRF_PARAMS = {
    "k": 5,
    "bw": 1.0,
    "dw": 1.2,
}

# ── Fusion functions ─────────────────────────────────────────


def simple_rrf(b, d, k=5, bw=1.0, dw=1.2):
    """Simple asymmetric RRF baseline."""
    scores = defaultdict(float)
    for rank, (did, _) in enumerate(b):
        scores[did] += bw / (k + rank + 1)
    for rank, (did, _) in enumerate(d):
        scores[did] += dw / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def riverbed_tension(b, d, k_low=2, k_high=10, top_n=10,
                     boost_max=1.3, score_w=0.3, bw=1.0, dw=1.2):
    """Riverbed×Tension fusion: adaptive RRF + score normalisation."""
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

    b_n = norm(b)
    d_n = norm(d)
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


def dense_only_passthrough(b, d):
    """Dense-only: return dense results as-is (ignore BM25)."""
    return d


# ── Core logic ───────────────────────────────────────────────


def load_or_encode_embeddings(model, corpus, dataset_name, base_dir):
    """Encode corpus passages with caching to .npz file."""
    cache_path = os.path.join(base_dir, f".cache_{dataset_name}_e5pt_base_embs.npz")
    doc_id_list = list(corpus.keys())

    if os.path.exists(cache_path):
        print(f"  Loading cached embeddings: {cache_path}", flush=True)
        data = np.load(cache_path)
        return data["embs"], doc_id_list

    print(f"  Encoding {len(corpus)} passages (this may take a while on CPU)...",
          flush=True)
    texts = []
    for doc_id in doc_id_list:
        doc = corpus[doc_id]
        title = doc.get("title", "")
        text = doc.get("text", "")
        texts.append(f"passage: {title} {text}".strip())

    # Checkpoint: resume from partial encoding
    ckpt_path = cache_path + ".ckpt.npz"
    start_idx = 0
    all_embs = []
    if os.path.exists(ckpt_path):
        ckpt = np.load(ckpt_path)
        start_idx = int(ckpt["done"])
        all_embs = [ckpt["embs"]]
        print(f"  Resuming from checkpoint: {start_idx}/{len(texts)}", flush=True)

    batch_size = 64
    for i in range(start_idx, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        embs = model.encode(batch, normalize_embeddings=True,
                            show_progress_bar=False, batch_size=32)
        all_embs.append(embs)
        done = min(i + batch_size, len(texts))
        # Save checkpoint every 2000 docs
        if done % 2000 == 0 or done == len(texts):
            partial = np.vstack(all_embs)
            np.savez_compressed(ckpt_path, embs=partial, done=done)
            print(f"    Encoded {done}/{len(texts)} (checkpoint saved)",
                  flush=True)

    passage_embs = np.vstack(all_embs)
    np.savez_compressed(cache_path, embs=passage_embs)
    # Remove checkpoint after final save
    if os.path.exists(ckpt_path):
        os.remove(ckpt_path)
    print(f"  Saved embeddings cache: {cache_path}", flush=True)
    return passage_embs, doc_id_list


def evaluate_dataset(dataset_name, model, base_dir):
    """Run all three strategies on one dataset, return metrics dict."""
    print(f"\n{'='*60}", flush=True)
    print(f"Dataset: {dataset_name}", flush=True)
    print(f"{'='*60}", flush=True)

    # Download if needed
    data_path = os.path.join(base_dir, dataset_name)
    if not os.path.isdir(data_path):
        print(f"  Downloading {dataset_name}...", flush=True)
        data_path = util.download_and_unzip(DATASET_URLS[dataset_name], base_dir)

    corpus, queries, qrels = GenericDataLoader(data_path).load(split="test")
    print(f"  Corpus: {len(corpus)} docs | Queries: {len(queries)}", flush=True)

    # BM25
    print("  Building BM25 index...", flush=True)
    t0 = time.time()
    bm25, doc_ids = build_bm25_with_ids(corpus)
    print(f"  BM25 built in {time.time() - t0:.1f}s", flush=True)

    # Dense embeddings
    passage_embs, doc_id_list = load_or_encode_embeddings(
        model, corpus, dataset_name, base_dir
    )

    def dense_search(query_text, top_k=100):
        q = model.encode(["query: " + query_text], normalize_embeddings=True)
        sims = (passage_embs @ q.T).flatten()
        idx = np.argsort(sims)[::-1][:top_k]
        return [(doc_id_list[i], float(sims[i])) for i in idx]

    # Pre-cache all queries
    print("  Caching query results...", flush=True)
    cached = {}
    for qid, qt in queries.items():
        rel = {d: r for d, r in qrels.get(qid, {}).items() if r > 0}
        cached[qid] = {
            "bm25": search_bm25_by_ids(bm25, doc_ids, qt, top_k=100),
            "dense": dense_search(qt, top_k=100),
            "rel": rel,
        }
    print(f"  Cached {len(cached)} queries", flush=True)

    # Evaluate a fusion function
    def eval_strategy(fusion_fn, **kw):
        ndcgs = []
        recalls = []
        for e in cached.values():
            fused = fusion_fn(e["bm25"], e["dense"], **kw)
            ranked = [d for d, _ in fused[:100]]
            ndcgs.append(ndcg_at_k(ranked, e["rel"], k=10))
            recalls.append(recall_at_k(ranked, e["rel"], k=100))
        avg_ndcg = sum(ndcgs) / len(ndcgs) if ndcgs else 0.0
        avg_recall = sum(recalls) / len(recalls) if recalls else 0.0
        return round(avg_ndcg, 4), round(avg_recall, 4)

    # 1. Simple RRF baseline
    rrf_ndcg, rrf_recall = eval_strategy(simple_rrf, **BASELINE_RRF_PARAMS)
    print(f"  Simple RRF:          nDCG@10={rrf_ndcg:.4f}  R@100={rrf_recall:.4f}",
          flush=True)

    # 2. Riverbed×Tension (fixed params)
    rt_ndcg, rt_recall = eval_strategy(riverbed_tension, **FIXED_PARAMS)
    print(f"  Riverbed×Tension:    nDCG@10={rt_ndcg:.4f}  R@100={rt_recall:.4f}",
          flush=True)

    # 3. Dense only
    dense_ndcg, dense_recall = eval_strategy(dense_only_passthrough)
    print(f"  Dense only:          nDCG@10={dense_ndcg:.4f}  R@100={dense_recall:.4f}",
          flush=True)

    # Delta vs dense
    rt_delta = rt_ndcg - dense_ndcg
    rrf_delta = rrf_ndcg - dense_ndcg
    print(f"  RT delta vs dense:   {rt_delta:+.4f}", flush=True)
    print(f"  RRF delta vs dense:  {rrf_delta:+.4f}", flush=True)

    return {
        "dataset": dataset_name,
        "corpus_size": len(corpus),
        "num_queries": len(queries),
        "simple_rrf": {"ndcg@10": rrf_ndcg, "recall@100": rrf_recall},
        "riverbed_tension": {"ndcg@10": rt_ndcg, "recall@100": rt_recall},
        "dense_only": {"ndcg@10": dense_ndcg, "recall@100": dense_recall},
        "rt_delta_vs_dense": round(rt_delta, 4),
        "rrf_delta_vs_dense": round(rrf_delta, 4),
    }


def main():
    datasets = ["scifact", "nfcorpus", "fiqa"]
    base_dir = os.path.join(os.path.dirname(__file__), "datasets")
    os.makedirs(base_dir, exist_ok=True)

    print("Cross-Dataset Validation: Riverbed×Tension Fusion", flush=True)
    print(f"Fixed params: {FIXED_PARAMS}", flush=True)
    print(f"Baseline RRF: {BASELINE_RRF_PARAMS}", flush=True)
    print(f"Datasets: {datasets}", flush=True)
    print("Model: intfloat/e5-base-unsupervised (CPU)", flush=True)

    # Load model once, reuse across datasets
    print("\nLoading SentenceTransformer...", flush=True)
    model = SentenceTransformer("intfloat/e5-base-unsupervised", device="cpu")
    print("Model loaded.", flush=True)

    all_results = {}
    for ds in datasets:
        t0 = time.time()
        result = evaluate_dataset(ds, model, base_dir)
        result["elapsed_s"] = round(time.time() - t0, 1)
        all_results[ds] = result

    # ── Summary table ────────────────────────────────────────
    print(f"\n{'='*72}", flush=True)
    print("CROSS-DATASET COMPARISON (same params for all)", flush=True)
    print(f"{'='*72}", flush=True)
    print(f"Params: kl={FIXED_PARAMS['k_low']} kh={FIXED_PARAMS['k_high']} "
          f"tn={FIXED_PARAMS['top_n']} bm={FIXED_PARAMS['boost_max']} "
          f"sw={FIXED_PARAMS['score_w']} bw={FIXED_PARAMS['bw']} "
          f"dw={FIXED_PARAMS['dw']}", flush=True)
    print(flush=True)

    cols = ["Dataset", "Dense nDCG", "RRF nDCG", "RT nDCG", "RT delta", "RT R@100"]
    header = (
        f"{cols[0]:<12} {cols[1]:>11} {cols[2]:>11} "
        f"{cols[3]:>11} {cols[4]:>10} {cols[5]:>10}"
    )
    print(header, flush=True)
    print("-" * len(header), flush=True)

    rt_wins = 0
    for ds in datasets:
        r = all_results[ds]
        dense_n = r["dense_only"]["ndcg@10"]
        rrf_n = r["simple_rrf"]["ndcg@10"]
        rt_n = r["riverbed_tension"]["ndcg@10"]
        rt_d = r["rt_delta_vs_dense"]
        rt_r = r["riverbed_tension"]["recall@100"]
        marker = " *" if rt_n > rrf_n else ""
        row = (
            f"{ds:<12} {dense_n:>11.4f} {rrf_n:>11.4f} "
            f"{rt_n:>11.4f} {rt_d:>+10.4f} {rt_r:>10.4f}{marker}"
        )
        print(row, flush=True)
        if rt_n > rrf_n:
            rt_wins += 1

    print(flush=True)
    print(f"RT beats RRF on {rt_wins}/{len(datasets)} datasets", flush=True)
    print("(* = RT > RRF)", flush=True)

    # Averages
    n = len(datasets)
    avg_dense = sum(
        all_results[ds]["dense_only"]["ndcg@10"] for ds in datasets
    ) / n
    avg_rrf = sum(
        all_results[ds]["simple_rrf"]["ndcg@10"] for ds in datasets
    ) / n
    avg_rt = sum(
        all_results[ds]["riverbed_tension"]["ndcg@10"] for ds in datasets
    ) / n
    avg_rt_recall = sum(
        all_results[ds]["riverbed_tension"]["recall@100"] for ds in datasets
    ) / n

    print(f"\nAverages across {len(datasets)} datasets:", flush=True)
    print(f"  Dense nDCG@10:  {avg_dense:.4f}", flush=True)
    print(f"  RRF nDCG@10:    {avg_rrf:.4f}", flush=True)
    print(f"  RT nDCG@10:     {avg_rt:.4f} (delta vs dense: {avg_rt - avg_dense:+.4f})",
          flush=True)
    print(f"  RT Recall@100:  {avg_rt_recall:.4f}", flush=True)

    # Save results
    out_dir = os.path.join(os.path.dirname(__file__), "results")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "cross_dataset_validation.json")

    output = {
        "fixed_params": FIXED_PARAMS,
        "baseline_rrf_params": BASELINE_RRF_PARAMS,
        "model": "intfloat/e5-base-unsupervised",
        "device": "cpu",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "datasets": all_results,
        "averages": {
            "dense_ndcg@10": round(avg_dense, 4),
            "rrf_ndcg@10": round(avg_rrf, 4),
            "rt_ndcg@10": round(avg_rt, 4),
            "rt_recall@100": round(avg_rt_recall, 4),
            "rt_delta_vs_dense": round(avg_rt - avg_dense, 4),
            "rt_wins_vs_rrf": rt_wins,
        },
    }

    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nSaved to {out_path}", flush=True)


if __name__ == "__main__":
    main()
