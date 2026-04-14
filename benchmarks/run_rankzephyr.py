"""RankZephyr via rank_llm official API — TREC-COVID only"""
import json
import os

import bm25s
import numpy as np
import pytrec_eval
import torch
from beir import util
from beir.datasets.data_loader import GenericDataLoader
from rank_llm.data import Candidate, Request
from rank_llm.rerank import RankLLMAgent, Reranker
from sentence_transformers import SentenceTransformer

BEIR_BASE = (
    "https://public.ukp.informatik.tu-darmstadt.de"
    "/thakur/BEIR/datasets"
)
METRICS = {"ndcg_cut_10", "recall_100", "map"}


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
    all_docs = set(bn) | set(dn)
    return sorted(
        {
            x: (bw * bn.get(x, 0) + dw * dn.get(x, 0)) / tw
            for x in all_docs
        }.items(),
        key=lambda x: x[1],
        reverse=True,
    )


def evaluate_official(qrels, run_dict):
    qi = {
        q: {d: int(r) for d, r in v.items()}
        for q, v in qrels.items()
    }
    ev = pytrec_eval.RelevanceEvaluator(qi, METRICS)
    sc = ev.evaluate(run_dict)
    return {
        m: round(sum(sc[q].get(m, 0) for q in sc) / len(sc), 6)
        for m in METRICS
    }


def get_doc_text(corpus, did):
    title = corpus[did].get("title", "")
    text = corpus[did].get("text", "")
    return f"{title} {text}"


def main():
    ds = "trec-covid"
    dp = f"./datasets/{ds}"
    if not os.path.isdir(dp):
        dp = util.download_and_unzip(
            f"{BEIR_BASE}/{ds}.zip", "./datasets",
        )
    corpus, queries, qrels = GenericDataLoader(dp).load(
        split="test",
    )
    doc_ids = list(corpus.keys())
    print(f"{len(corpus)} docs, {len(queries)} queries")

    # Load BGE-large cache
    tag = "BAAI_bge_large_en_v1.5"
    epath = f"./cache/{ds}_{tag}_embs.npz"
    ipath = f"./cache/{ds}_{tag}_doc_ids.json"
    embs = np.load(epath)["embs"]
    with open(ipath) as f:
        doc_id_list = json.load(f)
    print(f"Cache: {embs.shape}")

    # BM25
    print("BM25...")
    ct = [get_doc_text(corpus, d) for d in doc_ids]
    bm25 = bm25s.BM25()
    bm25.index(bm25s.tokenize(ct))

    # Query encoding
    print("Query encoding...")
    model = SentenceTransformer(
        "BAAI/bge-large-en-v1.5", device="cuda",
    )
    qids = list(queries.keys())
    qts = [
        "Represent this sentence for searching: "
        + queries[q]
        for q in qids
    ]
    qembs = model.encode(
        qts, normalize_embeddings=True, batch_size=256,
    )
    del model
    torch.cuda.empty_cache()

    # Dense retrieval (GPU)
    print("Dense retrieval (GPU)...")
    pt = torch.from_numpy(embs).cuda()
    qt = torch.from_numpy(qembs).cuda()
    sim_matrix = (pt @ qt.T).cpu().numpy()
    del pt, qt
    torch.cuda.empty_cache()

    # Build candidates
    print("Building candidates...")
    all_bm25 = {}
    all_dense = {}
    all_fusion = {}
    for qi, qid in enumerate(qids):
        t = bm25s.tokenize([queries[qid]])
        r, s = bm25.retrieve(t, corpus=doc_ids, k=100)
        bres = [
            (str(r[0, i]), float(s[0, i]))
            for i in range(len(r[0]))
            if s[0, i] > 0
        ]
        all_bm25[qid] = bres
        idx = np.argsort(sim_matrix[:, qi])[::-1][:100]
        dres = [
            (doc_id_list[i], float(sim_matrix[i, qi]))
            for i in idx
        ]
        all_dense[qid] = dres
        all_fusion[qid] = riverbed(bres, dres)

    # Baselines
    print("\n--- Baselines ---")
    for nm, src in [("dense", all_dense), ("fusion", all_fusion)]:
        run = {
            q: {d: s for d, s in src[q][:100]}
            for q in qids
        }
        m = evaluate_official(qrels, run)
        print(f"  {nm:<15} nDCG@10={m['ndcg_cut_10']:.4f}")

    # RankZephyr
    print("\nLoading RankZephyr...")
    reranker = Reranker(
        model_path="castorini/rank_zephyr_7b_v1_full",
        rerank_agent=RankLLMAgent.RANKZEPHYR,
    )

    methods = {
        "bm25": all_bm25,
        "dense": all_dense,
        "fusion": all_fusion,
    }
    results = {}
    for mname, source in methods.items():
        print(f"\n  {mname}->rankzephyr:")
        run = {}
        for qi, qid in enumerate(qids):
            cands = source[qid][:30]
            candidates = [
                Candidate(
                    docid=did,
                    score=sc,
                    doc={"text": get_doc_text(corpus, did)},
                )
                for did, sc in cands
            ]
            req = Request(
                query=queries[qid], candidates=candidates,
            )
            result = reranker.rerank_batch([req])
            reranked = result[0].candidates
            run[qid] = {
                c.docid: float(len(reranked) - i)
                for i, c in enumerate(reranked)
            }
            if (qi + 1) % 10 == 0:
                print(f"    {qi + 1}/{len(qids)}")
        m = evaluate_official(qrels, run)
        results[f"{mname}_rankzephyr"] = m
        print(
            f"  {mname}->rankzephyr  "
            f"nDCG@10={m['ndcg_cut_10']:.4f}"
        )

    # Summary
    print(f"\n{'=' * 50}")
    print("RANKZEPHYR RESULTS")
    print(f"{'=' * 50}")
    for k, v in sorted(
        results.items(),
        key=lambda x: x[1]["ndcg_cut_10"],
        reverse=True,
    ):
        print(f"  {v['ndcg_cut_10']:.4f}  {k}")
    with open("rankzephyr_results.json", "w") as f:
        json.dump(results, f, indent=2)
    print("Saved to rankzephyr_results.json")


if __name__ == "__main__":
    main()
