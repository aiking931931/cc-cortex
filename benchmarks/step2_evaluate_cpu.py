"""
Step 2: BM25 + fusion + evaluation (CPU machine)
Usage: python step2_evaluate_cpu.py [--cache-dir ./cache]
Requires: encoding cache from step1_encode_gpu.py
"""

import argparse
import json
import os
import re
import time
from collections import defaultdict

import numpy as np
import pytrec_eval
from beir import util
from beir.datasets.data_loader import GenericDataLoader
from rank_bm25 import BM25Okapi
from sentence_transformers import SentenceTransformer

MODEL_ID = "intfloat/e5-base-unsupervised"
PREFIX_Q = "query: "
BEIR_BASE = (
    "https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets"
)
CQA_FORUMS = [
    "android", "english", "gaming", "gis", "mathematica",
    "physics", "programmers", "stats", "tex",
    "unix", "webmasters", "wordpress",
]
METRICS = {"ndcg_cut_10", "recall_100", "map"}

BATCH_A = {
    "scifact": {
        "riverbed": 0.7576, "rt_full": 0.7557,
        "rrf": 0.7503, "dense_only": 0.7371,
    },
    "nfcorpus": {
        "riverbed": 0.3633, "rt_full": 0.3666,
        "rrf": 0.3609, "dense_only": 0.3585,
    },
    "arguana": {
        "riverbed": 0.3286, "rt_full": 0.3318,
        "rrf": 0.3347, "dense_only": 0.3174,
    },
    "scidocs": {
        "riverbed": 0.2116, "rt_full": 0.2110,
        "rrf": 0.2056, "dense_only": 0.2110,
    },
    "fiqa": {
        "riverbed": 0.4160, "rt_full": 0.4122,
        "rrf": 0.3962, "dense_only": 0.4008,
    },
}


# ── Fusion functions ──


def _norm(results):
    if not results:
        return {}
    vals = [s for _, s in results]
    mn, mx = min(vals), max(vals)
    rng = mx - mn if mx > mn else 1.0
    return {did: (s - mn) / rng for did, s in results}


def simple_rrf(b, d, k=5, bw=1.0, dw=1.2):
    scores = defaultdict(float)
    for rank, (did, _) in enumerate(b):
        scores[did] += bw / (k + rank + 1)
    for rank, (did, _) in enumerate(d):
        scores[did] += dw / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def riverbed_only(b, d, bw=0.8, dw=1.4):
    b_n, d_n = _norm(b), _norm(d)
    all_docs = set(b_n) | set(d_n)
    tw = bw + dw
    return sorted(
        {
            did: (bw * b_n.get(did, 0) + dw * d_n.get(did, 0)) / tw
            for did in all_docs
        }.items(),
        key=lambda x: x[1],
        reverse=True,
    )


def rt_full(
    b, d, k_low=3, k_high=10, top_n=20,
    boost_max=1.2, score_w=0.5, bw=0.8, dw=1.4,
):
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
    b_n, d_n = _norm(b), _norm(d)
    rv = list(rrf_scores.values())
    r_mn, r_mx = min(rv), max(rv)
    r_rng = r_mx - r_mn if r_mx > r_mn else 1.0
    tw = bw + dw
    all_docs = set(rrf_scores) | set(b_n) | set(d_n)
    final = {}
    for did in all_docs:
        r = (rrf_scores.get(did, 0) - r_mn) / r_rng
        s = (bw * b_n.get(did, 0) + dw * d_n.get(did, 0)) / tw
        final[did] = (1 - score_w) * r + score_w * s
    return sorted(final.items(), key=lambda x: x[1], reverse=True)


STRATEGIES = {
    "dense_only": lambda b, d: d,
    "rrf": lambda b, d: simple_rrf(b, d),
    "riverbed": lambda b, d: riverbed_only(b, d),
    "rt_full": lambda b, d: rt_full(b, d),
}


def tokenize(text):
    return re.findall(r"\w+", text.lower())


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


def load_cache(cache_dir, ds_name):
    embs_path = os.path.join(cache_dir, f"{ds_name}_embs.npz")
    ids_path = os.path.join(cache_dir, f"{ds_name}_doc_ids.json")
    if not os.path.exists(embs_path) or not os.path.exists(ids_path):
        msg = f"Missing cache for {ds_name}. Run step1 first."
        raise FileNotFoundError(msg)
    embs = np.load(embs_path)["embs"]
    with open(ids_path) as f:
        doc_ids = json.load(f)
    print(f"  Loaded cache: {embs.shape}")
    return embs, doc_ids


