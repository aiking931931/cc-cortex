"""
Pipeline Batch B: GPU encoding + CPU BM25 in parallel on one machine.
While GPU encodes dataset N+1, CPU evaluates dataset N.

Usage: python pipeline_batch_b.py [--cache-dir ./cache]

Output format: pytrec_eval official + TREC run file for EvalAI submission.
"""

import argparse
import json
import os
import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytrec_eval
import torch
from beir import util
from beir.datasets.data_loader import GenericDataLoader
import bm25s
from sentence_transformers import SentenceTransformer

# ── Config ──
MODEL_ID = "intfloat/e5-base-unsupervised"
PREFIX_Q = "query: "
PREFIX_D = "passage: "
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
    agreement = (
        len(b_set & d_set) / len(union) if union else 0.0
    )
    tension = 1.0 - agreement
    adaptive_k = max(
        1, int(k_low + (k_high - k_low) * tension)
    )
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
        s = (
            bw * b_n.get(did, 0) + dw * d_n.get(did, 0)
        ) / tw
        final[did] = (1 - score_w) * r + score_w * s
    return sorted(
        final.items(), key=lambda x: x[1], reverse=True,
    )


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


# ── GPU: Encode corpus ──


def encode_corpus(model, corpus, ds_name, cache_dir):
    cache_path = os.path.join(
        cache_dir, f"{ds_name}_embs.npz",
    )
    ckpt_path = os.path.join(
        cache_dir, f"{ds_name}_embs.ckpt.npz",
    )
    ids_path = os.path.join(
        cache_dir, f"{ds_name}_doc_ids.json",
    )
    doc_id_list = list(corpus.keys())

    if os.path.exists(cache_path) and os.path.exists(ids_path):
        data = np.load(cache_path)
        print(f"  [GPU] Cache hit: {data['embs'].shape}")
        return data["embs"], doc_id_list

    texts = [
        f"{PREFIX_D}"
        f"{corpus[did].get('title', '')} "
        f"{corpus[did].get('text', '')}".strip()
        for did in doc_id_list
    ]

    start_idx = 0
    all_embs = []
    if os.path.exists(ckpt_path):
        ckpt = np.load(ckpt_path)
        start_idx = int(ckpt["done"])
        all_embs = [ckpt["embs"]]
        print(
            f"  [GPU] Resuming from "
            f"{start_idx}/{len(texts)}"
        )

    batch_size = 512
    t0 = time.time()
    for i in range(start_idx, len(texts), batch_size):
        batch = texts[i: i + batch_size]
        embs = model.encode(
            batch,
            normalize_embeddings=True,
            show_progress_bar=False,
            batch_size=256,
        )
        all_embs.append(embs)
        done = min(i + batch_size, len(texts))
        if done % 10000 < batch_size or done == len(texts):
            partial = np.vstack(all_embs)
            np.savez_compressed(
                ckpt_path, embs=partial, done=done,
            )
            elapsed = time.time() - t0
            speed = (
                (done - start_idx) / elapsed
                if elapsed > 0 else 0
            )
            eta = (
                (len(texts) - done) / speed
                if speed > 0 else 0
            )
            print(
                f"  [GPU] {ds_name}: "
                f"{done}/{len(texts)} "
                f"({speed:.0f} d/s, ETA {eta:.0f}s)"
            )

    passage_embs = np.vstack(all_embs)
    np.savez_compressed(cache_path, embs=passage_embs)
    with open(ids_path, "w") as f:
        json.dump(doc_id_list, f)
    if os.path.exists(ckpt_path):
        os.remove(ckpt_path)
    total = time.time() - t0
    print(
        f"  [GPU] {ds_name} DONE: "
        f"{passage_embs.shape} in {total:.0f}s"
    )
    return passage_embs, doc_id_list


# ── CPU: BM25 + evaluate ──


