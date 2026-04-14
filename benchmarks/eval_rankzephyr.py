"""
M5-OS: RankZephyr 7B (open-source listwise reranker) + fusion
Goal: Beat SOTA on fully open-source pipeline.

Pipeline:
1. BM25 + Dense → riverbed fusion → top-30 candidates
2. RankZephyr 7B listwise rerank → final ranking

Tests on TREC-COVID first, then all datasets if successful.

Usage: python eval_rankzephyr.py --cache-dir ./cache
Requires: A100 80GB (RankZephyr 7B needs ~14GB VRAM)

NOTE: Do NOT run yet. Script ready, waiting for user approval.
（注意：先不要跑。腳本已就緒，等用戶批准。）
"""

import argparse
import json
import os
import time

import bm25s
import numpy as np
import pytrec_eval
import torch
from beir import util
from beir.datasets.data_loader import GenericDataLoader
from sentence_transformers import SentenceTransformer
from transformers import AutoModelForCausalLM, AutoTokenizer

BEIR_BASE = (
    "https://public.ukp.informatik.tu-darmstadt.de"
    "/thakur/BEIR/datasets"
)
METRICS = {"ndcg_cut_10", "recall_100", "map"}

# RankZephyr: open-source listwise reranker
# （RankZephyr：開源列表式重排序模型）
# Distilled from GPT-3.5/4 ranking behavior
# （從 GPT-3.5/4 排序行為蒸餾而來）
RANKZEPHYR_ID = "castorini/rank_zephyr_7b_v1_full"

# Embedding models to test
EMBED_CONFIGS = {
    "bge-large": {
        "id": "BAAI/bge-large-en-v1.5",
        "prefix_q": "Represent this sentence for searching: ",
        "prefix_d": "",
    },
}

# Datasets (start with TREC-COVID, add more later)
# （先從 TREC-COVID 開始，成功後再加其他資料集）
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