_model = None


def _get_model():
    global _model  # noqa: PLW0603
    if _model is None:
        print("  Loading model for query encoding (CPU)...")
        _model = SentenceTransformer(MODEL_ID, device="cpu")
    return _model


def evaluate_dataset(ds_name, corpus, queries, qrels, cache_dir):
    passage_embs, doc_id_list = load_cache(cache_dir, ds_name)

    print("  Building BM25...")
    doc_ids = list(corpus.keys())
    tokenized = [
        tokenize(
            f"{corpus[did].get('title', '')} "
            f"{corpus[did].get('text', '')}"
        )
        for did in doc_ids
    ]
    bm25_index = BM25Okapi(tokenized)

    def search_bm25(query, top_k=100):
        scores = bm25_index.get_scores(tokenize(query))  # noqa: F821
        top_idx = scores.argsort()[-top_k:][::-1]
        return [
            (doc_ids[i], float(scores[i]))
            for i in top_idx if scores[i] > 0
        ]

    model = _get_model()

    print(f"  Processing {len(queries)} queries...")
    cached = {}
    t0 = time.time()
    for qi, (qid, qt) in enumerate(queries.items()):
        bm25_res = search_bm25(qt, top_k=100)
        q_emb = model.encode(
            [PREFIX_Q + qt], normalize_embeddings=True,
        )
        sims = (passage_embs @ q_emb.T).flatten()
        idx = np.argsort(sims)[::-1][:100]
        dense_res = [(doc_id_list[i], float(sims[i])) for i in idx]
        cached[qid] = {"bm25": bm25_res, "dense": dense_res}
        if (qi + 1) % 100 == 0:
            elapsed = time.time() - t0
            speed = (qi + 1) / elapsed
            eta = (len(queries) - qi - 1) / speed
            print(
                f"    {qi + 1}/{len(queries)} "
                f"({speed:.1f} q/s, ETA {eta:.0f}s)"
            )
    print(f"  Done in {time.time() - t0:.1f}s")

    ds_results = {}
    for strat_name, strat_fn in STRATEGIES.items():
        run_dict = {}
        for qid in queries:
            e = cached[qid]
            fused = strat_fn(e["bm25"], e["dense"])
            run_dict[qid] = {
                did: float(score) for did, score in fused[:100]
            }
        metrics = evaluate_official(qrels, run_dict)
        ds_results[strat_name] = metrics
        print(
            f"  {strat_name:<15} "
            f"nDCG@10={metrics['ndcg_cut_10']:.4f}  "
            f"MAP={metrics['map']:.4f}"
        )
    best = max(ds_results.items(), key=lambda x: x[1]["ndcg_cut_10"])
    print(f"  >>> BEST: {best[0]} = {best[1]['ndcg_cut_10']:.4f}")
    del cached, bm25_index
    return ds_results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir", default="./cache")
    parser.add_argument("--data-dir", default="./datasets")
    parser.add_argument("--output", default="batch_b_results.json")
    args = parser.parse_args()

    os.makedirs(args.data_dir, exist_ok=True)

    expected = (
        ["trec-covid", "webis-touche2020", "quora"]
        + [f"cqa_{f}" for f in CQA_FORUMS]
    )
    missing = [
        n for n in expected
        if not os.path.exists(
            os.path.join(args.cache_dir, f"{n}_embs.npz")
        )
    ]
    if missing:
        print(f"ERROR: Missing cache for: {missing}")
        print("Run step1_encode_gpu.py first.")
        return

    print(f"All {len(expected)} cache files found.\n")

    ds_urls = {
        "trec-covid": f"{BEIR_BASE}/trec-covid.zip",
        "webis-touche2020": f"{BEIR_BASE}/webis-touche2020.zip",
        "quora": f"{BEIR_BASE}/quora.zip",
    }

    loaded_simple = {}
    for ds_name, url in ds_urls.items():
        data_path = os.path.join(args.data_dir, ds_name)
        if not os.path.isdir(data_path):
            print(f"Downloading {ds_name}...")
            data_path = util.download_and_unzip(url, args.data_dir)
        corpus, queries, qrels = GenericDataLoader(data_path).load(
            split="test",
        )
        loaded_simple[ds_name] = (corpus, queries, qrels)
        print(f"{ds_name}: {len(corpus)} docs, {len(queries)} queries")

    cqa_path = os.path.join(args.data_dir, "cqadupstack")
    if not os.path.isdir(cqa_path):
        print("Downloading cqadupstack...")
        cqa_path = util.download_and_unzip(
            f"{BEIR_BASE}/cqadupstack.zip", args.data_dir,
        )

    loaded_cqa = {}
    for forum in CQA_FORUMS:
        forum_path = os.path.join(cqa_path, forum)
        corpus, queries, qrels = GenericDataLoader(forum_path).load(
            split="test",
        )
        loaded_cqa[forum] = (corpus, queries, qrels)
        print(
            f"cqa/{forum}: {len(corpus)} docs, {len(queries)} queries"
        )

    # ── Evaluate simple datasets ──
    all_results = {}
    for ds_name, (corpus, queries, qrels) in loaded_simple.items():
        print(
            f"\n{'=' * 60}\n"
            f"{ds_name.upper()} "
            f"({len(corpus)} docs, {len(queries)} queries)"
            f"\n{'=' * 60}"
        )
        all_results[ds_name] = evaluate_dataset(
            ds_name, corpus, queries, qrels, args.cache_dir,
        )

    # ── Evaluate CQADupStack ──
    cqa_results = {}
    for forum, (corpus, queries, qrels) in loaded_cqa.items():
        print(
            f"\n{'=' * 60}\n"
            f"CQA/{forum.upper()} "
            f"({len(corpus)} docs, {len(queries)} queries)"
            f"\n{'=' * 60}"
        )
        cqa_results[forum] = evaluate_dataset(
            f"cqa_{forum}", corpus, queries, qrels, args.cache_dir,
        )

    cqa_avg = {}
    for strat in STRATEGIES:
        avg_metrics = {}
        for metric in METRICS:
            vals = [cqa_results[f][strat][metric] for f in cqa_results]
            avg_metrics[metric] = round(sum(vals) / len(vals), 6)
        cqa_avg[strat] = avg_metrics
    all_results["cqadupstack"] = cqa_avg

    print(
        f"\n{'=' * 60}\n"
        f"CQADUPSTACK AVERAGE ({len(cqa_results)} subforums)"
        f"\n{'=' * 60}"
    )
    for strat, m in cqa_avg.items():
        print(
            f"  {strat:<15} "
            f"nDCG@10={m['ndcg_cut_10']:.4f}  "
            f"MAP={m['map']:.4f}"
        )

    # ── Combined summary ──
    print(f"\n{'=' * 80}")
    print("ALL 9 DATASETS — Confluence Fusion (pytrec_eval official)")
    print(f"{'=' * 80}")

    all_ds = {**BATCH_A}
    for ds_name in list(ds_urls) + ["cqadupstack"]:
        r = all_results[ds_name]
        all_ds[ds_name] = {s: r[s]["ndcg_cut_10"] for s in STRATEGIES}

    strats = list(STRATEGIES.keys())
    header = (
        f"{'Dataset':<20}"
        + "".join(f"{s:>14}" for s in strats)
        + f"{'BEST':>14}{'vs Dense':>10}"
    )
    print(header)
    print("-" * len(header))

    wins = 0
    for ds_name, scores in all_ds.items():
        best_strat = max(scores, key=scores.get)
        best_score = scores[best_strat]
        dense = scores["dense_only"]
        if best_score > dense:
            wins += 1
        row = f"{ds_name:<20}"
        for s in strats:
            marker = " *" if s == best_strat else "  "
            row += f"{scores[s]:>12.4f}{marker}"
        row += f"{best_strat:>14}{best_score - dense:>+10.4f}"
        print(row)
    print(f"\nConfluence beats Dense: {wins}/{len(all_ds)}")

    # ── Save JSON ──
    output = {
        "experiment": "batch_b_beir_medium",
        "model": {
            "name": "E5-base",
            "hf_id": MODEL_ID,
            "params": "110M",
        },
        "evaluator": "pytrec_eval (official BEIR standard)",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "datasets": {},
        "cqa_per_forum": {},
    }
    for ds_name, (corpus, queries, _) in loaded_simple.items():
        output["datasets"][ds_name] = {
            "corpus_size": len(corpus),
            "num_queries": len(queries),
            "results": all_results[ds_name],
        }
    output["datasets"]["cqadupstack"] = {
        "num_subforums": len(loaded_cqa),
        "results": all_results["cqadupstack"],
    }
    for forum, (corpus, queries, _) in loaded_cqa.items():
        output["cqa_per_forum"][forum] = {
            "corpus_size": len(corpus),
            "num_queries": len(queries),
            "results": cqa_results[forum],
        }
    with open(args.output, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nSaved to {args.output}")


if __name__ == "__main__":
    main()