def evaluate_dataset(
    ds_name, corpus, queries, qrels,
    passage_embs, doc_id_list, q_model,
):
    print(f"  [CPU] {ds_name}: Building BM25 (bm25s)...")
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

    print(
        f"  [EVAL] {ds_name}: "
        f"Batch encoding {len(queries)} queries..."
    )
    qid_list = list(queries.keys())
    qt_list = [PREFIX_Q + queries[qid] for qid in qid_list]
    t0 = time.time()
    all_q_embs = q_model.encode(
        qt_list, normalize_embeddings=True,
        batch_size=256, show_progress_bar=True,
    )
    print(
        f"  [EVAL] {ds_name}: "
        f"queries encoded in {time.time() - t0:.1f}s"
    )

    # Dense retrieval: batch matrix multiply
    print(f"  [EVAL] {ds_name}: Dense retrieval...")
    all_sims = passage_embs @ all_q_embs.T  # (docs, queries)

    print(
        f"  [EVAL] {ds_name}: "
        f"BM25 + fusion for {len(queries)} queries..."
    )
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
            eta = (len(queries) - qi - 1) / speed
            print(
                f"    [EVAL] {ds_name}: "
                f"{qi + 1}/{len(queries)} "
                f"({speed:.1f} q/s, ETA {eta:.0f}s)"
            )
    print(
        f"  [EVAL] {ds_name}: "
        f"done in {time.time() - t0:.1f}s"
    )

    ds_results = {}
    trec_runs = {}
    for strat_name, strat_fn in STRATEGIES.items():
        run_dict = {}
        for qid in queries:
            e = cached[qid]
            fused = strat_fn(e["bm25"], e["dense"])
            run_dict[qid] = {
                did: float(score)
                for did, score in fused[:100]
            }
        metrics = evaluate_official(qrels, run_dict)
        ds_results[strat_name] = metrics
        trec_runs[strat_name] = run_dict
        print(
            f"  [CPU] {ds_name} {strat_name:<15} "
            f"nDCG@10={metrics['ndcg_cut_10']:.4f}"
        )
    best = max(
        ds_results.items(),
        key=lambda x: x[1]["ndcg_cut_10"],
    )
    print(
        f"  [CPU] {ds_name} >>> BEST: "
        f"{best[0]} = {best[1]['ndcg_cut_10']:.4f}"
    )
    del cached, bm25_idx
    return ds_results, trec_runs


