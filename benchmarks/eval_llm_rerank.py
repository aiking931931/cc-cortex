"""
M5+: LLM Listwise Reranker (Claude) + Confluence Fusion
Replicate RankGPT method but with our fusion as first-stage.

Pipeline:
1. Our fusion (BM25 + Dense) → top-30 candidates
2. Claude listwise rerank → final ranking

Tests both:
- BM25 → Claude rerank (baseline, like RankGPT)
- Our fusion → Claude rerank (should beat baseline)

Usage: python eval_llm_rerank.py --cache-dir ./cache [--full]
"""

import argparse
import json
import os

import anthropic
import bm25s
import numpy as np
import pytrec_eval
import torch
from beir import util
from beir.datasets.data_loader import GenericDataLoader
from sentence_transformers import SentenceTransformer

BEIR_BASE = (
    "https://public.ukp.informatik.tu-darmstadt.de"
    "/thakur/BEIR/datasets"
)
METRICS = {"ndcg_cut_10", "recall_100", "map"}
ANTHROPIC_KEY = __import__("os").environ.get("ANTHROPIC_API_KEY", "")


def listwise_rerank_claude(query, docs, client, top_k=10):
    """Listwise reranking using Claude.
    （使用 Claude 進行列表式重排序）
    Sliding window approach like RankGPT.
    """
    # Build document list
    doc_texts = []
    for i, (did, text) in enumerate(docs):
        truncated = text[:500]
        doc_texts.append(f"[{i + 1}] {truncated}")

    docs_str = "\n".join(doc_texts)
    n = len(docs)

    prompt = (
        f"I will provide a query and {n} passages. "
        f"Rank the passages by relevance to the query. "
        f"Return ONLY a JSON array of passage numbers "
        f"(1-indexed) in order from most to least relevant. "
        f"Example: [3, 1, 5, 2, 4]\n\n"
        f"Query: {query}\n\n"
        f"Passages:\n{docs_str}\n\n"
        f"Ranking (JSON array, most relevant first):"
    )

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        # Parse JSON array
        ranking = json.loads(text)
        # Convert to doc_id order
        reranked = []
        for rank_idx, pos in enumerate(ranking):
            if 1 <= pos <= n:
                did = docs[pos - 1][0]
                reranked.append(
                    (did, float(n - rank_idx))
                )
        return reranked
    except Exception as e:
        print(f"    Claude error: {e}")
        # Fallback: return original order
        return [(did, float(n - i)) for i, (did, _) in enumerate(docs)]


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
        "--output", default="llm_rerank_results.json",
    )
    parser.add_argument(
        "--full", action="store_true",
        help="Run all 50 queries (default: 5 pilot)",
    )
    parser.add_argument(
        "--top-k", type=int, default=30,
        help="Number of candidates for reranking",
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

    # Load BGE-large embeddings
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
        print("Encoding corpus...")
        embed_model = SentenceTransformer(model_id, device=device)
        texts = [
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

    # Query encoding
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

    # Retrieval
    print("Retrieving...")
    all_sims = embs @ q_embs.T

    all_bm25 = {}
    all_dense = {}
    all_fusion = {}
    for qi, qid in enumerate(qid_list):
        # BM25
        qt = bm25s.tokenize([queries[qid]])
        res, sc = bm25_idx.retrieve(  # noqa: F821
            qt, corpus=doc_ids, k=100,
        )
        bm25_res = [
            (str(res[0, i]), float(sc[0, i]))
            for i in range(len(res[0]))
            if sc[0, i] > 0
        ]
        all_bm25[qid] = bm25_res

        # Dense
        sims = all_sims[:, qi]
        idx = np.argsort(sims)[::-1][:100]
        dense_res = [
            (doc_id_list[i], float(sims[i])) for i in idx
        ]
        all_dense[qid] = dense_res

        # Fusion
        fused = riverbed(bm25_res, dense_res)
        all_fusion[qid] = fused

    # Baselines without LLM rerank
    print("\n--- Baselines (no LLM rerank) ---")
    for name, source in [
        ("bm25_only", all_bm25),
        ("dense_only", all_dense),
        ("fusion_riverbed", all_fusion),
    ]:
        run = {
            qid: {did: s for did, s in source[qid][:100]}
            for qid in qid_list
        }
        m = evaluate_official(qrels, run)
        print(f"  {name:<25} nDCG@10={m['ndcg_cut_10']:.4f}")

    # LLM Reranking
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    n_queries = len(qid_list) if args.full else 5
    test_qids = qid_list[:n_queries]
    top_k = args.top_k

    print(
        f"\n--- LLM Reranking ({n_queries} queries, "
        f"top-{top_k}) ---"
    )

    # Test 3 first-stage methods
    methods = {
        "bm25→claude": all_bm25,
        "dense→claude": all_dense,
        "fusion→claude": all_fusion,
    }

    all_results = {}
    for method_name, source in methods.items():
        print(f"\n  {method_name}:")
        run = {}
        for qi, qid in enumerate(test_qids):
            # Get top-K candidates with full text
            candidates = source[qid][:top_k]
            doc_with_text = [
                (
                    did,
                    f"{corpus[did].get('title', '')} "
                    f"{corpus[did].get('text', '')}",
                )
                for did, _ in candidates
            ]

            reranked = listwise_rerank_claude(
                queries[qid], doc_with_text, client,
            )
            run[qid] = {
                did: s for did, s in reranked[:100]
            }
            print(f"    {qi + 1}/{n_queries} done")

        # Evaluate only on test queries
        test_qrels = {
            qid: qrels[qid]
            for qid in test_qids if qid in qrels
        }
        m = evaluate_official(test_qrels, run)
        all_results[method_name] = m
        print(
            f"  {method_name:<25} "
            f"nDCG@10={m['ndcg_cut_10']:.4f}"
        )

    # Summary
    print(f"\n{'=' * 50}")
    print(
        f"LLM RERANK RESULTS "
        f"({'FULL' if args.full else 'PILOT 5q'})"
    )
    print(f"{'=' * 50}")
    for name, m in sorted(
        all_results.items(),
        key=lambda x: x[1]["ndcg_cut_10"],
        reverse=True,
    ):
        print(
            f"  {m['ndcg_cut_10']:.4f}  {name}"
        )

    with open(args.output, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\nSaved to {args.output}")


if __name__ == "__main__":
    main()
