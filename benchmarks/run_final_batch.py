"""
Final batch:
1. TREC-COVID fusion→Qwen3 TREC run file (for submission)
2. ArguAna no-prefix test (fix score gap)
3. FiQA no-prefix test (fix score gap)
"""
import json
import os

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
UNIV = {
    "k_low": 2, "k_high": 5, "top_n": 20,
    "boost_max": 1.2, "score_w": 0.5, "bw": 0.8, "dw": 1.0,
}


def _norm(r):
    if not r:
        return {}
    v = [s for _, s in r]
    mn, mx = min(v), max(v)
    rng = mx - mn if mx > mn else 1.0
    return {d: (s - mn) / rng for d, s in r}


def riverbed(b, d, bw=0.8, dw=1.0):
    bn, dn = _norm(b), _norm(d)
    tw = bw + dw
    return sorted(
        {
            x: (bw * bn.get(x, 0) + dw * dn.get(x, 0)) / tw
            for x in set(bn) | set(dn)
        }.items(),
        key=lambda x: x[1], reverse=True,
    )


def get_text(corpus, did):
    return (
        corpus[did].get("title", "") + " "
        + corpus[did].get("text", "")
    )


def evaluate(qrels, run):
    qi = {
        q: {d: int(r) for d, r in v.items()}
        for q, v in qrels.items()
    }
    ev = pytrec_eval.RelevanceEvaluator(qi, METRICS)
    sc = ev.evaluate(run)
    return {
        m: round(sum(sc[q].get(m, 0) for q in sc) / len(sc), 6)
        for m in METRICS
    }


def write_trec(run, path, name):
    with open(path, "w") as f:
        for qid in sorted(run, key=str):
            docs = sorted(
                run[qid].items(),
                key=lambda x: x[1], reverse=True,
            )
            for r, (did, sc) in enumerate(docs, 1):
                f.write(
                    f"{qid} Q0 {did} {r} {sc:.6f} {name}\n"
                )


def qwen3_rerank(queries, corpus, run_dict, rr_model,
                 tokenizer, ptoks, stoks, tf_id, tt_id,
                 device="cuda"):
    task = (
        "Given a web search query, retrieve relevant "
        "passages that answer the query"
    )
    reranked = {}
    qids = list(run_dict.keys())
    for qi, qid in enumerate(qids):
        cands = sorted(
            run_dict[qid].items(),
            key=lambda x: x[1], reverse=True,
        )[:100]
        pairs = []
        dids = []
        for did, _ in cands:
            txt = get_text(corpus, did)[:1500]
            pairs.append(
                f"<Instruct>: {task}\n"
                f"<Query>: {queries[qid]}\n"
                f"<Document>: {txt}"
            )
            dids.append(did)
        scores = []
        for bi in range(0, len(pairs), 8):
            bp = pairs[bi:bi + 8]
            inp = tokenizer(
                bp, padding=False,
                truncation="longest_first",
                return_attention_mask=False,
                max_length=4096 - len(ptoks) - len(stoks),
            )
            for i, e in enumerate(inp["input_ids"]):
                inp["input_ids"][i] = ptoks + e + stoks
            inp = tokenizer.pad(
                inp, padding=True,
                return_tensors="pt", max_length=4096,
            )
            for k in inp:
                inp[k] = inp[k].to(device)
            with torch.no_grad():
                lo = rr_model(**inp).logits[:, -1, :]
                st = torch.stack(
                    [lo[:, tf_id], lo[:, tt_id]], dim=1,
                )
                lp = torch.nn.functional.log_softmax(st, dim=1)
                scores.extend(lp[:, 1].exp().tolist())
            del inp, lo
            torch.cuda.empty_cache()
        reranked[qid] = {
            dids[i]: scores[i] for i in range(len(dids))
        }
        if (qi + 1) % 10 == 0 or qi + 1 == len(qids):
            print(f"    Reranked {qi + 1}/{len(qids)}")
    return reranked


def gpu_matmul(a, b):
    if torch.cuda.is_available() and a.shape[0] * b.shape[0] > 1e8:
        at = torch.from_numpy(a).cuda()
        bt = torch.from_numpy(b).cuda()
        r = (at @ bt.T).cpu().numpy()
        del at, bt
        torch.cuda.empty_cache()
        return r
    return a @ b.T


