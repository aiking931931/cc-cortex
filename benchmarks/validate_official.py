"""Validate our scores against official BEIR evaluator (pytrec_eval).

Critical: If our custom ndcg_at_k differs from pytrec_eval,
our #1 claim is invalid. This script checks the gap.
"""
from __future__ import annotations

import json
import math
import os
import sys
from collections import defaultdict

import numpy as np
import pytrec_eval

sys.path.insert(0, os.path.dirname(__file__))
os.environ["BEIR_DEVICE"] = "cpu"


def ndcg_at_k_custom(ranked_ids, relevant, k=10):
    """Our custom implementation."""
    dcg = sum(
        relevant.get(doc_id, 0) / math.log2(i + 2)
        for i, doc_id in enumerate(ranked_ids[:k])
    )
    ideal_rels = sorted(relevant.values(), reverse=True)[:k]
    idcg = sum(
        r / math.log2(i + 2) for i, r in enumerate(ideal_rels)
    )
    return dcg / idcg if idcg > 0 else 0.0


def riverbed_tension(
    b, d, k_low=3, k_high=10, top_n=20,
    boost_max=1.2, score_w=0.5, bw=0.8, dw=1.4,
):
    """RT fusion (E5PT params, world #1 config)."""
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
    tw = bw + dw

    all_docs = set(rrf_scores) | set(b_n) | set(d_n)
    final = {}
    for did in all_docs:
        r = (rrf_scores.get(did, 0) - r_mn) / r_rng
        s = (bw * b_n.get(did, 0) + dw * d_n.get(did, 0)) / tw
        final[did] = (1 - score_w) * r + score_w * s
    return sorted(final.items(), key=lambda x: x[1], reverse=True)


def simple_rrf(b, d, k=5, bw=1.0, dw=1.2):
    """Simple RRF baseline."""
    scores = defaultdict(float)
    for rank, (did, _) in enumerate(b):
        scores[did] += bw / (k + rank + 1)
    for rank, (did, _) in enumerate(d):
        scores[did] += dw / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def evaluate_official(qrels_dict, run_dict, k=10):
    """Evaluate with pytrec_eval (official BEIR method)."""
    # pytrec_eval expects: {qid: {did: int_relevance}}
    qrels_int = {}
    for qid, rels in qrels_dict.items():
        qrels_int[qid] = {
            did: int(rel) for did, rel in rels.items()
        }

    evaluator = pytrec_eval.RelevanceEvaluator(
        qrels_int, {f"ndcg_cut_{k}"},
    )
    scores = evaluator.evaluate(run_dict)
    ndcgs = [
        scores[qid][f"ndcg_cut_{k}"]
        for qid in scores
    ]
    return sum(ndcgs) / len(ndcgs) if ndcgs else 0.0


def main():
    from beir.datasets.data_loader import GenericDataLoader
    from sentence_transformers import SentenceTransformer

    from beir_runner import build_bm25_with_ids, search_bm25_by_ids

    base = os.path.join(os.path.dirname(__file__), "datasets")
    corpus, queries, qrels = GenericDataLoader(
        os.path.join(base, "scifact"),
    ).load(split="test")
    bm25, doc_ids = build_bm25_with_ids(corpus)

    model = SentenceTransformer(
        "intfloat/e5-base-unsupervised", device="cpu",
    )
    cache = np.load(
        os.path.join(base, ".cache_e5pt_base_embs.npz"),
    )
    passage_embs = cache["embs"]
    doc_id_list = list(corpus.keys())

    def e5pt_search(qt, top_k=100):
        q = model.encode(
            ["query: " + qt], normalize_embeddings=True,
        )
        sims = (passage_embs @ q.T).flatten()
        idx = np.argsort(sims)[::-1][:top_k]
        return [(doc_id_list[i], float(sims[i])) for i in idx]

    print("Caching queries...", flush=True)
    cached = {}
    for qid, qt in queries.items():
        cached[qid] = {
            "bm25": search_bm25_by_ids(bm25, doc_ids, qt, top_k=100),
            "dense": e5pt_search(qt, top_k=100),
        }
    print(f"Cached {len(cached)} queries\n", flush=True)

    # ── Test configs ──
    configs = {
        "Dense only": lambda b, d: d,
        "Simple RRF": lambda b, d: simple_rrf(b, d),
        "RT (E5PT)": lambda b, d: riverbed_tension(b, d),
    }

    print(f"{'Config':<20} {'Custom':>10} {'Official':>10} {'Gap':>10}")
    print("-" * 55)

    for name, fusion_fn in configs.items():
        # Custom evaluation
        custom_ndcgs = []
        # Official evaluation: build run dict
        run_dict = {}

        for qid in queries:
            rel = {
                d: r for d, r in qrels.get(qid, {}).items()
                if r > 0
            }
            e = cached[qid]
            fused = fusion_fn(e["bm25"], e["dense"])
            ranked = [d for d, _ in fused[:100]]
            custom_ndcgs.append(
                ndcg_at_k_custom(ranked, rel, k=10),
            )

            # pytrec_eval format: {qid: {did: float_score}}
            run_dict[qid] = {
                did: float(score)
                for did, score in fused[:100]
            }

        custom_avg = sum(custom_ndcgs) / len(custom_ndcgs)
        official_avg = evaluate_official(qrels, run_dict, k=10)
        gap = custom_avg - official_avg

        print(
            f"{name:<20} {custom_avg:>10.6f} "
            f"{official_avg:>10.6f} {gap:>+10.6f}",
        )

    # ── Save official scores ──
    print("\n\nGenerating submission-ready results...")

    # RT (E5PT) official
    run_dict = {}
    for qid in queries:
        e = cached[qid]
        fused = riverbed_tension(e["bm25"], e["dense"])
        run_dict[qid] = {
            did: float(score) for did, score in fused[:100]
        }

    qrels_int = {
        qid: {did: int(rel) for did, rel in rels.items()}
        for qid, rels in qrels.items()
    }

    # Full official evaluation
    evaluator = pytrec_eval.RelevanceEvaluator(
        qrels_int,
        {"ndcg_cut_10", "recall_100", "map", "recip_rank"},
    )
    scores = evaluator.evaluate(run_dict)

    metrics = {}
    for metric in ["ndcg_cut_10", "recall_100", "map", "recip_rank"]:
        vals = [scores[qid].get(metric, 0) for qid in scores]
        metrics[metric] = round(sum(vals) / len(vals), 6)

    print(f"\n{'='*50}")
    print("OFFICIAL SCORES (pytrec_eval)")
    print(f"{'='*50}")
    for k, v in metrics.items():
        print(f"  {k}: {v}")

    out = {
        "system": "Confluence RT (E5-base + BM25)",
        "method": "Hybrid retrieval: adaptive tension-based fusion",
        "dataset": "scifact",
        "split": "test",
        "evaluator": "pytrec_eval 0.5.10",
        "model": "intfloat/e5-base-unsupervised (110M)",
        "params": {
            "k_low": 3, "k_high": 10, "top_n": 20,
            "boost_max": 1.2, "score_w": 0.5,
            "bw": 0.8, "dw": 1.4,
        },
        "metrics": metrics,
    }
    out_path = os.path.join(
        os.path.dirname(__file__), "results",
        "official_scores_scifact.json",
    )
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    main()
