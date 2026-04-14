"""
SciFact: fusion → Claude Opus listwise rerank.
300 queries × top-30 docs.
Goal: beat 0.7994 (jina-reranker-m0).
"""
import json
import os
from collections import defaultdict

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
UNIV = {
    "k_low": 2, "k_high": 5, "top_n": 20,
    "boost_max": 1.2, "score_w": 0.5, "bw": 0.8, "dw": 1.0,
}
ANTHROPIC_KEY = __import__("os").environ.get("ANTHROPIC_API_KEY", "")


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


def claude_listwise_rerank(query, docs, client, top_k=30):
    doc_texts = []
    for i, (did, text) in enumerate(docs[:top_k]):
        truncated = text[:500]
        doc_texts.append(f"[{i + 1}] {truncated}")
    docs_str = "\n".join(doc_texts)
    n = len(doc_texts)

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
            model="claude-opus-4-20250514",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        ranking = json.loads(text)
        reranked = []
        for rank_idx, pos in enumerate(ranking):
            if 1 <= pos <= n:
                did = docs[pos - 1][0]
                reranked.append((did, float(n - rank_idx)))
        return reranked
    except Exception as e:
        print(f"    Claude error: {e}")
        return [(did, float(n - i)) for i, (did, _) in enumerate(docs[:top_k])]


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

    sims = embs @ qembs.T

    print("BM25...")
    ct = [get_text(corpus, d) for d in doc_ids]
    bm25 = bm25s.BM25()
    bm25.index(bm25s.tokenize(ct))

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

    # Claude listwise rerank
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    methods = {
        "dense": all_dense,
        "fusion": all_fusion,
        "rt_univ": all_rt,
    }
    results = {}

    for mname, source in methods.items():
        print(f"\n  {mname}→claude:")
        run = {}
        for qi, qid in enumerate(qids):
            cands = source[qid][:30]
            doc_with_text = [
                (did, get_text(corpus, did))
                for did, _ in cands
            ]
            reranked = claude_listwise_rerank(
                queries[qid], doc_with_text, client,
            )
            run[qid] = {did: s for did, s in reranked}
            if (qi + 1) % 50 == 0:
                print(f"    {qi + 1}/{len(qids)}")
        m = evaluate(qrels, run)
        results[f"{mname}_claude"] = m
        print(
            f"  {mname}→claude: "
            f"nDCG@10={m['ndcg_cut_10']:.4f}"
        )
        write_trec(
            run, f"./scifact_{mname}_claude.trec",
            f"cf_{mname}_claude",
        )

    sep = "=" * 50
    print(f"\n{sep}")
    print("SCIFACT CLAUDE LISTWISE RESULTS")
    print("SOTA: jina-reranker-m0 = 0.7994")
    print(sep)
    for k, v in sorted(
        results.items(),
        key=lambda x: x[1]["ndcg_cut_10"],
        reverse=True,
    ):
        marker = " <<<#1!" if v["ndcg_cut_10"] > 0.7994 else ""
        print(f"  {v['ndcg_cut_10']:.4f}  {k}{marker}")
    with open("scifact_claude_results.json", "w") as f:
        json.dump(results, f, indent=2)
    print("Saved + TREC files written")


if __name__ == "__main__":
    main()