def run_dataset(ds_name, model_id, prefix_q, prefix_d,
                use_bm25, cache_dir, data_dir):
    """Run retrieval + return candidates."""
    if ds_name == "arguana":
        url_name = "arguana"
    else:
        url_name = ds_name
    dp = os.path.join(data_dir, ds_name)
    if not os.path.isdir(dp):
        dp = util.download_and_unzip(
            f"{BEIR_BASE}/{url_name}.zip", data_dir,
        )
    corpus, queries, qrels = GenericDataLoader(dp).load(
        split="test",
    )
    doc_ids = list(corpus.keys())
    print(f"  {len(corpus)} docs, {len(queries)} queries")

    tag = model_id.replace("/", "_").replace("-", "_")
    cache_key = f"{ds_name}_{tag}"
    cache_p = os.path.join(cache_dir, f"{cache_key}_embs.npz")
    ids_p = os.path.join(cache_dir, f"{cache_key}_doc_ids.json")

    model = SentenceTransformer(model_id, device="cuda")
    if os.path.exists(cache_p):
        embs = np.load(cache_p)["embs"]
        with open(ids_p) as f:
            doc_id_list = json.load(f)
        print(f"  Cache: {embs.shape}")
    else:
        texts = [
            f"{prefix_d}{get_text(corpus, d)}".strip()
            for d in doc_ids
        ]
        print(f"  Encoding {len(texts)} docs...")
        embs = model.encode(
            texts, normalize_embeddings=True,
            batch_size=256, show_progress_bar=True,
        )
        doc_id_list = doc_ids
        np.savez_compressed(cache_p, embs=embs)
        with open(ids_p, "w") as f:
            json.dump(doc_id_list, f)

    qids = list(queries.keys())
    qembs = model.encode(
        [prefix_q + queries[q] for q in qids],
        normalize_embeddings=True, batch_size=256,
    )
    del model
    torch.cuda.empty_cache()

    sims = gpu_matmul(embs, qembs)

    all_bm25 = {}
    all_dense = {}
    all_fusion = {}
    if use_bm25:
        ct = [get_text(corpus, d) for d in doc_ids]
        bm25 = bm25s.BM25()
        bm25.index(bm25s.tokenize(ct))

    for qi, qid in enumerate(qids):
        idx = np.argsort(sims[:, qi])[::-1][:100]
        dres = [
            (doc_id_list[i], float(sims[i, qi])) for i in idx
        ]
        all_dense[qid] = dres
        if use_bm25:
            t = bm25s.tokenize([queries[qid]])
            r, s = bm25.retrieve(t, corpus=doc_ids, k=100)
            bres = [
                (str(r[0, i]), float(s[0, i]))
                for i in range(len(r[0])) if s[0, i] > 0
            ]
            all_bm25[qid] = bres
            all_fusion[qid] = riverbed(bres, dres)
        else:
            all_fusion[qid] = dres

    return corpus, queries, qrels, qids, all_dense, all_fusion