def rankzephyr_rerank(query, docs, model, tokenizer,
                      window_size=20, step=10):
    """Listwise reranking with RankZephyr (sliding window).
    （使用 RankZephyr 進行滑動窗口列表式重排序）
    Based on RankGPT/RankZephyr paper methodology.
    """
    # Build initial order (doc_id, text, score)
    items = list(docs)  # [(did, text), ...]
    n = len(items)

    # Sliding window reranking
    # （滑動窗口重排序）
    # Process from end to start (bubble up good docs)
    # （從後往前處理，好文件會浮上來）
    end = n
    while end > 0:
        start = max(0, end - window_size)
        window = items[start:end]

        # Build prompt
        doc_list = "\n".join(
            f"[{i + 1}] {text[:300]}"
            for i, (_, text) in enumerate(window)
        )
        prompt = (
            f"I will provide a query and {len(window)} passages. "
            f"Rank the passages by relevance to the query. "
            f"Output ONLY the ranking as a list of numbers "
            f"from most to least relevant.\n\n"
            f"Query: {query}\n\n"
            f"Passages:\n{doc_list}\n\n"
            f"Ranking:"
        )

        inputs = tokenizer(
            prompt, return_tensors="pt", truncation=True,
            max_length=4096,
        ).to(model.device)

        with torch.no_grad():
            outputs = model.generate(
                **inputs, max_new_tokens=100,
                do_sample=False, temperature=0,
            )

        response = tokenizer.decode(
            outputs[0][inputs["input_ids"].shape[1]:],
            skip_special_tokens=True,
        ).strip()

        # Parse ranking from response
        # （從回應中解析排序）
        try:
            # Try to extract numbers
            import re
            numbers = [
                int(x) for x in re.findall(r"\d+", response)
            ]
            # Validate and reorder window
            valid = [
                x - 1 for x in numbers
                if 1 <= x <= len(window)
            ]
            # Remove duplicates while preserving order
            seen = set()
            unique = []
            for x in valid:
                if x not in seen:
                    seen.add(x)
                    unique.append(x)
            # Fill missing indices
            for i in range(len(window)):
                if i not in seen:
                    unique.append(i)
            # Reorder window
            reordered = [window[i] for i in unique]
            items[start:end] = reordered
        except Exception:
            pass  # Keep original order on failure

        end -= step

    # Assign scores based on final position
    # （根據最終位置分配分數）
    return [
        (did, float(n - i)) for i, (did, _) in enumerate(items)
    ]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir", default="./cache")
    parser.add_argument("--data-dir", default="./datasets")
    parser.add_argument(
        "--output", default="rankzephyr_results.json",
    )
    args = parser.parse_args()

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Device: {device}")
    if device == "cuda":
        free_mem = torch.cuda.get_device_properties(0).total_memory
        print(f"GPU RAM: {free_mem / 1e9:.1f} GB")

    # Load RankZephyr
    # （載入 RankZephyr 模型）
    print(f"Loading {RANKZEPHYR_ID}...")
    t0 = time.time()
    tokenizer = AutoTokenizer.from_pretrained(RANKZEPHYR_ID)
    model_rz = AutoModelForCausalLM.from_pretrained(
        RANKZEPHYR_ID,
        torch_dtype=torch.float16,
        device_map="auto",
    )
    print(f"Loaded in {time.time() - t0:.0f}s")

    for ds_name in DATASETS:
        print(f"\n{'=' * 60}\n{ds_name.upper()}\n{'=' * 60}")

        # Load dataset
        url = f"{BEIR_BASE}/{ds_name}.zip"
        data_path = os.path.join(args.data_dir, ds_name)
        if not os.path.isdir(data_path):
            data_path = util.download_and_unzip(
                url, args.data_dir,
            )
        corpus, queries, qrels = GenericDataLoader(
            data_path,
        ).load(split="test")
        doc_ids = list(corpus.keys())

        for embed_name, cfg in EMBED_CONFIGS.items():
            model_id = cfg["id"]
            tag = model_id.replace("/", "_").replace("-", "_")
            cache_key = f"{ds_name}_{tag}"

            # Load embeddings
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
                print(f"  Cache hit: {embs.shape}")
            else:
                print(f"  Encoding {len(corpus)} docs...")
                embed_model = SentenceTransformer(
                    model_id, device=device,
                )
                texts = [
                    f"{cfg['prefix_d']}"
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
                torch.cuda.empty_cache()

            # BM25
            print("  BM25...")
            corpus_texts = [
                f"{corpus[did].get('title', '')} "
                f"{corpus[did].get('text', '')}"
                for did in doc_ids
            ]
            corpus_tokens = bm25s.tokenize(corpus_texts)
            bm25_idx = bm25s.BM25()
            bm25_idx.index(corpus_tokens)

            # Query encoding
            print("  Encoding queries...")
            embed_model = SentenceTransformer(
                model_id, device=device,
            )
            qid_list = list(queries.keys())
            qt_list = [
                cfg["prefix_q"] + queries[qid]
                for qid in qid_list
            ]
            q_embs = embed_model.encode(
                qt_list, normalize_embeddings=True,
                batch_size=256,
            )
            del embed_model
            torch.cuda.empty_cache()

            # Retrieval (GPU for large matrices)
            print("  Retrieving...")
            if (
                torch.cuda.is_available()
                and embs.shape[0] * q_embs.shape[0] > 1e8
            ):
                p_t = torch.from_numpy(embs).cuda()
                q_t = torch.from_numpy(q_embs).cuda()
                all_sims = (p_t @ q_t.T).cpu().numpy()
                del p_t, q_t
                torch.cuda.empty_cache()
            else:
                all_sims = embs @ q_embs.T

            all_bm25 = {}
            all_dense = {}
            all_fusion = {}
            for qi, qid in enumerate(qid_list):
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

                sims = all_sims[:, qi]
                idx = np.argsort(sims)[::-1][:100]
                dense_res = [
                    (doc_id_list[i], float(sims[i]))
                    for i in idx
                ]
                all_dense[qid] = dense_res
                all_fusion[qid] = riverbed(bm25_res, dense_res)

            # RankZephyr reranking
            # （RankZephyr 重排序）
            methods = {
                "bm25→rankzephyr": all_bm25,
                "dense→rankzephyr": all_dense,
                "fusion→rankzephyr": all_fusion,
            }

            all_results = {}

            # Baselines
            for name, source in [
                ("dense_only", all_dense),
                ("fusion_riverbed", all_fusion),
            ]:
                run = {
                    qid: {
                        did: s for did, s in source[qid][:100]
                    }
                    for qid in qid_list
                }
                m = evaluate_official(qrels, run)
                all_results[f"{embed_name}_{name}"] = m
                print(
                    f"  {name:<25} "
                    f"nDCG@10={m['ndcg_cut_10']:.4f}"
                )

            for method_name, source in methods.items():
                print(f"\n  {method_name}:")
                run = {}
                for qi, qid in enumerate(qid_list):
                    candidates = source[qid][:30]
                    doc_with_text = [
                        (
                            did,
                            f"{corpus[did].get('title', '')} "
                            f"{corpus[did].get('text', '')}",
                        )
                        for did, _ in candidates
                    ]
                    reranked = rankzephyr_rerank(
                        queries[qid], doc_with_text,
                        model_rz, tokenizer,
                    )
                    run[qid] = {
                        did: s for did, s in reranked[:100]
                    }
                    if (qi + 1) % 10 == 0:
                        print(f"    {qi + 1}/{len(qid_list)}")

                m = evaluate_official(qrels, run)
                key = f"{embed_name}_{method_name}"
                all_results[key] = m
                print(
                    f"  {method_name:<25} "
                    f"nDCG@10={m['ndcg_cut_10']:.4f}"
                )

            del bm25_idx

    # Summary
    print(f"\n{'=' * 60}")
    print("RANKZEPHYR 7B RESULTS")
    print(f"{'=' * 60}")
    for name, m in sorted(
        all_results.items(),
        key=lambda x: x[1]["ndcg_cut_10"],
        reverse=True,
    ):
        print(f"  {m['ndcg_cut_10']:.4f}  {name}")

    with open(args.output, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\nSaved to {args.output}")


if __name__ == "__main__":
    main()
