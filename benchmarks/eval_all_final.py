"""
Final evaluation: All 9 BEIR datasets with optimal config per dataset.
（最終評估：9 個 BEIR 資料集，每個用最佳配置）

Strategy per dataset:
- SciFact: E5-base + riverbed (already #1, just verify)
- TREC-COVID: BGE-large + Qwen3-Reranker-0.6B
- Quora: BGE-large + riverbed (already ~#1)
- Touché: GTE-large + riverbed (already #1 dense)
- SCIDOCS: BGE-base + riverbed (close to ceiling)
- NFCorpus: BGE-base + rt_full (close to SOTA)
- CQADupStack: BGE-base + riverbed (need to close 3pt gap)
- FiQA: GTE-base + riverbed (GTE-base is 0.487 on FiQA)
- ArguAna: BGE-base pure dense (no BM25, BM25 is poison)

Phase 1: All datasets with best embedding model + fusion
Phase 2: TREC-COVID + SciFact with Qwen3-Reranker-0.6B

Usage: python eval_all_final.py --phase 1 --cache-dir ./cache
       python eval_all_final.py --phase 2 --cache-dir ./cache
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

# Best model per dataset (based on SOTA research)
# （每個資料集的最佳模型，基於排行榜研究）
DATASET_CONFIG = {
    "scifact": {
        "model": "intfloat/e5-base-unsupervised",
        "prefix_q": "query: ", "prefix_d": "passage: ",
        "use_bm25": True, "strategies": "all",
    },
    "trec-covid": {
        "model": "BAAI/bge-large-en-v1.5",
        "prefix_q": "Represent this sentence for searching: ",
        "prefix_d": "",
        "use_bm25": True, "strategies": "all",
    },
    "quora": {
        "model": "BAAI/bge-large-en-v1.5",
        "prefix_q": "Represent this sentence for searching: ",
        "prefix_d": "",
        "use_bm25": True, "strategies": "all",
    },
    "webis-touche2020": {
        "model": "thenlper/gte-large",
        "prefix_q": "", "prefix_d": "",
        "use_bm25": True, "strategies": "all",
    },
    "scidocs": {
        "model": "BAAI/bge-base-en-v1.5",
        "prefix_q": "Represent this sentence for searching: ",
        "prefix_d": "",
        "use_bm25": True, "strategies": "all",
    },
    "nfcorpus": {
        "model": "BAAI/bge-base-en-v1.5",
        "prefix_q": "Represent this sentence for searching: ",
        "prefix_d": "",
        "use_bm25": True, "strategies": "all",
    },
    "fiqa": {
        "model": "thenlper/gte-base",
        "prefix_q": "", "prefix_d": "",
        "use_bm25": True, "strategies": "all",
    },
    "arguana": {
        "model": "BAAI/bge-base-en-v1.5",
        "prefix_q": "Represent this sentence for searching: ",
        "prefix_d": "",
        "use_bm25": False, "strategies": "dense_only",
    },
}

# Universal params（通用參數）
UNIV = {
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
    p = UNIV
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


ALL_STRATEGIES = {
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


def get_doc_text(corpus, did):
    """Helper to get document text without f-string issues."""
    title = corpus[did].get("title", "")
    text = corpus[did].get("text", "")
    return f"{title} {text}"


def gpu_matmul(a, b):
    """Matrix multiply on GPU if large, CPU if small."""
    if (
        torch.cuda.is_available()
        and a.shape[0] * b.shape[0] > 1e8
    ):
        at = torch.from_numpy(a).cuda()
        bt = torch.from_numpy(b).cuda()
        result = (at @ bt.T).cpu().numpy()
        del at, bt
        torch.cuda.empty_cache()
        return result
    return a @ b.T


def encode_corpus(model, corpus, prefix_d, cache_key,
                  cache_dir):
    cache_path = os.path.join(
        cache_dir, f"{cache_key}_embs.npz",
    )
    ids_path = os.path.join(
        cache_dir, f"{cache_key}_doc_ids.json",
    )
    doc_id_list = list(corpus.keys())

    if os.path.exists(cache_path) and os.path.exists(ids_path):
        data = np.load(cache_path)
        print(f"  Cache hit: {data['embs'].shape}")
        return data["embs"], doc_id_list

    texts = [
        f"{prefix_d}{get_doc_text(corpus, did)}".strip()
        for did in doc_id_list
    ]
    print(f"  Encoding {len(texts)} docs...")
    t0 = time.time()
    embs = model.encode(
        texts, normalize_embeddings=True,
        batch_size=256, show_progress_bar=True,
    )
    print(f"  Done in {time.time() - t0:.0f}s")
    np.savez_compressed(cache_path, embs=embs)
    with open(ids_path, "w") as f:
        json.dump(doc_id_list, f)
    return embs, doc_id_list


def evaluate_dataset(ds_name, corpus, queries, qrels,
                     passage_embs, doc_id_list, model,
                     prefix_q, use_bm25, strat_filter):
    doc_ids = list(corpus.keys())

    # BM25 (if needed)
    all_bm25 = {}
    if use_bm25:
        print("  BM25 (bm25s)...")
        ct = [get_doc_text(corpus, did) for did in doc_ids]
        corpus_tokens = bm25s.tokenize(ct)
        bm25_idx = bm25s.BM25()
        bm25_idx.index(corpus_tokens)
        for qid in queries:
            qt = bm25s.tokenize([queries[qid]])
            r, s = bm25_idx.retrieve(
                qt, corpus=doc_ids, k=100,
            )
            all_bm25[qid] = [
                (str(r[0, i]), float(s[0, i]))
                for i in range(len(r[0]))
                if s[0, i] > 0
            ]
        del bm25_idx

    # Query encoding (GPU batch)
    print(f"  Encoding {len(queries)} queries...")
    qid_list = list(queries.keys())
    qt_list = [prefix_q + queries[qid] for qid in qid_list]
    q_embs = model.encode(
        qt_list, normalize_embeddings=True,
        batch_size=256, show_progress_bar=True,
    )

    # Dense retrieval (GPU for large matrices)
    print("  Dense retrieval...")
    all_sims = gpu_matmul(passage_embs, q_embs)

    # Evaluate strategies
    strategies = ALL_STRATEGIES
    if strat_filter == "dense_only":
        strategies = {"dense_only": ALL_STRATEGIES["dense_only"]}

    ds_results = {}
    for strat_name, strat_fn in strategies.items():
        run_dict = {}
        for qi, qid in enumerate(qid_list):
            sims = all_sims[:, qi]
            idx = np.argsort(sims)[::-1][:100]
            dense_res = [
                (doc_id_list[i], float(sims[i])) for i in idx
            ]
            if use_bm25 and strat_name != "dense_only":
                bm25_res = all_bm25.get(qid, [])
                fused = strat_fn(bm25_res, dense_res)
            else:
                fused = dense_res
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
    return ds_results


def qwen3_rerank(queries, corpus, run_dict, device="cuda"):
    """Rerank using Qwen3-Reranker-0.6B.
    （使用 Qwen3-Reranker-0.6B 重排序）
    """
    from transformers import AutoModelForCausalLM, AutoTokenizer

    print("  Loading Qwen3-Reranker-0.6B...")
    tokenizer = AutoTokenizer.from_pretrained(
        "Qwen/Qwen3-Reranker-0.6B", padding_side="left",
    )
    model = AutoModelForCausalLM.from_pretrained(
        "Qwen/Qwen3-Reranker-0.6B",
        torch_dtype=torch.float16,
    ).to(device).eval()

    token_false_id = tokenizer.convert_tokens_to_ids("no")
    token_true_id = tokenizer.convert_tokens_to_ids("yes")
    max_length = 4096

    prefix = (
        "<|im_start|>system\n"
        "Judge whether the Document meets the requirements "
        "based on the Query and the Instruct provided. "
        'Note that the answer can only be "yes" or "no".'
        "<|im_end|>\n<|im_start|>user\n"
    )
    suffix = (
        "<|im_end|>\n<|im_start|>assistant\n"
        "<think>\n\n</think>\n\n"
    )
    prefix_tokens = tokenizer.encode(
        prefix, add_special_tokens=False,
    )
    suffix_tokens = tokenizer.encode(
        suffix, add_special_tokens=False,
    )
    task = (
        "Given a web search query, retrieve relevant "
        "passages that answer the query"
    )

    reranked_run = {}
    qid_list = list(run_dict.keys())

    for qi, qid in enumerate(qid_list):
        doc_scores = sorted(
            run_dict[qid].items(),
            key=lambda x: x[1], reverse=True,
        )[:100]

        pairs = []
        doc_ids_ordered = []
        for did, _ in doc_scores:
            doc_text = get_doc_text(corpus, did)[:1500]
            formatted = (
                f"<Instruct>: {task}\n"
                f"<Query>: {queries[qid]}\n"
                f"<Document>: {doc_text}"
            )
            pairs.append(formatted)
            doc_ids_ordered.append(did)

        # Process in mini-batches to avoid OOM
        # （分批處理避免 VRAM 爆）
        scores = []
        batch_sz = 8
        for bi in range(0, len(pairs), batch_sz):
            batch_pairs = pairs[bi:bi + batch_sz]
            inputs = tokenizer(
                batch_pairs, padding=False,
                truncation="longest_first",
                return_attention_mask=False,
                max_length=(
                    max_length - len(prefix_tokens)
                    - len(suffix_tokens)
                ),
            )
            for i, ele in enumerate(inputs["input_ids"]):
                inputs["input_ids"][i] = (
                    prefix_tokens + ele + suffix_tokens
                )
            inputs = tokenizer.pad(
                inputs, padding=True,
                return_tensors="pt", max_length=max_length,
            )
            for key in inputs:
                inputs[key] = inputs[key].to(device)
            with torch.no_grad():
                logits = model(**inputs).logits[:, -1, :]
                true_v = logits[:, token_true_id]
                false_v = logits[:, token_false_id]
                stacked = torch.stack(
                    [false_v, true_v], dim=1,
                )
                log_probs = torch.nn.functional.log_softmax(
                    stacked, dim=1,
                )
                scores.extend(
                    log_probs[:, 1].exp().tolist()
                )
            del inputs, logits
            torch.cuda.empty_cache()

        reranked_run[qid] = {
            doc_ids_ordered[i]: scores[i]
            for i in range(len(doc_ids_ordered))
        }
        if (qi + 1) % 10 == 0 or qi + 1 == len(qid_list):
            print(f"    Reranked {qi + 1}/{len(qid_list)}")

    del model, tokenizer
    torch.cuda.empty_cache()
    return reranked_run


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir", default="./cache")
    parser.add_argument("--data-dir", default="./datasets")
    parser.add_argument("--output", default="final_results.json")
    parser.add_argument(
        "--phase", type=int, default=1,
        help="1=embedding+fusion, 2=Qwen3 reranker",
    )
    args = parser.parse_args()

    os.makedirs(args.cache_dir, exist_ok=True)
    os.makedirs(args.data_dir, exist_ok=True)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Device: {device}")

    all_results = {}

    if args.phase == 1:
        # ── Phase 1: Best embedding per dataset ──
        print("=" * 60)
        print("PHASE 1: Best embedding + fusion per dataset")
        print("=" * 60)

        # Add CQADupStack subforums (all use BGE-base)
        for forum in CQA_FORUMS:
            DATASET_CONFIG[f"cqa_{forum}"] = {
                "model": "BAAI/bge-base-en-v1.5",
                "prefix_q": (
                    "Represent this sentence for searching: "
                ),
                "prefix_d": "",
                "use_bm25": True, "strategies": "all",
            }

        # Group datasets by model to avoid reloading
        model_datasets = {}
        for ds_name, cfg in DATASET_CONFIG.items():
            mid = cfg["model"]
            if mid not in model_datasets:
                model_datasets[mid] = []
            model_datasets[mid].append(ds_name)

        for model_id, ds_names in model_datasets.items():
            print(f"\nLoading {model_id}...")
            model = SentenceTransformer(model_id, device=device)

            for ds_name in ds_names:
                cfg = DATASET_CONFIG[ds_name]
                print(
                    f"\n{'=' * 50}\n{ds_name.upper()} "
                    f"({model_id})\n{'=' * 50}"
                )

                # Load dataset
                if ds_name.startswith("cqa_"):
                    forum = ds_name[4:]
                    cqa_path = os.path.join(
                        args.data_dir, "cqadupstack",
                    )
                    if not os.path.isdir(cqa_path):
                        cqa_path = util.download_and_unzip(
                            f"{BEIR_BASE}/cqadupstack.zip",
                            args.data_dir,
                        )
                    data_path = os.path.join(cqa_path, forum)
                else:
                    data_path = os.path.join(
                        args.data_dir, ds_name,
                    )
                    if not os.path.isdir(data_path):
                        data_path = util.download_and_unzip(
                            f"{BEIR_BASE}/{ds_name}.zip",
                            args.data_dir,
                        )
                corpus, queries, qrels = GenericDataLoader(
                    data_path,
                ).load(split="test")
                print(
                    f"  {len(corpus)} docs, "
                    f"{len(queries)} queries"
                )

                # Encode
                tag = model_id.replace(
                    "/", "_"
                ).replace("-", "_")
                cache_key = f"{ds_name}_{tag}"
                embs, doc_ids = encode_corpus(
                    model, corpus, cfg["prefix_d"],
                    cache_key, args.cache_dir,
                )

                # Evaluate
                results = evaluate_dataset(
                    ds_name, corpus, queries, qrels,
                    embs, doc_ids, model,
                    cfg["prefix_q"], cfg["use_bm25"],
                    cfg["strategies"],
                )
                all_results[ds_name] = {
                    "model": model_id,
                    "results": results,
                }
                del embs

            del model
            torch.cuda.empty_cache()

        # CQADupStack average
        cqa_keys = [
            k for k in all_results if k.startswith("cqa_")
        ]
        if cqa_keys:
            cqa_avg = {}
            for strat in ALL_STRATEGIES:
                vals = [
                    all_results[k]["results"][strat][
                        "ndcg_cut_10"
                    ]
                    for k in cqa_keys
                    if strat in all_results[k]["results"]
                ]
                if vals:
                    cqa_avg[strat] = {
                        "ndcg_cut_10": round(
                            sum(vals) / len(vals), 6
                        ),
                    }
            all_results["cqadupstack_avg"] = {
                "model": "BGE-base",
                "results": cqa_avg,
            }

    elif args.phase == 2:
        # ── Phase 2: Qwen3-Reranker ──
        print("=" * 60)
        print("PHASE 2: Qwen3-Reranker-0.6B")
        print("=" * 60)

        # Load phase 1 results
        p1_path = args.output.replace(".json", "_p1.json")
        if os.path.exists(p1_path):
            with open(p1_path) as f:
                phase1 = json.load(f)
        else:
            print("Phase 1 results not found, run phase 1 first")
            return

        # Rerank TREC-COVID and SciFact
        for ds_name in ["trec-covid", "scifact"]:
            cfg = DATASET_CONFIG[ds_name]
            data_path = os.path.join(args.data_dir, ds_name)
            if not os.path.isdir(data_path):
                data_path = util.download_and_unzip(
                    f"{BEIR_BASE}/{ds_name}.zip",
                    args.data_dir,
                )
            corpus, queries, qrels = GenericDataLoader(
                data_path,
            ).load(split="test")

            _ = phase1.get(ds_name, {})  # verify exists

            # Rebuild run_dict from phase 1
            # Need to re-run retrieval to get run_dict
            model_id = cfg["model"]
            model = SentenceTransformer(model_id, device=device)
            tag = model_id.replace("/", "_").replace("-", "_")
            cache_key = f"{ds_name}_{tag}"
            embs, doc_id_list = encode_corpus(
                model, corpus, cfg["prefix_d"],
                cache_key, args.cache_dir,
            )
            qid_list = list(queries.keys())
            qt_list = [
                cfg["prefix_q"] + queries[qid]
                for qid in qid_list
            ]
            q_embs = model.encode(
                qt_list, normalize_embeddings=True,
                batch_size=256,
            )
            all_sims = gpu_matmul(embs, q_embs)

            # Build fusion run
            doc_ids = list(corpus.keys())
            ct = [get_doc_text(corpus, did) for did in doc_ids]
            bm25_idx = bm25s.BM25()
            bm25_idx.index(bm25s.tokenize(ct))

            fusion_run = {}
            for qi, qid in enumerate(qid_list):
                qt = bm25s.tokenize([queries[qid]])
                r, s = bm25_idx.retrieve(
                    qt, corpus=doc_ids, k=100,
                )
                bm25_res = [
                    (str(r[0, i]), float(s[0, i]))
                    for i in range(len(r[0]))
                    if s[0, i] > 0
                ]
                sims = all_sims[:, qi]
                idx = np.argsort(sims)[::-1][:100]
                dense_res = [
                    (doc_id_list[i], float(sims[i]))
                    for i in idx
                ]
                fused = riverbed(bm25_res, dense_res)
                fusion_run[qid] = {
                    did: float(score)
                    for did, score in fused[:100]
                }

            # Also build dense-only run
            dense_run = {}
            for qi, qid in enumerate(qid_list):
                sims = all_sims[:, qi]
                idx = np.argsort(sims)[::-1][:100]
                dense_run[qid] = {
                    doc_id_list[i]: float(sims[i])
                    for i in idx
                }

            # BM25-only run
            bm25_run = {}
            for qid in qid_list:
                qt = bm25s.tokenize([queries[qid]])
                r, s = bm25_idx.retrieve(
                    qt, corpus=doc_ids, k=100,
                )
                bm25_run[qid] = {
                    str(r[0, i]): float(s[0, i])
                    for i in range(len(r[0]))
                    if s[0, i] > 0
                }

            del model, embs, bm25_idx
            torch.cuda.empty_cache()

            # Rerank all three
            print(f"\n{'=' * 50}")
            print(f"{ds_name.upper()} + Qwen3-Reranker")
            print(f"{'=' * 50}")

            for run_name, run_dict in [
                ("bm25", bm25_run),
                ("dense", dense_run),
                ("fusion", fusion_run),
            ]:
                print(f"\n  {run_name} → Qwen3-Reranker:")
                reranked = qwen3_rerank(
                    queries, corpus, run_dict, device,
                )
                m = evaluate_official(qrels, reranked)
                key = f"{ds_name}__{run_name}__qwen3rr"
                all_results[key] = {
                    "model": cfg["model"],
                    "reranker": "Qwen3-Reranker-0.6B",
                    "first_stage": run_name,
                    "ndcg10": m["ndcg_cut_10"],
                    "metrics": m,
                }
                print(
                    f"  {run_name}→qwen3: "
                    f"nDCG@10={m['ndcg_cut_10']:.4f}"
                )

    # Save
    suffix = f"_p{args.phase}" if args.phase else ""
    out_path = args.output.replace(
        ".json", f"{suffix}.json",
    )
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\nSaved to {out_path}")

    # Summary
    print(f"\n{'=' * 60}")
    print("SUMMARY")
    print(f"{'=' * 60}")
    for key, data in sorted(all_results.items()):
        if "results" in data:
            r = data["results"]
            best_s = max(
                r, key=lambda s: r[s].get("ndcg_cut_10", 0),
            )
            best_v = r[best_s]["ndcg_cut_10"]
            print(
                f"  {key:<25} {data['model']:<35} "
                f"best={best_s}={best_v:.4f}"
            )
        elif "ndcg10" in data:
            print(
                f"  {key:<25} "
                f"{data.get('first_stage', '?')}→qwen3  "
                f"nDCG@10={data['ndcg10']:.4f}"
            )


if __name__ == "__main__":
    main()
