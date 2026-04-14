"""
J-4: Confluence Fusion + Reranker = absolute #1
Our fusion top-100 → cross-encoder reranker → final ranking.
Tests whether better first-stage candidates + reranker beats
traditional BM25 + reranker.

Usage: python eval_reranker_boost.py --cache-dir ./cache
"""

import argparse
import json
import os
from collections import defaultdict

import bm25s
import numpy as np
import pytrec_eval
import torch
from beir import util
from beir.datasets.data_loader import GenericDataLoader
from sentence_transformers import CrossEncoder, SentenceTransformer

BEIR_BASE = (
    "https://public.ukp.informatik.tu-darmstadt.de"
    "/thakur/BEIR/datasets"
)
METRICS = {"ndcg_cut_10", "recall_100", "map"}

# Universal params
UNIVERSAL = {
    "k_low": 2, "k_high": 5, "top_n": 20,
    "boost_max": 1.2, "score_w": 0.5,
    "bw": 0.8, "dw": 1.0,
}

# Models to test
EMBED_MODELS = {
    "bge-large": {
        "id": "BAAI/bge-large-en-v1.5",
        "prefix_q": "Represent this sentence for searching: ",
        "prefix_d": "",
    },
    "gte-large": {
        "id": "thenlper/gte-large",
        "prefix_q": "",
        "prefix_d": "",
    },
}

RERANKERS = [
    "BAAI/bge-reranker-v2-m3",
    "cross-encoder/ms-marco-MiniLM-L-12-v2",
]

# Datasets to test
DATASETS = ["trec-covid"]


def _norm(results):
    if not results:
        return {}
    vals = [s for _, s in results]
    mn, mx = min(vals), max(vals)
    rng = mx - mn if mx > mn else 1.0
    return {did: (s - mn) / rng for did, s in results}


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


