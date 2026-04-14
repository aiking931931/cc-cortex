"""
J-3: Evaluate all 9 datasets with FIXED_UNIVERSAL params.
Proves generalization (same params work everywhere).
Uses existing encoding cache — no GPU needed.

Usage: python eval_universal_params.py --cache-dir ./cache
"""

import argparse
import json
import os
import time
from collections import defaultdict

import bm25s
import numpy as np
import pytrec_eval
import torch
from beir import util
from beir.datasets.data_loader import GenericDataLoader
from sentence_transformers import SentenceTransformer

MODEL_ID = "intfloat/e5-base-unsupervised"
PREFIX_Q = "query: "
BEIR_BASE = (
    "https://public.ukp.informatik.tu-darmstadt.de"
    "/thakur/BEIR/datasets"
)
CQA_FORUMS = [
    "android", "english", "gaming", "gis", "mathematica",
    "physics", "programmers", "stats", "tex",
    "unix", "webmasters", "wordpress",
]
METRICS = {"ndcg_cut_10", "recall_100", "map"}

# ── FIXED_UNIVERSAL params (patent core) ──
UNIVERSAL = {
    "k_low": 2, "k_high": 5, "top_n": 20,
    "boost_max": 1.2, "score_w": 0.5,
    "bw": 0.8, "dw": 1.0,
}


def _norm(results):
    if not results:
        return {}
    vals = [s for _, s in results]
    mn, mx = min(vals), max(vals)
    rng = mx - mn if mx > mn else 1.0
    return {did: (s - mn) / rng for did, s in results}


def simple_rrf(b, d, k=5, bw=0.8, dw=1.0):
    scores = defaultdict(float)
    for rank, (did, _) in enumerate(b):
        scores[did] += bw / (k + rank + 1)
    for rank, (did, _) in enumerate(d):
        scores[did] += dw / (k + rank + 1)
    return sorted(
        scores.items(), key=lambda x: x[1], reverse=True,
    )


def riverbed_only(b, d, bw=0.8, dw=1.0):
    b_n, d_n = _norm(b), _norm(d)
    all_docs = set(b_n) | set(d_n)
    tw = bw + dw
    return sorted(
        {
            did: (
                bw * b_n.get(did, 0) + dw * d_n.get(did, 0)
            ) / tw
            for did in all_docs
        }.items(),
        key=lambda x: x[1],
        reverse=True,
    )


def rt_universal(b, d):
    p = UNIVERSAL
    b_set = {did for did, _ in b[:p["top_n"]]}
    d_set = {did for did, _ in d[:p["top_n"]]}
    union = b_set | d_set
    agreement = (
        len(b_set & d_set) / len(union) if union else 0.0
    )
    tension = 1.0 - agreement
    adaptive_k = max(
        1,
        int(p["k_low"] + (p["k_high"] - p["k_low"]) * tension),
    )
    boost = 1.0 + (p["boost_max"] - 1.0) * agreement
    rrf_scores = defaultdict(float)
    presence = defaultdict(int)
    for rank, (did, _) in enumerate(b):
        rrf_scores[did] += p["bw"] / (adaptive_k + rank + 1)
        presence[did] += 1
    for rank, (did, _) in enumerate(d):
        rrf_scores[did] += p["dw"] / (adaptive_k + rank + 1)
        presence[did] += 1
    for did in rrf_scores:
        if presence[did] >= 2:
            rrf_scores[did] *= boost
    b_n, d_n = _norm(b), _norm(d)
    rv = list(rrf_scores.values())
    r_mn, r_mx = min(rv), max(rv)
    r_rng = r_mx - r_mn if r_mx > r_mn else 1.0
    tw = p["bw"] + p["dw"]
    all_docs = set(rrf_scores) | set(b_n) | set(d_n)
    final = {}
    for did in all_docs:
        r = (rrf_scores.get(did, 0) - r_mn) / r_rng
        s = (
            p["bw"] * b_n.get(did, 0)
            + p["dw"] * d_n.get(did, 0)
        ) / tw
        final[did] = (1 - p["score_w"]) * r + p["score_w"] * s
    return sorted(
        final.items(), key=lambda x: x[1], reverse=True,
    )


