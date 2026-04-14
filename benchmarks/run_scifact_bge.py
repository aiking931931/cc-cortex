"""SciFact BGE-large + Qwen3-Reranker + TREC run files"""
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

METRICS = {"ndcg_cut_10", "recall_100", "map"}
BEIR_BASE = (
    "https://public.ukp.informatik.tu-darmstadt.de"
    "/thakur/BEIR/datasets"
)
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

    model_id = "BAAI/bge-large-en-v1.5"
    os.makedirs("./cache", exist_ok=True)
    cache_p = "./cache/scifact_BAAI_bge_large_en_v1.5_embs.npz"
    ids_p = "./cache/scifact_BAAI_bge_large_en_v1.5_doc_ids.json"

    if os.path.exists(cache_p):
        embs = np.load(cache_p)["embs"]
        with open(ids_p) as f:
            doc_id_list = json.load(f)
        print(f"Cache: {embs.shape}")
    else:
        model = SentenceTransformer(model_id, device="cuda")
        texts = [get_text(corpus, d) for d in doc_ids]
        embs = model.encode(
            texts, normalize_embeddings=True,
            batch_size=256, show_progress_bar=True,
        )
        doc_id_list = doc_ids
        np.savez_compressed(cache_p, embs=embs)
        with open(ids_p, "w") as f:
            json.dump(doc_id_list, f)
        del model
        torch.cuda.empty_cache()

    print("BM25...")
    ct = [get_text(corpus, d) for d in doc_ids]
    bm25 = bm25s.BM25()
    bm25.index(bm25s.tokenize(ct))

    print("Query encode...")
    model = SentenceTransformer(model_id, device="cuda")
    qids = list(queries.keys())
    pq = "Represent this sentence for searching: "
    qembs = model.encode(
        [pq + queries[q] for q in qids],
        normalize_embeddings=True, batch_size=256,
    )
    del model
    torch.cuda.empty_cache()

    print("Dense retrieval (GPU)...")
    pt = torch.from_numpy(embs).cuda()
    qt = torch.from_numpy(qembs).cuda()
    sim = (pt @ qt.T).cpu().numpy()
    del pt, qt
    torch.cuda.empty_cache()

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
        idx = np.argsort(sim[:, qi])[::-1][:100]
        dres = [(doc_id_list[i], float(sim[i, qi])) for i in idx]
        all_dense[qid] = dres
        all_fusion[qid] = riverbed(bres, dres)
        all_rt[qid] = rt_univ(bres, dres)

    print("\n--- Without reranker ---")
    for nm, src in [
        ("dense", all_dense),
        ("riverbed", all_fusion),
        ("rt_univ", all_rt),
    ]:
        run = {q: {d: s for d, s in src[q][:100]} for q in qids}
        m = evaluate(qrels, run)
        print(f"  {nm:<15} nDCG@10={m['ndcg_cut_10']:.4f}")
        write_trec(run, f"./scifact_{nm}.trec", f"cf_{nm}")

    print("\nLoading Qwen3-Reranker...")
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
    task = (
        "Given a web search query, retrieve relevant "
        "passages that answer the query"
    )

    methods = {
        "bm25": all_bm25, "dense": all_dense,
        "fusion": all_fusion, "rt_univ": all_rt,
    }
    results = {}
    for mname, source in methods.items():
        print(f"\n  {mname}->qwen3:")
        run = {}
        for qi, qid in enumerate(qids):
            cands = source[qid][:100]
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
            run[qid] = {
                dids[i]: scores[i] for i in range(len(dids))
            }
            if (qi + 1) % 100 == 0:
                print(f"    {qi + 1}/{len(qids)}")
        m = evaluate(qrels, run)
        results[f"{mname}_qwen3"] = m
        print(
            f"  {mname}->qwen3  "
            f"nDCG@10={m['ndcg_cut_10']:.4f}"
        )
        write_trec(
            run, f"./scifact_{mname}_qwen3.trec",
            f"cf_{mname}_qwen3",
        )

    sep = "=" * 50
    print(f"\n{sep}")
    print("SCIFACT BGE-LARGE + QWEN3 RESULTS")
    print(sep)
    for k, v in sorted(
        results.items(),
        key=lambda x: x[1]["ndcg_cut_10"],
        reverse=True,
    ):
        print(f"  {v['ndcg_cut_10']:.4f}  {k}")
    with open("scifact_bge_qwen3.json", "w") as f:
        json.dump(results, f, indent=2)
    print("Saved + TREC files written")


if __name__ == "__main__":
    main()
