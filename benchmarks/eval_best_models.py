"""
J-2: Evaluate with domain-best models.
Each dataset uses the strongest model for that domain.
GPU batch encode + bm25s multi-core.

Usage: python eval_best_models.py --cache-dir ./cache
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

PREFIX_Q_MAP = {
    "intfloat/e5-base-unsupervised": ("query: ", "passage: "),
    "intfloat/e5-large-unsupervised": ("query: ", "passage: "),
    "pritamdeka/PubMedBERT-mnli-snli-scinli-scitail-mednli-stsb": ("", ""),
    "thenlper/gte-large": ("", ""),
    "BAAI/bge-base-en-v1.5": (
        "Represent this sentence for searching: ", ""
    ),
    "BAAI/bge-large-en-v1.5": (
        "Represent this sentence for searching: ", ""
    ),
}

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

# Models per dataset (domain-best)
DATASET_MODELS = {
    "trec-covid": [
        "BAAI/bge-large-en-v1.5",
        "thenlper/gte-large",
    ],
    "webis-touche2020": [
        "BAAI/bge-large-en-v1.5",
        "thenlper/gte-large",
    ],
    "quora": [
        "BAAI/bge-large-en-v1.5",
        "thenlper/gte-large",
    ],
}

# Universal params
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


def riverbed(b, d, bw=0.8, dw=1.0):
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
        key=lambda x: x[1], reverse=True,
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
    "rrf": lambda b, d: simple_rrf(b, d),
    "riverbed": lambda b, d: riverbed(b, d),
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


def encode_corpus(model, corpus, prefix_d, cache_key, cache_dir):
    cache_path = os.path.join(cache_dir, f"{cache_key}_embs.npz")
    ids_path = os.path.join(cache_dir, f"{cache_key}_doc_ids.json")
    doc_id_list = list(corpus.keys())

    if os.path.exists(cache_path) and os.path.exists(ids_path):
        data = np.load(cache_path)
        print(f"  Cache hit: {data['embs'].shape}")
        return data["embs"], doc_id_list

    texts = [
        f"{prefix_d}"
        f"{corpus[did].get('title', '')} "
        f"{corpus[did].get('text', '')}".strip()
        for did in doc_id_list
    ]

    print(f"  Encoding {len(texts)} docs...")
    t0 = time.time()
    embs = model.encode(
        texts, normalize_embeddings=True,
        batch_size=256, show_progress_bar=True,
    )
    elapsed = time.time() - t0
    print(f"  Done: {embs.shape} in {elapsed:.0f}s")

    np.savez_compressed(cache_path, embs=embs)
    with open(ids_path, "w") as f:
        json.dump(doc_id_list, f)
    return embs, doc_id_list


def evaluate_dataset(ds_name, corpus, queries, qrels,
                     passage_embs, doc_id_list, model, prefix_q):
    doc_ids = list(corpus.keys())

    # BM25
    print("  BM25 (bm25s)...")
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

    # Batch encode queries
    print(f"  Encoding {len(queries)} queries...")
    qid_list = list(queries.keys())
    qt_list = [prefix_q + queries[qid] for qid in qid_list]
    all_q_embs = model.encode(
        qt_list, normalize_embeddings=True,
        batch_size=256, show_progress_bar=True,
    )

    # Dense retrieval (GPU accelerated for large matrices)
    # （大矩陣用 GPU 加速）
    print("  Dense retrieval...")
    if (
        torch.cuda.is_available()
        and passage_embs.shape[0] * len(qid_list) > 1e8
    ):
        p_t = torch.from_numpy(passage_embs).cuda()
        q_t = torch.from_numpy(all_q_embs).cuda()
        all_sims = (p_t @ q_t.T).cpu().numpy()
        del p_t, q_t
        torch.cuda.empty_cache()
    else:
        all_sims = passage_embs @ all_q_embs.T

    # Fusion
    print(f"  Fusion ({len(queries)} queries)...")
    cached = {}
    for qi, qid in enumerate(qid_list):
        bm25_res = search_bm25(queries[qid], top_k=100)
        sims = all_sims[:, qi]
        idx = np.argsort(sims)[::-1][:100]
        dense_res = [
            (doc_id_list[i], float(sims[i])) for i in idx
        ]
        cached[qid] = {"bm25": bm25_res, "dense": dense_res}

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
            f"  {strat_name:<15} "
            f"nDCG@10={metrics['ndcg_cut_10']:.4f}"
        )
    best = max(
        ds_results.items(),
        key=lambda x: x[1]["ndcg_cut_10"],
    )
    print(
        f"  >>> BEST: {best[0]} = "
        f"{best[1]['ndcg_cut_10']:.4f}"
    )
    del cached, bm25_idx
    return ds_results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir", default="./cache")
    parser.add_argument("--data-dir", default="./datasets")
    parser.add_argument(
        "--output", default="best_models_results.json",
    )
    args = parser.parse_args()

    os.makedirs(args.cache_dir, exist_ok=True)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Device: {device}")

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
        print(
            f"{ds_name}: {len(corpus)} docs, "
            f"{len(queries)} queries"
        )

    all_results = {}
    for ds_name, model_ids in DATASET_MODELS.items():
        corpus, queries, qrels = all_datasets[ds_name]
        for model_id in model_ids:
            prefixes = PREFIX_Q_MAP.get(model_id, ("", ""))
            prefix_q, prefix_d = prefixes
            tag = model_id.replace("/", "_").replace("-", "_")
            cache_key = f"{ds_name}_{tag}"

            print(
                f"\n{'=' * 60}\n"
                f"{ds_name.upper()} + {model_id}\n"
                f"{'=' * 60}"
            )

            model = SentenceTransformer(model_id, device=device)
            embs, doc_ids = encode_corpus(
                model, corpus, prefix_d,
                cache_key, args.cache_dir,
            )
            results = evaluate_dataset(
                ds_name, corpus, queries, qrels,
                embs, doc_ids, model, prefix_q,
            )
            key = f"{ds_name}__{tag}"
            all_results[key] = {
                "model": model_id,
                "dataset": ds_name,
                "results": results,
            }
            del model, embs
            torch.cuda.empty_cache()

    # Summary
    print(f"\n{'=' * 70}")
    print("BEST MODELS — Summary")
    print(f"{'=' * 70}")
    for key, data in all_results.items():
        r = data["results"]
        best_s = max(r, key=lambda s: r[s]["ndcg_cut_10"])
        best_v = r[best_s]["ndcg_cut_10"]
        dense_v = r["dense_only"]["ndcg_cut_10"]
        print(
            f"  {data['dataset']:<20} "
            f"{data['model']:<40} "
            f"dense={dense_v:.4f}  "
            f"best={best_s}={best_v:.4f}  "
            f"{best_v - dense_v:+.4f}"
        )

    with open(args.output, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\nSaved to {args.output}")


if __name__ == "__main__":
    main()
