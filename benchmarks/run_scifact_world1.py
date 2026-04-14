"""
SciFact: Take back #1 with stronger rerankers.
Test: E5-base fusion → mxbai-rerank-large-v2 / jina-reranker-v3
SOTA: jina-reranker-m0 = 0.7994

Usage: python run_scifact_world1.py
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
from sentence_transformers import CrossEncoder, SentenceTransformer

BEIR_BASE = (
    "https://public.ukp.informatik.tu-darmstadt.de"
    "/thakur/BEIR/datasets"
)
METRICS = {"ndcg_cut_10", "recall_100", "map"}
UNIV = {
    "k_low": 2, "k_high": 5, "top_n": 20,
    "boost_max": 1.2, "score_w": 0.5, "bw": 0.8, "dw": 1.0,
}

RERANKERS = [
    "mixedbread-ai/mxbai-rerank-large-v1",
    "BAAI/bge-reranker-v2-m3",
]


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


def cross_encoder_rerank(reranker, queries, corpus, run_dict):
    reranked = {}
    qids = list(run_dict.keys())
    for qi, qid in enumerate(qids):
        docs = sorted(
            run_dict[qid].items(),
            key=lambda x: x[1], reverse=True,
        )[:100]
        pairs = [
            (queries[qid], get_text(corpus, did))
            for did, _ in docs
        ]
        dids = [did for did, _ in docs]
        scores = reranker.predict(
            pairs, batch_size=32, show_progress_bar=False,
        )
        reranked[qid] = {
            dids[i]: float(scores[i]) for i in range(len(dids))
        }
        if (qi + 1) % 100 == 0:
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

    # E5-base (best model for SciFact)
    model_id = "intfloat/e5-base-unsupervised"
    model = SentenceTransformer(model_id, device="cuda")
    texts = [
        f"passage: {get_text(corpus, d)}".strip()
        for d in doc_ids
    ]
    print("Encoding corpus...")
    embs = model.encode(
        texts, normalize_embeddings=True,
        batch_size=256, show_progress_bar=True,
    )
    qids = list(queries.keys())
    qembs = model.encode(
        [f"query: {queries[q]}" for q in qids],
        normalize_embeddings=True, batch_size=256,
    )
    del model
    torch.cuda.empty_cache()

    # Dense retrieval
    print("Dense retrieval...")
    sims = embs @ qembs.T

    # BM25
    print("BM25...")
    ct = [get_text(corpus, d) for d in doc_ids]
    bm25 = bm25s.BM25()
    bm25.index(bm25s.tokenize(ct))

    # Build candidates
    all_bm25 = {}
    all_dense = {}
    all_fusion = {}
    all_rt = {}
    for qi, qid in enumerate(qids):
        t = bm25s.tokenize([queries[qid]])
        r, s = bm25.retrieve(t, corpus=doc_ids, k=100)
        bres = [
            (str(r[0, i]), float(s[0, i]))
            for i in range(len(r[0])) if s[0, i] > 0
        ]
        all_bm25[qid] = bres
        idx = np.argsort(sims[:, qi])[::-1][:100]
        dres = [(doc_ids[i], float(sims[i, qi])) for i in idx]
        all_dense[qid] = dres
        all_fusion[qid] = riverbed(bres, dres)
        all_rt[qid] = rt_univ(bres, dres)

    # Baselines
    print("\n--- Without reranker ---")
    for nm, src in [
        ("dense", all_dense), ("fusion", all_fusion),
        ("rt_univ", all_rt),
    ]:
        run = {q: {d: s for d, s in src[q][:100]} for q in qids}
        m = evaluate(qrels, run)
        print(f"  {nm:<15} nDCG@10={m['ndcg_cut_10']:.4f}")

    # Test each reranker
    results = {}
    methods = {
        "bm25": all_bm25, "dense": all_dense,
        "fusion": all_fusion, "rt_univ": all_rt,
    }

    for rr_id in RERANKERS:
        print(f"\nReranker: {rr_id}")
        reranker = CrossEncoder(rr_id, device="cuda")

        for mname, source in methods.items():
            run = {
                q: {d: s for d, s in source[q][:100]}
                for q in qids
            }
            reranked = cross_encoder_rerank(
                reranker, queries, corpus, run,
            )
            m = evaluate(qrels, reranked)
            rr_short = rr_id.split("/")[-1]
            key = f"{mname}__{rr_short}"
            results[key] = m
            print(
                f"  {mname}→{rr_short}: "
                f"nDCG@10={m['ndcg_cut_10']:.4f}"
            )
            write_trec(
                reranked,
                f"./scifact_{mname}_{rr_short}.trec",
                f"cf_{mname}_{rr_short}",
            )

        del reranker
        torch.cuda.empty_cache()

    # Summary
    sep = "=" * 60
    print(f"\n{sep}")
    print("SCIFACT WORLD #1 ATTEMPT")
    print("SOTA: jina-reranker-m0 = 0.7994")
    print(sep)
    for k, v in sorted(
        results.items(),
        key=lambda x: x[1]["ndcg_cut_10"],
        reverse=True,
    ):
        marker = " <<<#1!" if v["ndcg_cut_10"] > 0.7994 else ""
        print(f"  {v['ndcg_cut_10']:.4f}  {k}{marker}")
    with open("scifact_world1_results.json", "w") as f:
        json.dump(results, f, indent=2)
    print("\nSaved + TREC files written")


if __name__ == "__main__":
    main()