STRATEGIES = {
    "dense_only": lambda b, d: d,
    "rrf_univ": lambda b, d: simple_rrf(b, d),
    "riverbed_univ": lambda b, d: riverbed_only(b, d),
    "rt_univ": lambda b, d: rt_universal(b, d),
}


def evaluate_official(qrels, run_dict):
    qrels_int = {
        qid: {did: int(rel) for did, rel in rels.items()}
        for qid, rels in qrels.items()
    }
    evaluator = pytrec_eval.RelevanceEvaluator(qrels_int, METRICS)
    scores = evaluator.evaluate(run_dict)
    result = {}
    for metric in METRICS:
        vals = [scores[qid].get(metric, 0) for qid in scores]
        result[metric] = round(sum(vals) / len(vals), 6)
    return result


def evaluate_dataset(ds_name, corpus, queries, qrels,
                     cache_dir, model):
    # Load cached embeddings
    embs_path = os.path.join(cache_dir, f"{ds_name}_embs.npz")
    ids_path = os.path.join(cache_dir, f"{ds_name}_doc_ids.json")
    if not os.path.exists(embs_path):
        print(f"  SKIP {ds_name}: no cache")
        return None
    passage_embs = np.load(embs_path)["embs"]
    with open(ids_path) as f:
        doc_id_list = json.load(f)
    print(f"  Cache: {passage_embs.shape}")

    # BM25
    print("  BM25 (bm25s)...")
    doc_ids = list(corpus.keys())
    corpus_texts = [
        f"{corpus[did].get('title', '')} "
        f"{corpus[did].get('text', '')}"
        for did in doc_ids
    ]
    corpus_tokens = bm25s.tokenize(corpus_texts)
    bm25_idx = bm25s.BM25()
    bm25_idx.index(corpus_tokens)

    def search_bm25(query, top_k=100):
        qt = bm25s.tokenize([query])
        results, scores = bm25_idx.retrieve(  # noqa: F821
            qt, corpus=doc_ids, k=top_k,
        )
        return [
            (str(results[0, i]), float(scores[0, i]))
            for i in range(len(results[0]))
            if scores[0, i] > 0
        ]

    # Batch encode queries on GPU
    print(f"  Encoding {len(queries)} queries...")
    qid_list = list(queries.keys())
    qt_list = [PREFIX_Q + queries[qid] for qid in qid_list]
    all_q_embs = model.encode(
        qt_list, normalize_embeddings=True,
        batch_size=256, show_progress_bar=True,
    )

    # Dense retrieval
    print("  Dense retrieval...")
    all_sims = passage_embs @ all_q_embs.T

    # BM25 + fusion
    print(f"  Fusion ({len(queries)} queries)...")
    cached = {}
    t0 = time.time()
    for qi, qid in enumerate(qid_list):
        bm25_res = search_bm25(queries[qid], top_k=100)
        sims = all_sims[:, qi]
        idx = np.argsort(sims)[::-1][:100]
        dense_res = [
            (doc_id_list[i], float(sims[i])) for i in idx
        ]
        cached[qid] = {"bm25": bm25_res, "dense": dense_res}
        if (qi + 1) % 500 == 0:
            elapsed = time.time() - t0
            speed = (qi + 1) / elapsed
            print(f"    {qi + 1}/{len(queries)} ({speed:.1f} q/s)")
    print(f"  Fusion done in {time.time() - t0:.1f}s")

    ds_results = {}
    for strat_name, strat_fn in STRATEGIES.items():
        run_dict = {}
        for qid in qid_list:
            e = cached[qid]
            fused = strat_fn(e["bm25"], e["dense"])
            run_dict[qid] = {
                did: float(score)
                for did, score in fused[:100]
            }
        metrics = evaluate_official(qrels, run_dict)
        ds_results[strat_name] = metrics
        print(
            f"  {strat_name:<20} "
            f"nDCG@10={metrics['ndcg_cut_10']:.4f}"
        )
    best = max(
        ds_results.items(),
        key=lambda x: x[1]["ndcg_cut_10"],
    )
    print(f"  >>> BEST: {best[0]} = {best[1]['ndcg_cut_10']:.4f}")
    del cached, bm25_idx
    return ds_results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir", default="./cache")
    parser.add_argument("--data-dir", default="./datasets")
    parser.add_argument(
        "--output", default="universal_params_results.json",
    )
    args = parser.parse_args()

    print(f"FIXED_UNIVERSAL params: {UNIVERSAL}")

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Device: {device}")
    model = SentenceTransformer(MODEL_ID, device=device)

    # Load datasets
    ds_urls = {
        "trec-covid": f"{BEIR_BASE}/trec-covid.zip",
        "webis-touche2020": f"{BEIR_BASE}/webis-touche2020.zip",
        "quora": f"{BEIR_BASE}/quora.zip",
    }

    all_datasets = {}
    for ds_name, url in ds_urls.items():
        data_path = os.path.join(args.data_dir, ds_name)
        if not os.path.isdir(data_path):
            data_path = util.download_and_unzip(url, args.data_dir)
        corpus, queries, qrels = GenericDataLoader(
            data_path,
        ).load(split="test")
        all_datasets[ds_name] = (corpus, queries, qrels)
        print(f"{ds_name}: {len(corpus)} docs, {len(queries)} q")

    cqa_path = os.path.join(args.data_dir, "cqadupstack")
    if not os.path.isdir(cqa_path):
        cqa_path = util.download_and_unzip(
            f"{BEIR_BASE}/cqadupstack.zip", args.data_dir,
        )
    for forum in CQA_FORUMS:
        forum_path = os.path.join(cqa_path, forum)
        corpus, queries, qrels = GenericDataLoader(
            forum_path,
        ).load(split="test")
        all_datasets[f"cqa_{forum}"] = (corpus, queries, qrels)

    # Evaluate all
    all_results = {}
    for ds_name, (corpus, queries, qrels) in all_datasets.items():
        print(f"\n{'=' * 50}\n{ds_name.upper()}\n{'=' * 50}")
        r = evaluate_dataset(
            ds_name, corpus, queries, qrels,
            args.cache_dir, model,
        )
        if r:
            all_results[ds_name] = r

    # CQA average
    cqa_names = [f"cqa_{f}" for f in CQA_FORUMS]
    cqa_avg = {}
    for strat in STRATEGIES:
        avg = {}
        for metric in METRICS:
            vals = [
                all_results[n][strat][metric]
                for n in cqa_names if n in all_results
            ]
            if vals:
                avg[metric] = round(sum(vals) / len(vals), 6)
        cqa_avg[strat] = avg
    all_results["cqadupstack"] = cqa_avg

    # Summary
    print(f"\n{'=' * 70}")
    print("UNIVERSAL PARAMS — All datasets")
    print(f"{'=' * 70}")
    simple_names = list(ds_urls.keys()) + ["cqadupstack"]
    wins = 0
    for ds_name in simple_names:
        r = all_results[ds_name]
        best_s = max(r, key=lambda s: r[s]["ndcg_cut_10"])
        best_v = r[best_s]["ndcg_cut_10"]
        dense_v = r["dense_only"]["ndcg_cut_10"]
        won = best_v > dense_v
        if won:
            wins += 1
        print(
            f"  {ds_name:<20} dense={dense_v:.4f}  "
            f"best={best_s}={best_v:.4f}  "
            f"{'WIN' if won else 'LOSE'}  "
            f"{best_v - dense_v:+.4f}"
        )
    print(f"\nUniversal beats Dense: {wins}/{len(simple_names)}")

    # Save
    output = {
        "experiment": "universal_params_all_datasets",
        "params": UNIVERSAL,
        "model": MODEL_ID,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "results": all_results,
    }
    with open(args.output, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nSaved to {args.output}")


if __name__ == "__main__":
    main()
