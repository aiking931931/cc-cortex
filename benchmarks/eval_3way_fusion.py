"""
M2: Three-way fusion (BM25 + Dense + Reranker → riverbed)
Reranker as 3rd signal, not as override.
Tests on TREC-COVID with BGE-large + BGE-reranker.

Usage: python eval_3way_fusion.py --cache-dir ./cache
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


def _norm(results):
    """Normalize（正規化）scores to 0-1 range."""
    if not results:
        return {}
    vals = [s for _, s in results]
    mn, mx = min(vals), max(vals)
    rng = mx - mn if mx > mn else 1.0
    return {did: (s - mn) / rng for did, s in results}


def three_way_riverbed(bm25_res, dense_res, rerank_res,
                       bw=0.8, dw=1.0, rw=1.2):
    """Three-way riverbed fusion（三路河床融合）.
    BM25 + Dense + Reranker scores normalized and weighted.
    """
    b_n = _norm(bm25_res)
    d_n = _norm(dense_res)
    r_n = _norm(rerank_res)
    all_docs = set(b_n) | set(d_n) | set(r_n)
    tw = bw + dw + rw
    final = {
        did: (
            bw * b_n.get(did, 0)
            + dw * d_n.get(did, 0)
            + rw * r_n.get(did, 0)
        ) / tw
        for did in all_docs
    }
    return sorted(
        final.items(), key=lambda x: x[1], reverse=True,
    )


def three_way_rt(bm25_res, dense_res, rerank_res,
                 k_low=2, k_high=5, top_n=20,
                 boost_max=1.2, score_w=0.5,
                 bw=0.8, dw=1.0, rw=1.2):
    """Three-way RT fusion（三路張力融合）.
    RRF with adaptive k + riverbed score blending, 3 sources.
    """
    # Agreement between BM25 and Dense
    b_set = {did for did, _ in bm25_res[:top_n]}
    d_set = {did for did, _ in dense_res[:top_n]}
    union = b_set | d_set
    agreement = (
        len(b_set & d_set) / len(union) if union else 0.0
    )
    tension = 1.0 - agreement
    adaptive_k = max(
        1, int(k_low + (k_high - k_low) * tension),
    )
    boost = 1.0 + (boost_max - 1.0) * agreement

    # RRF from all 3 sources
    rrf_scores = defaultdict(float)
    presence = defaultdict(int)
    for rank, (did, _) in enumerate(bm25_res):
        rrf_scores[did] += bw / (adaptive_k + rank + 1)
        presence[did] += 1
    for rank, (did, _) in enumerate(dense_res):
        rrf_scores[did] += dw / (adaptive_k + rank + 1)
        presence[did] += 1
    for rank, (did, _) in enumerate(rerank_res):
        rrf_scores[did] += rw / (adaptive_k + rank + 1)
        presence[did] += 1
    for did in rrf_scores:
        if presence[did] >= 2:
            rrf_scores[did] *= boost

    # Normalize all
    b_n = _norm(bm25_res)
    d_n = _norm(dense_res)
    r_n = _norm(rerank_res)
    rv = list(rrf_scores.values())
    r_mn, r_mx = min(rv), max(rv)
    r_rng = r_mx - r_mn if r_mx > r_mn else 1.0
    tw = bw + dw + rw

    all_docs = (
        set(rrf_scores) | set(b_n) | set(d_n) | set(r_n)
    )
    final = {}
    for did in all_docs:
        rrf_v = (rrf_scores.get(did, 0) - r_mn) / r_rng
        score_v = (
            bw * b_n.get(did, 0)
            + dw * d_n.get(did, 0)
            + rw * r_n.get(did, 0)
        ) / tw
        final[did] = (
            (1 - score_w) * rrf_v + score_w * score_v
        )
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir", default="./cache")
    parser.add_argument("--data-dir", default="./datasets")
    parser.add_argument(
        "--output", default="3way_fusion_results.json",
    )
    args = parser.parse_args()

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Device: {device}")

    # Load TREC-COVID
    ds_name = "trec-covid"
    url = f"{BEIR_BASE}/{ds_name}.zip"
    data_path = os.path.join(args.data_dir, ds_name)
    if not os.path.isdir(data_path):
        data_path = util.download_and_unzip(url, args.data_dir)
    corpus, queries, qrels = GenericDataLoader(
        data_path,
    ).load(split="test")
    print(f"{len(corpus)} docs, {len(queries)} queries")
    doc_ids = list(corpus.keys())

    # Load BGE-large embeddings (from J-2 cache)
    model_id = "BAAI/bge-large-en-v1.5"
    tag = model_id.replace("/", "_").replace("-", "_")
    cache_key = f"{ds_name}_{tag}"
    cache_path = os.path.join(
        args.cache_dir, f"{cache_key}_embs.npz",
    )
    ids_path = os.path.join(
        args.cache_dir, f"{cache_key}_doc_ids.json",
    )

    if os.path.exists(cache_path):
        embs = np.load(cache_path)["embs"]
        with open(ids_path) as f:
            doc_id_list = json.load(f)
        print(f"Cache hit: {embs.shape}")
    else:
        print("No cache, encoding...")
        embed_model = SentenceTransformer(model_id, device=device)
        prefix_d = ""
        texts = [
            f"{prefix_d}"
            f"{corpus[did].get('title', '')} "
            f"{corpus[did].get('text', '')}".strip()
            for did in doc_ids
        ]
        embs = embed_model.encode(
            texts, normalize_embeddings=True,
            batch_size=256, show_progress_bar=True,
        )
        doc_id_list = doc_ids
        np.savez_compressed(cache_path, embs=embs)
        with open(ids_path, "w") as f:
            json.dump(doc_id_list, f)
        del embed_model

    # BM25
    print("Building BM25...")
    corpus_texts = [
        f"{corpus[did].get('title', '')} "
        f"{corpus[did].get('text', '')}"
        for did in doc_ids
    ]
    corpus_tokens = bm25s.tokenize(corpus_texts)
    bm25_idx = bm25s.BM25()
    bm25_idx.index(corpus_tokens)

    # Query encoding (GPU batch)
    print("Encoding queries...")
    prefix_q = "Represent this sentence for searching: "
    embed_model = SentenceTransformer(model_id, device=device)
    qid_list = list(queries.keys())
    qt_list = [prefix_q + queries[qid] for qid in qid_list]
    q_embs = embed_model.encode(
        qt_list, normalize_embeddings=True, batch_size=256,
    )
    del embed_model
    torch.cuda.empty_cache()

    # Dense retrieval (batch)
    print("Dense retrieval...")
    all_sims = embs @ q_embs.T

    # BM25 retrieval
    print("BM25 retrieval...")
    all_bm25 = {}
    for qi, qid in enumerate(qid_list):
        qt = bm25s.tokenize([queries[qid]])
        res, sc = bm25_idx.retrieve(  # noqa: F821
            qt, corpus=doc_ids, k=100,
        )
        all_bm25[qid] = [
            (str(res[0, i]), float(sc[0, i]))
            for i in range(len(res[0]))
            if sc[0, i] > 0
        ]

    # Dense results
    all_dense = {}
    for qi, qid in enumerate(qid_list):
        sims = all_sims[:, qi]
        idx = np.argsort(sims)[::-1][:100]
        all_dense[qid] = [
            (doc_id_list[i], float(sims[i])) for i in idx
        ]

    # Reranker scores (top-100 from dense)
    print("Reranker scoring (top-100 per query)...")
    reranker = CrossEncoder(
        "BAAI/bge-reranker-v2-m3", device=device,
    )
    all_rerank = {}
    for qi, qid in enumerate(qid_list):
        top_docs = all_dense[qid][:100]
        pairs = [
            (
                queries[qid],
                f"{corpus[did].get('title', '')} "
                f"{corpus[did].get('text', '')}",
            )
            for did, _ in top_docs
        ]
        ce_scores = reranker.predict(
            pairs, batch_size=256, show_progress_bar=False,
        )
        all_rerank[qid] = [
            (top_docs[i][0], float(ce_scores[i]))
            for i in range(len(top_docs))
        ]
        if (qi + 1) % 10 == 0:
            print(f"  Reranked {qi + 1}/{len(qid_list)}")
    del reranker
    torch.cuda.empty_cache()

    # ── Test all fusion methods ──
    print(f"\n{'=' * 60}")
    print("THREE-WAY FUSION RESULTS")
    print(f"{'=' * 60}")

    all_results = {}

    # Baselines（基線）
    baselines = {
        "dense_only": {
            qid: {did: s for did, s in all_dense[qid][:100]}
            for qid in qid_list
        },
        "reranker_only": {
            qid: {did: s for did, s in all_rerank[qid][:100]}
            for qid in qid_list
        },
    }

    for name, run in baselines.items():
        m = evaluate_official(qrels, run)
        all_results[name] = m
        print(f"  {name:<30} nDCG@10={m['ndcg_cut_10']:.4f}")

    # 2-way（兩路融合，對照組）
    two_way_configs = [
        ("2way_riverbed_bd", 0.8, 1.0),
    ]
    for name, bw, dw in two_way_configs:
        run = {}
        for qid in qid_list:
            fused = three_way_riverbed(
                all_bm25[qid], all_dense[qid], [],
                bw=bw, dw=dw, rw=0,
            )
            run[qid] = {
                did: s for did, s in fused[:100]
            }
        m = evaluate_official(qrels, run)
        all_results[name] = m
        print(f"  {name:<30} nDCG@10={m['ndcg_cut_10']:.4f}")

    # 3-way riverbed sweep（三路河床掃參）
    print("\n  --- Three-way riverbed sweep ---")
    rw_values = [0.4, 0.6, 0.8, 1.0, 1.2, 1.5, 2.0]
    best_3way_score = 0

    for rw in rw_values:
        run = {}
        for qid in qid_list:
            fused = three_way_riverbed(
                all_bm25[qid], all_dense[qid],
                all_rerank[qid],
                bw=0.8, dw=1.0, rw=rw,
            )
            run[qid] = {
                did: s for did, s in fused[:100]
            }
        m = evaluate_official(qrels, run)
        name = f"3way_riverbed_rw{rw}"
        all_results[name] = m
        score = m["ndcg_cut_10"]
        marker = ""
        if score > best_3way_score:
            best_3way_score = score
            pass  # best tracked by score
            marker = " <<<BEST"
        print(
            f"  {name:<30} "
            f"nDCG@10={score:.4f}{marker}"
        )

    # 3-way RT sweep（三路張力掃參）
    print("\n  --- Three-way RT sweep ---")
    best_3rt_score = 0

    for rw in rw_values:
        run = {}
        for qid in qid_list:
            fused = three_way_rt(
                all_bm25[qid], all_dense[qid],
                all_rerank[qid],
                bw=0.8, dw=1.0, rw=rw,
            )
            run[qid] = {
                did: s for did, s in fused[:100]
            }
        m = evaluate_official(qrels, run)
        name = f"3way_rt_rw{rw}"
        all_results[name] = m
        score = m["ndcg_cut_10"]
        marker = ""
        if score > best_3rt_score:
            best_3rt_score = score
            pass  # best tracked by score
            marker = " <<<BEST"
        print(
            f"  {name:<30} "
            f"nDCG@10={score:.4f}{marker}"
        )

    # Summary（總結）
    print(f"\n{'=' * 60}")
    print("SUMMARY")
    print(f"{'=' * 60}")
    sorted_r = sorted(
        all_results.items(),
        key=lambda x: x[1]["ndcg_cut_10"],
        reverse=True,
    )
    for name, m in sorted_r[:10]:
        print(f"  {m['ndcg_cut_10']:.4f}  {name}")

    with open(args.output, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\nSaved to {args.output}")


if __name__ == "__main__":
    main()