def main():
    cache_dir = "./cache"
    data_dir = "./datasets"
    os.makedirs(cache_dir, exist_ok=True)
    os.makedirs(data_dir, exist_ok=True)
    results = {}

    # ── 1. TREC-COVID fusion→Qwen3 TREC run file ──
    print("\n" + "=" * 50)
    print("TREC-COVID: Generate TREC run file for #1 score")
    print("=" * 50)

    corpus, queries, qrels, qids, dense, fusion = run_dataset(
        "trec-covid", "BAAI/bge-large-en-v1.5",
        "Represent this sentence for searching: ", "",
        True, cache_dir, data_dir,
    )

    # Evaluate fusion without reranker
    fusion_run = {
        q: {d: s for d, s in fusion[q][:100]} for q in qids
    }
    m = evaluate(qrels, fusion_run)
    print(f"  fusion (no rr): nDCG@10={m['ndcg_cut_10']:.4f}")

    # Load Qwen3
    print("  Loading Qwen3-Reranker...")
    tokenizer = AutoTokenizer.from_pretrained(
        "Qwen/Qwen3-Reranker-0.6B", padding_side="left",
    )
    rr_model = AutoModelForCausalLM.from_pretrained(
        "Qwen/Qwen3-Reranker-0.6B", torch_dtype=torch.float16,
    ).cuda().eval()
    tf_id = tokenizer.convert_tokens_to_ids("no")
    tt_id = tokenizer.convert_tokens_to_ids("yes")
    prefix = (
        "<|im_start|>system\nJudge whether the Document meets "
        "the requirements based on the Query and the Instruct "
        "provided. Note that the answer can only be "
        '"yes" or "no".<|im_end|>\n<|im_start|>user\n'
    )
    suffix = (
        "<|im_end|>\n<|im_start|>assistant\n"
        "<think>\n\n</think>\n\n"
    )
    ptoks = tokenizer.encode(prefix, add_special_tokens=False)
    stoks = tokenizer.encode(suffix, add_special_tokens=False)

    # Rerank fusion
    print("  fusion→Qwen3:")
    reranked = qwen3_rerank(
        queries, corpus, fusion_run,
        rr_model, tokenizer, ptoks, stoks, tf_id, tt_id,
    )
    m = evaluate(qrels, reranked)
    results["trec-covid_fusion_qwen3"] = m
    print(f"  fusion→qwen3: nDCG@10={m['ndcg_cut_10']:.4f}")
    write_trec(
        reranked,
        "./trec-covid_fusion_qwen3.trec",
        "confluence_fusion_qwen3",
    )
    print("  TREC run file written!")

    del rr_model, tokenizer
    torch.cuda.empty_cache()

    # ── 2. ArguAna no-prefix test ──
    print("\n" + "=" * 50)
    print("ARGUANA: No-prefix test (fix score gap)")
    print("=" * 50)

    # With prefix (current)
    print("  [with prefix]")
    _, _, qrels_a, qids_a, dense_wp, _ = run_dataset(
        "arguana", "BAAI/bge-base-en-v1.5",
        "Represent this sentence for searching: ", "",
        False, cache_dir, data_dir,
    )
    run_wp = {q: {d: s for d, s in dense_wp[q][:100]} for q in qids_a}
    m_wp = evaluate(qrels_a, run_wp)
    print(f"  with prefix: nDCG@10={m_wp['ndcg_cut_10']:.4f}")

    # Without prefix (test)
    print("  [no prefix]")
    _, _, _, _, dense_np, _ = run_dataset(
        "arguana", "BAAI/bge-base-en-v1.5",
        "", "",  # no prefix
        False,
        cache_dir + "/noprefix", data_dir,
    )
    run_np = {q: {d: s for d, s in dense_np[q][:100]} for q in qids_a}
    m_np = evaluate(qrels_a, run_np)
    print(f"  no prefix: nDCG@10={m_np['ndcg_cut_10']:.4f}")
    results["arguana_with_prefix"] = m_wp
    results["arguana_no_prefix"] = m_np

    # ── 3. FiQA no-prefix test ──
    print("\n" + "=" * 50)
    print("FIQA: No-prefix test (fix score gap)")
    print("=" * 50)

    print("  [with prefix — GTE-base has no prefix]")
    _, _, qrels_f, qids_f, dense_f, fusion_f = run_dataset(
        "fiqa", "thenlper/gte-base",
        "", "",  # GTE has no prefix
        True, cache_dir, data_dir,
    )
    for nm, src in [("dense", dense_f), ("fusion", fusion_f)]:
        run_f = {q: {d: s for d, s in src[q][:100]} for q in qids_f}
        m_f = evaluate(qrels_f, run_f)
        print(f"  {nm}: nDCG@10={m_f['ndcg_cut_10']:.4f}")
        results[f"fiqa_{nm}"] = m_f

    # Summary
    sep = "=" * 50
    print(f"\n{sep}")
    print("FINAL BATCH RESULTS")
    print(sep)
    for k, v in results.items():
        print(f"  {k:<35} nDCG@10={v['ndcg_cut_10']:.4f}")
    with open("final_batch_results.json", "w") as f:
        json.dump(results, f, indent=2)
    print("Saved to final_batch_results.json")


if __name__ == "__main__":
    main()