def write_trec_run(run_dict, output_path, run_name):
    """Write TREC run file for EvalAI submission."""
    with open(output_path, "w") as f:
        for qid in sorted(run_dict, key=lambda x: str(x)):
            docs = sorted(
                run_dict[qid].items(),
                key=lambda x: x[1],
                reverse=True,
            )
            for rank, (did, score) in enumerate(docs, 1):
                f.write(
                    f"{qid} Q0 {did} {rank} "
                    f"{score:.6f} {run_name}\n"
                )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir", default="./cache")
    parser.add_argument("--data-dir", default="./datasets")
    parser.add_argument("--output", default="batch_b_results.json")
    parser.add_argument(
        "--trec-dir", default="./trec_runs",
    )
    args = parser.parse_args()

    os.makedirs(args.cache_dir, exist_ok=True)
    os.makedirs(args.data_dir, exist_ok=True)
    os.makedirs(args.trec_dir, exist_ok=True)

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Device: {device}")
    if device == "cuda":
        print(f"GPU: {torch.cuda.get_device_name(0)}")
    import multiprocessing
    print(f"CPU cores: {multiprocessing.cpu_count()}")

    # ── Load model ──
    print(f"Loading {MODEL_ID}...")
    gpu_model = SentenceTransformer(MODEL_ID, device=device)
    cpu_model = SentenceTransformer(MODEL_ID, device="cpu")

    # ── Download all datasets first ──
    print("\n=== Downloading datasets ===")
    ds_urls = {
        "trec-covid": f"{BEIR_BASE}/trec-covid.zip",
        "webis-touche2020": (
            f"{BEIR_BASE}/webis-touche2020.zip"
        ),
        "quora": f"{BEIR_BASE}/quora.zip",
    }

    all_datasets = {}
    for ds_name, url in ds_urls.items():
        data_path = os.path.join(args.data_dir, ds_name)
        if not os.path.isdir(data_path):
            print(f"Downloading {ds_name}...")
            data_path = util.download_and_unzip(
                url, args.data_dir,
            )
        corpus, queries, qrels = GenericDataLoader(
            data_path,
        ).load(split="test")
        all_datasets[ds_name] = (corpus, queries, qrels)
        print(
            f"  {ds_name}: {len(corpus)} docs, "
            f"{len(queries)} queries"
        )

    cqa_path = os.path.join(args.data_dir, "cqadupstack")
    if not os.path.isdir(cqa_path):
        print("Downloading cqadupstack (4.98 GB)...")
        cqa_path = util.download_and_unzip(
            f"{BEIR_BASE}/cqadupstack.zip", args.data_dir,
        )
    for forum in CQA_FORUMS:
        forum_path = os.path.join(cqa_path, forum)
        corpus, queries, qrels = GenericDataLoader(
            forum_path,
        ).load(split="test")
        ds_key = f"cqa_{forum}"
        all_datasets[ds_key] = (corpus, queries, qrels)
        print(
            f"  {ds_key}: {len(corpus)} docs, "
            f"{len(queries)} queries"
        )

    # ── Pipeline: encode N+1 on GPU while eval N on CPU ──
    print("\n=== Pipeline: GPU encode + CPU eval ===")
    ds_names = list(all_datasets.keys())
    all_results = {}
    all_trec = {}

    # Encode first dataset (no overlap yet)
    first = ds_names[0]
    corpus_0 = all_datasets[first][0]
    print(f"\n--- {first.upper()} ---")
    embs_0, ids_0 = encode_corpus(
        gpu_model, corpus_0, first, args.cache_dir,
    )

    pending_embs = None
    pending_ids = None

    for i in range(len(ds_names)):
        ds = ds_names[i]
        corpus, queries, qrels = all_datasets[ds]

        if i == 0:
            embs, doc_ids = embs_0, ids_0
        else:
            embs = pending_embs
            doc_ids = pending_ids

        # Start GPU encoding next dataset in background
        gpu_future = None

        if i + 1 < len(ds_names):
            next_ds = ds_names[i + 1]
            next_corpus = all_datasets[next_ds][0]
            print(f"\n--- {next_ds.upper()} (GPU) + "
                  f"{ds.upper()} (CPU) parallel ---")

            executor = ThreadPoolExecutor(max_workers=1)
            gpu_future = executor.submit(
                encode_corpus,
                gpu_model, next_corpus,
                next_ds, args.cache_dir,
            )

        # Use GPU for query encoding when available
        q_model = cpu_model if gpu_future else gpu_model
        results, trec = evaluate_dataset(
            ds, corpus, queries, qrels,
            embs, doc_ids, q_model,
        )
        all_results[ds] = results

        # Write TREC run files
        for strat, run in trec.items():
            path = os.path.join(
                args.trec_dir,
                f"{ds}_{strat}.txt",
            )
            write_trec_run(
                run, path, f"confluence_{strat}",
            )
        all_trec[ds] = trec

        # Wait for GPU to finish next dataset
        if gpu_future is not None:
            pending_embs, pending_ids = gpu_future.result()
            executor.shutdown(wait=False)

        # Free memory
        del embs

    # ── CQADupStack average ──
    cqa_names = [f"cqa_{f}" for f in CQA_FORUMS]
    cqa_avg = {}
    for strat in STRATEGIES:
        avg = {}
        for metric in METRICS:
            vals = [
                all_results[n][strat][metric]
                for n in cqa_names
                if n in all_results
            ]
            if vals:
                avg[metric] = round(sum(vals) / len(vals), 6)
        cqa_avg[strat] = avg
    all_results["cqadupstack"] = cqa_avg

    # ── Print summary ──
    print(f"\n{'=' * 80}")
    print("ALL 9 DATASETS — Confluence Fusion (pytrec_eval)")
    print(f"{'=' * 80}")

    all_ds = {**BATCH_A}
    simple_names = list(ds_urls.keys()) + ["cqadupstack"]
    for ds_name in simple_names:
        r = all_results[ds_name]
        all_ds[ds_name] = {
            s: r[s]["ndcg_cut_10"] for s in STRATEGIES
        }

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
        row += f"{best_strat:>14}"
        row += f"{best_score - dense:>+10.4f}"
        print(row)
    print(f"\nConfluence beats Dense: {wins}/{len(all_ds)}")

    # ── Save JSON ──
    output = {
        "experiment": "batch_b_pipeline",
        "model": {
            "name": "E5-base",
            "hf_id": MODEL_ID,
            "params": "110M",
        },
        "device": device,
        "gpu": (
            torch.cuda.get_device_name(0)
            if device == "cuda" else "CPU"
        ),
        "evaluator": "pytrec_eval (official BEIR standard)",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "datasets": {},
        "cqa_per_forum": {},
    }
    for ds_name in list(ds_urls.keys()):
        corpus, queries, _ = all_datasets[ds_name]
        output["datasets"][ds_name] = {
            "corpus_size": len(corpus),
            "num_queries": len(queries),
            "results": all_results[ds_name],
        }
    output["datasets"]["cqadupstack"] = {
        "num_subforums": len(CQA_FORUMS),
        "results": all_results["cqadupstack"],
    }
    for forum in CQA_FORUMS:
        key = f"cqa_{forum}"
        if key in all_datasets and key in all_results:
            corpus, queries, _ = all_datasets[key]
            output["cqa_per_forum"][forum] = {
                "corpus_size": len(corpus),
                "num_queries": len(queries),
                "results": all_results[key],
            }

    with open(args.output, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nJSON saved to {args.output}")
    print(f"TREC runs saved to {args.trec_dir}/")


if __name__ == "__main__":
    main()