def rerank_with_cross_encoder(
    reranker, queries, corpus, run_dict, top_k=100,
):
    """Rerank top-K results using cross-encoder."""
    reranked = {}
    qids = list(run_dict.keys())
    total = len(qids)

    for qi, qid in enumerate(qids):
        query_text = queries[qid]
        doc_scores = sorted(
            run_dict[qid].items(),
            key=lambda x: x[1], reverse=True,
        )[:top_k]

        pairs = []
        doc_ids_ordered = []
        for did, _ in doc_scores:
            doc_text = (
                f"{corpus[did].get('title', '')} "
                f"{corpus[did].get('text', '')}"
            )
            pairs.append((query_text, doc_text))
            doc_ids_ordered.append(did)

        if not pairs:
            reranked[qid] = {}
            continue

        ce_scores = reranker.predict(
            pairs, batch_size=256, show_progress_bar=False,
        )
        reranked[qid] = {
            doc_ids_ordered[i]: float(ce_scores[i])
            for i in range(len(doc_ids_ordered))
        }

        if (qi + 1) % 10 == 0 or qi + 1 == total:
            print(f"    Reranked {qi + 1}/{total}")

    return reranked


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir", default="./cache")
    parser.add_argument("--data-dir", default="./datasets")
    parser.add_argument(
        "--output", default="reranker_boost_results.json",
    )
    args = parser.parse_args()

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Device: {device}")

    # Load datasets
    all_datasets = {}
    for ds_name in DATASETS:
        url = f"{BEIR_BASE}/{ds_name}.zip"
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

    for ds_name in DATASETS:
        corpus, queries, qrels = all_datasets[ds_name]
        doc_ids = list(corpus.keys())

        # BM25 (shared across all models)
        print(f"\n{'=' * 60}")
        print(f"{ds_name.upper()} — Building BM25...")
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
            res, sc = bm25_idx.retrieve(  # noqa: F821
                qt, corpus=doc_ids, k=top_k,
            )
            return [
                (str(res[0, i]), float(sc[0, i]))
                for i in range(len(res[0]))
                if sc[0, i] > 0
            ]

        # BM25-only baseline (for reranker comparison)
        print("  BM25 baseline for reranker...")
        bm25_run = {}
        for qid, qt in queries.items():
            bm25_res = search_bm25(qt, top_k=100)
            bm25_run[qid] = {
                did: float(score)
                for did, score in bm25_res[:100]
            }

        for embed_name, embed_cfg in EMBED_MODELS.items():
            model_id = embed_cfg["id"]
            prefix_q = embed_cfg["prefix_q"]
            prefix_d = embed_cfg["prefix_d"]
            tag = model_id.replace("/", "_").replace("-", "_")
            cache_key = f"{ds_name}_{tag}"

            print(f"\n--- {embed_name} ---")

            # Encode corpus
            cache_path = os.path.join(
                args.cache_dir, f"{cache_key}_embs.npz",
            )
            ids_path = os.path.join(
                args.cache_dir, f"{cache_key}_doc_ids.json",
            )
            embed_model = SentenceTransformer(
                model_id, device=device,
            )

            if os.path.exists(cache_path):
                embs = np.load(cache_path)["embs"]
                with open(ids_path) as f:
                    doc_id_list = json.load(f)
                print(f"  Cache hit: {embs.shape}")
            else:
                texts = [
                    f"{prefix_d}"
                    f"{corpus[did].get('title', '')} "
                    f"{corpus[did].get('text', '')}".strip()
                    for did in doc_ids
                ]
                print(f"  Encoding {len(texts)} docs...")
                embs = embed_model.encode(
                    texts, normalize_embeddings=True,
                    batch_size=256, show_progress_bar=True,
                )
                doc_id_list = doc_ids
                np.savez_compressed(cache_path, embs=embs)
                with open(ids_path, "w") as f:
                    json.dump(doc_id_list, f)

            # Encode queries
            qid_list = list(queries.keys())
            qt_list = [
                prefix_q + queries[qid] for qid in qid_list
            ]
            q_embs = embed_model.encode(
                qt_list, normalize_embeddings=True,
                batch_size=256,
            )
            del embed_model
            torch.cuda.empty_cache()

            # Dense retrieval
            all_sims = embs @ q_embs.T

            # Build fusion results
            fusion_methods = {
                "dense_only": lambda b, d: d,
                "riverbed": lambda b, d: riverbed(b, d),
                "rt_univ": lambda b, d: rt_universal(b, d),
            }

            fusion_runs = {}
            for method_name, method_fn in fusion_methods.items():
                run_dict = {}
                for qi, qid in enumerate(qid_list):
                    bm25_res = search_bm25(
                        queries[qid], top_k=100,
                    )
                    sims = all_sims[:, qi]
                    idx = np.argsort(sims)[::-1][:100]
                    dense_res = [
                        (doc_id_list[i], float(sims[i]))
                        for i in idx
                    ]
                    fused = method_fn(bm25_res, dense_res)
                    run_dict[qid] = {
                        did: float(score)
                        for did, score in fused[:100]
                    }
                fusion_runs[method_name] = run_dict

                # Evaluate without reranker
                metrics = evaluate_official(qrels, run_dict)
                key = f"{ds_name}__{embed_name}__{method_name}"
                all_results[key] = {
                    "model": model_id,
                    "method": method_name,
                    "reranker": None,
                    "ndcg10": metrics["ndcg_cut_10"],
                    "metrics": metrics,
                }
                print(
                    f"  {method_name:<15} "
                    f"nDCG@10={metrics['ndcg_cut_10']:.4f}"
                )

            # Now apply rerankers
            for reranker_id in RERANKERS:
                print(f"\n  Reranker: {reranker_id}")
                reranker = CrossEncoder(
                    reranker_id, device=device,
                )

                # Rerank BM25 baseline
                print("    Reranking BM25 baseline...")
                bm25_reranked = rerank_with_cross_encoder(
                    reranker, queries, corpus, bm25_run,
                )
                bm25_rr_metrics = evaluate_official(
                    qrels, bm25_reranked,
                )
                rr_tag = (
                    reranker_id.replace("/", "_")
                    .replace("-", "_")
                )
                key = f"{ds_name}__bm25__{rr_tag}"
                all_results[key] = {
                    "model": "BM25",
                    "method": "bm25_only",
                    "reranker": reranker_id,
                    "ndcg10": bm25_rr_metrics["ndcg_cut_10"],
                    "metrics": bm25_rr_metrics,
                }
                print(
                    f"    BM25→rerank: "
                    f"nDCG@10="
                    f"{bm25_rr_metrics['ndcg_cut_10']:.4f}"
                )

                # Rerank each fusion method
                for method_name, run_dict in fusion_runs.items():
                    print(
                        f"    Reranking "
                        f"{embed_name}+{method_name}..."
                    )
                    reranked = rerank_with_cross_encoder(
                        reranker, queries, corpus, run_dict,
                    )
                    metrics = evaluate_official(qrels, reranked)
                    key = (
                        f"{ds_name}__{embed_name}__"
                        f"{method_name}__{rr_tag}"
                    )
                    all_results[key] = {
                        "model": model_id,
                        "method": method_name,
                        "reranker": reranker_id,
                        "ndcg10": metrics["ndcg_cut_10"],
                        "metrics": metrics,
                    }
                    print(
                        f"    {method_name}→rerank: "
                        f"nDCG@10={metrics['ndcg_cut_10']:.4f}"
                    )

                del reranker
                torch.cuda.empty_cache()

        del bm25_idx

    # Summary
    print(f"\n{'=' * 70}")
    print("RERANKER BOOST — Full Comparison")
    print(f"{'=' * 70}")
    sorted_results = sorted(
        all_results.items(),
        key=lambda x: x[1]["ndcg10"],
        reverse=True,
    )
    for key, data in sorted_results:
        rr = data["reranker"] or "none"
        rr_short = rr.split("/")[-1] if "/" in rr else rr
        print(
            f"  {data['ndcg10']:.4f}  "
            f"{data.get('model', '?'):<35} "
            f"{data['method']:<15} "
            f"rr={rr_short}"
        )

    with open(args.output, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\nSaved to {args.output}")


if __name__ == "__main__":
    main()
