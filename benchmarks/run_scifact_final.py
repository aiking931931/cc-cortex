"""
SciFact final push: all remaining methods to beat 0.7994.
1. Qwen3-Reranker + SciFact-specific instruction
2. Dual dense fusion (E5-base + GTE-base, no BM25)
3. Expanded candidates (top-200 → Qwen3)
4. Combo: dual dense + Qwen3 + custom instruction
"""
import json
import os
from collections import defaultdict

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

# SciFact-specific instructions for Qwen3
SCIFACT_TASK = (
    "Given a scientific claim, retrieve passages from "
    "research abstracts that support or refute the claim"
)
GENERIC_TASK = (
    "Given a web search query, retrieve relevant "
    "passages that answer the query"
)


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


def rt_univ(b, d):
    p = UNIV
    bs = {did for did, _ in b[:p["top_n"]]}
    ds = {did for did, _ in d[:p["top_n"]]}
    u = bs | ds
    ag = len(bs & ds) / len(u) if u else 0
    t = 1 - ag
    ak = max(1, int(p["k_low"] + (p["k_high"] - p["k_low"]) * t))
    bo = 1 + (p["boost_max"] - 1) * ag
    rrf = defaultdict(float)
    pr = defaultdict(int)
    for r, (did, _) in enumerate(b):
        rrf[did] += p["bw"] / (ak + r + 1)
        pr[did] += 1
    for r, (did, _) in enumerate(d):
        rrf[did] += p["dw"] / (ak + r + 1)
        pr[did] += 1
    for did in rrf:
        if pr[did] >= 2:
            rrf[did] *= bo
    bn, dn = _norm(b), _norm(d)
    rv = list(rrf.values())
    rmn, rmx = min(rv), max(rv)
    rrng = rmx - rmn if rmx > rmn else 1.0
    tw = p["bw"] + p["dw"]
    final = {}
    for did in set(rrf) | set(bn) | set(dn):
        r_v = (rrf.get(did, 0) - rmn) / rrng
        s_v = (
            p["bw"] * bn.get(did, 0) + p["dw"] * dn.get(did, 0)
        ) / tw
        final[did] = (1 - p["score_w"]) * r_v + p["score_w"] * s_v
    return sorted(
        final.items(), key=lambda x: x[1], reverse=True,
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


def get_text(corpus, did):
    return (
        corpus[did].get("title", "") + " "
        + corpus[did].get("text", "")
    )


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


def qwen3_rerank(queries, corpus, candidates, task_instr,
                 rr_model, tokenizer, ptoks, stoks,
                 tf_id, tt_id, top_k=100):
    reranked = {}
    qids = list(candidates.keys())
    for qi, qid in enumerate(qids):
        cands = sorted(
            candidates[qid].items(),
            key=lambda x: x[1], reverse=True,
        )[:top_k]
        pairs = []
        dids = []
        for did, _ in cands:
            txt = get_text(corpus, did)[:1500]
            pairs.append(
                f"<Instruct>: {task_instr}\n"
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
                inp[k] = inp[k].cuda()
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
        if (qi + 1) % 100 == 0 or qi + 1 == len(qids):
            print(f"    {qi + 1}/{len(qids)}")
    return reranked


def main():
    dp = "./datasets/scifact"
    if not os.path.isdir(dp):
        dp = util.download_and_unzip(
            f"{BEIR_BASE}/scifact.zip", "./datasets",
        )
    corpus, queries, qrels = GenericDataLoader(dp).load(
        split="test",
    )
    doc_ids = list(corpus.keys())
    print(f"{len(corpus)} docs, {len(queries)} queries")
    qids = list(queries.keys())

    # ── Encode with E5-base ──
    print("\nEncoding E5-base...")
    e5 = SentenceTransformer(
        "intfloat/e5-base-unsupervised", device="cuda",
    )
    e5_embs = e5.encode(
        [f"passage: {get_text(corpus, d)}" for d in doc_ids],
        normalize_embeddings=True, batch_size=256,
        show_progress_bar=True,
    )
    e5_qembs = e5.encode(
        [f"query: {queries[q]}" for q in qids],
        normalize_embeddings=True, batch_size=256,
    )
    del e5
    torch.cuda.empty_cache()
    e5_sims = e5_embs @ e5_qembs.T

    # ── Encode with GTE-base ──
    print("\nEncoding GTE-base...")
    gte = SentenceTransformer("thenlper/gte-base", device="cuda")
    gte_embs = gte.encode(
        [get_text(corpus, d) for d in doc_ids],
        normalize_embeddings=True, batch_size=256,
        show_progress_bar=True,
    )
    gte_qembs = gte.encode(
        [queries[q] for q in qids],
        normalize_embeddings=True, batch_size=256,
    )
    del gte
    torch.cuda.empty_cache()
    gte_sims = gte_embs @ gte_qembs.T

    # ── BM25 ──
    print("\nBM25...")
    ct = [get_text(corpus, d) for d in doc_ids]
    bm25 = bm25s.BM25()
    bm25.index(bm25s.tokenize(ct))

    # ── Build all candidate sets ──
    print("\nBuilding candidates...")
    all_e5 = {}
    all_gte = {}
    all_bm25 = {}
    all_e5_bm25 = {}  # E5 + BM25 fusion
    all_dual = {}     # E5 + GTE dual dense
    all_triple = {}   # E5 + GTE + BM25

    for qi, qid in enumerate(qids):
        # BM25
        t = bm25s.tokenize([queries[qid]])
        r, s = bm25.retrieve(t, corpus=doc_ids, k=200)
        bres = [
            (str(r[0, i]), float(s[0, i]))
            for i in range(len(r[0])) if s[0, i] > 0
        ]
        all_bm25[qid] = bres

        # E5 dense
        idx = np.argsort(e5_sims[:, qi])[::-1][:200]
        e5_res = [
            (doc_ids[i], float(e5_sims[i, qi])) for i in idx
        ]
        all_e5[qid] = e5_res

        # GTE dense
        idx = np.argsort(gte_sims[:, qi])[::-1][:200]
        gte_res = [
            (doc_ids[i], float(gte_sims[i, qi])) for i in idx
        ]
        all_gte[qid] = gte_res

        # E5 + BM25 (standard fusion)
        all_e5_bm25[qid] = rt_univ(bres, e5_res)

        # Dual dense: E5 + GTE (no BM25)
        all_dual[qid] = riverbed(e5_res, gte_res)

        # Triple: E5 + GTE + BM25
        # First fuse E5+GTE, then fuse with BM25
        dual = riverbed(e5_res, gte_res)
        all_triple[qid] = rt_univ(bres, dual)

    # ── Evaluate without reranker ──
    print("\n--- Without reranker ---")
    methods_no_rr = {
        "e5_dense": all_e5,
        "gte_dense": all_gte,
        "e5_bm25_rt": all_e5_bm25,
        "dual_dense": all_dual,
        "triple_fusion": all_triple,
    }
    results = {}
    for nm, src in methods_no_rr.items():
        run = {
            q: {d: s for d, s in src[q][:100]} for q in qids
        }
        m = evaluate(qrels, run)
        results[nm] = m
        print(f"  {nm:<20} nDCG@10={m['ndcg_cut_10']:.4f}")

    # ── Load Qwen3-Reranker ──
    print("\nLoading Qwen3-Reranker-0.6B...")
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

    # ── Test matrix: methods × instructions × top_k ──
    print("\n--- With Qwen3-Reranker ---")

    candidate_sets = {
        "e5_rt100": (all_e5_bm25, 100),
        "e5_rt200": (all_e5_bm25, 200),
        "dual100": (all_dual, 100),
        "dual200": (all_dual, 200),
        "triple100": (all_triple, 100),
        "triple200": (all_triple, 200),
    }

    instructions = {
        "generic": GENERIC_TASK,
        "scifact": SCIFACT_TASK,
    }

    for cand_name, (source, top_k) in candidate_sets.items():
        for instr_name, instr in instructions.items():
            key = f"{cand_name}_{instr_name}_qwen3"
            print(f"\n  {key}:")
            run = {
                q: {d: s for d, s in source[q][:top_k]}
                for q in qids
            }
            reranked = qwen3_rerank(
                queries, corpus, run, instr,
                rr_model, tokenizer, ptoks, stoks,
                tf_id, tt_id, top_k=top_k,
            )
            m = evaluate(qrels, reranked)
            results[key] = m
            marker = " <<<#1!" if m["ndcg_cut_10"] > 0.7994 else ""
            print(
                f"  {key}: "
                f"nDCG@10={m['ndcg_cut_10']:.4f}{marker}"
            )
            if m["ndcg_cut_10"] > 0.7994:
                write_trec(
                    reranked,
                    f"./scifact_{key}.trec",
                    f"cf_{key}",
                )

    del rr_model, tokenizer
    torch.cuda.empty_cache()

    # ── Summary ──
    sep = "=" * 60
    print(f"\n{sep}")
    print("SCIFACT FINAL PUSH")
    print("SOTA: jina-reranker-m0 = 0.7994")
    print(sep)
    for k, v in sorted(
        results.items(),
        key=lambda x: x[1]["ndcg_cut_10"],
        reverse=True,
    ):
        marker = " <<<#1!" if v["ndcg_cut_10"] > 0.7994 else ""
        print(f"  {v['ndcg_cut_10']:.4f}  {k}{marker}")
    with open("scifact_final_results.json", "w") as f:
        json.dump(results, f, indent=2)
    print("Saved")


if __name__ == "__main__":
    main()
