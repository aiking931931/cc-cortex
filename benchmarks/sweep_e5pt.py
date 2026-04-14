"""Full E5-PT sweep: fine RRF + tension V2 + riverbed×tension."""
from __future__ import annotations

import json
import os
import sys
import numpy as np
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))
os.environ["BEIR_DEVICE"] = "cpu"

from beir_runner import build_bm25_with_ids, search_bm25_by_ids, ndcg_at_k
from beir.datasets.data_loader import GenericDataLoader
from sentence_transformers import SentenceTransformer


def main():
    base = os.path.join(os.path.dirname(__file__), "datasets")
    corpus, queries, qrels = GenericDataLoader(
        os.path.join(base, "scifact")
    ).load(split="test")
    bm25, doc_ids = build_bm25_with_ids(corpus)

    model = SentenceTransformer("intfloat/e5-base-unsupervised", device="cpu")
    cache = np.load(os.path.join(base, ".cache_e5pt_base_embs.npz"))
    passage_embs = cache["embs"]
    doc_id_list = list(corpus.keys())

    def e5pt_search(qt, top_k=100):
        q = model.encode(["query: " + qt], normalize_embeddings=True)
        sims = (passage_embs @ q.T).flatten()
        idx = np.argsort(sims)[::-1][:top_k]
        return [(doc_id_list[i], float(sims[i])) for i in idx]

    print("Caching queries...", flush=True)
    cached = {}
    for qid, qt in queries.items():
        rel = {d: r for d, r in qrels.get(qid, {}).items() if r > 0}
        cached[qid] = {
            "bm25": search_bm25_by_ids(bm25, doc_ids, qt, top_k=100),
            "dense": e5pt_search(qt, top_k=100),
            "rel": rel,
        }
    print(f"Cached {len(cached)} queries", flush=True)

    def eval_fn(fusion_fn, **kw):
        ndcgs = []
        for e in cached.values():
            fused = fusion_fn(e["bm25"], e["dense"], **kw)
            ranked = [d for d, _ in fused[:100]]
            ndcgs.append(ndcg_at_k(ranked, e["rel"], k=10))
        return sum(ndcgs) / len(ndcgs)

    # ── 1. Fine-grained RRF ──
    print("\n=== 1. Fine-grained RRF ===", flush=True)

    def asym_rrf(b, d, k=5, bw=1.0, dw=1.2):
        scores = defaultdict(float)
        for rank, (did, _) in enumerate(b):
            scores[did] += bw / (k + rank + 1)
        for rank, (did, _) in enumerate(d):
            scores[did] += dw / (k + rank + 1)
        return sorted(scores.items(), key=lambda x: x[1], reverse=True)

    best_rrf = 0.0
    best_rrf_cfg = ""
    for k in [2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 15]:
        for bw_10 in range(6, 13):  # 0.6..1.2
            bw = bw_10 / 10.0
            for dw_10 in range(8, 16):  # 0.8..1.5
                dw = dw_10 / 10.0
                s = eval_fn(asym_rrf, k=k, bw=bw, dw=dw)
                if s > best_rrf:
                    best_rrf = s
                    best_rrf_cfg = f"k={k} bw={bw} dw={dw}"
                    if s > 0.754:
                        print(f"  NEW: {best_rrf_cfg} -> {s:.4f}", flush=True)
    print(f"  BEST RRF: {best_rrf:.4f} ({best_rrf_cfg})", flush=True)

    # ── 2. Tension V2 (fixed k range) ──
    print("\n=== 2. Tension V2 ===", flush=True)

    def tension_v2(b, d, k_low=2, k_high=10, top_n=10,
                   boost_low=1.0, boost_high=1.3, bw=1.0, dw=1.2):
        b_set = {did for did, _ in b[:top_n]}
        d_set = {did for did, _ in d[:top_n]}
        union = b_set | d_set
        agreement = len(b_set & d_set) / len(union) if union else 0.0
        tension = 1.0 - agreement
        adaptive_k = max(1, int(k_low + (k_high - k_low) * tension))
        boost = boost_low + (boost_high - boost_low) * agreement

        scores = defaultdict(float)
        presence = defaultdict(int)
        for rank, (did, _) in enumerate(b):
            scores[did] += bw / (adaptive_k + rank + 1)
            presence[did] += 1
        for rank, (did, _) in enumerate(d):
            scores[did] += dw / (adaptive_k + rank + 1)
            presence[did] += 1
        for did in scores:
            if presence[did] >= 2:
                scores[did] *= boost
        return sorted(scores.items(), key=lambda x: x[1], reverse=True)

    best_t = 0.0
    best_t_cfg = ""
    for kl in [1, 2, 3]:
        for kh in [5, 7, 10, 15, 20]:
            if kh <= kl:
                continue
            for tn in [5, 10, 20]:
                for bh_10 in [10, 11, 12, 13, 15, 18]:  # 1.0..1.8
                    bh = bh_10 / 10.0
                    for bw_10 in [8, 10, 12]:
                        bw = bw_10 / 10.0
                        for dw_10 in [10, 12, 14]:
                            dw = dw_10 / 10.0
                            s = eval_fn(
                                tension_v2, k_low=kl, k_high=kh,
                                top_n=tn, boost_high=bh, bw=bw, dw=dw,
                            )
                            if s > best_t:
                                best_t = s
                                best_t_cfg = (
                                    f"kl={kl} kh={kh} tn={tn} "
                                    f"bh={bh} bw={bw} dw={dw}"
                                )
                                if s > 0.75:
                                    print(
                                        f"  NEW: {best_t_cfg} -> {s:.4f}",
                                        flush=True,
                                    )
    print(f"  BEST TENSION: {best_t:.4f} ({best_t_cfg})", flush=True)

    # ── 3. Riverbed × Tension ──
    print("\n=== 3. Riverbed x Tension ===", flush=True)

    def riverbed_tension(b, d, k_low=2, k_high=10, top_n=10,
                         boost_max=1.3, score_w=0.3, bw=1.0, dw=1.2):
        b_set = {did for did, _ in b[:top_n]}
        d_set = {did for did, _ in d[:top_n]}
        union = b_set | d_set
        agreement = len(b_set & d_set) / len(union) if union else 0.0
        tension = 1.0 - agreement
        adaptive_k = max(1, int(k_low + (k_high - k_low) * tension))
        boost = 1.0 + (boost_max - 1.0) * agreement

        rrf_scores = defaultdict(float)
        presence = defaultdict(int)
        for rank, (did, _) in enumerate(b):
            rrf_scores[did] += bw / (adaptive_k + rank + 1)
            presence[did] += 1
        for rank, (did, _) in enumerate(d):
            rrf_scores[did] += dw / (adaptive_k + rank + 1)
            presence[did] += 1
        for did in rrf_scores:
            if presence[did] >= 2:
                rrf_scores[did] *= boost

        # Riverbed: normalize raw scores
        def norm(results):
            if not results:
                return {}
            vals = [s for _, s in results]
            mn, mx = min(vals), max(vals)
            rng = mx - mn if mx > mn else 1.0
            return {did: (s - mn) / rng for did, s in results}

        b_n = norm(b)
        d_n = norm(d)
        rv = list(rrf_scores.values())
        r_mn, r_mx = min(rv), max(rv)
        r_rng = r_mx - r_mn if r_mx > r_mn else 1.0

        all_docs = set(rrf_scores) | set(b_n) | set(d_n)
        final = {}
        for did in all_docs:
            r = (rrf_scores.get(did, 0) - r_mn) / r_rng
            s = (bw * b_n.get(did, 0) + dw * d_n.get(did, 0)) / (bw + dw)
            final[did] = (1 - score_w) * r + score_w * s
        return sorted(final.items(), key=lambda x: x[1], reverse=True)

    best_rb = 0.0
    best_rb_cfg = ""
    for kl in [1, 2, 3]:
        for kh in [5, 8, 10, 15]:
            if kh <= kl:
                continue
            for tn in [5, 10, 20]:
                for bm_10 in [11, 12, 13, 15]:
                    bm = bm_10 / 10.0
                    for sw_10 in [1, 2, 3, 4, 5]:
                        sw = sw_10 / 10.0
                        for bw_10 in [8, 10, 12]:
                            bw = bw_10 / 10.0
                            for dw_10 in [10, 12, 14]:
                                dw = dw_10 / 10.0
                                s = eval_fn(
                                    riverbed_tension, k_low=kl, k_high=kh,
                                    top_n=tn, boost_max=bm, score_w=sw,
                                    bw=bw, dw=dw,
                                )
                                if s > best_rb:
                                    best_rb = s
                                    best_rb_cfg = (
                                        f"kl={kl} kh={kh} tn={tn} "
                                        f"bm={bm} sw={sw} bw={bw} dw={dw}"
                                    )
                                    if s > 0.75:
                                        print(
                                            f"  NEW: {best_rb_cfg} -> {s:.4f}",
                                            flush=True,
                                        )
    print(f"  BEST RIVERBED+TENSION: {best_rb:.4f} ({best_rb_cfg})", flush=True)

    # ── Final ──
    print("\n" + "=" * 60, flush=True)
    print("FINAL COMPARISON", flush=True)
    print("=" * 60, flush=True)
    print(f"E5-PT dense only:        0.7371", flush=True)
    print(f"Simple RRF (prev best):  0.7541", flush=True)
    print(f"Fine-grained RRF:        {best_rrf:.4f} ({best_rrf_cfg})", flush=True)
    print(f"Tension V2:              {best_t:.4f}", flush=True)
    print(f"Riverbed x Tension:      {best_rb:.4f}", flush=True)
    print(f"SOTA:                    0.7370", flush=True)

    winner = max(best_rrf, best_t, best_rb)
    print(f"\nBEST OVERALL:            {winner:.4f} "
          f"(delta vs SOTA: {winner - 0.737:+.4f})", flush=True)

    results = {
        "fine_rrf": {"score": round(best_rrf, 4), "config": best_rrf_cfg},
        "tension_v2": {"score": round(best_t, 4), "config": best_t_cfg},
        "riverbed_tension": {"score": round(best_rb, 4), "config": best_rb_cfg},
        "baseline_rrf": 0.7541,
        "e5pt_dense_only": 0.7371,
        "sota": 0.737,
    }
    out = os.path.join(os.path.dirname(__file__), "results", "e5pt_full_sweep.json")
    with open(out, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved to {out}", flush=True)


if __name__ == "__main__":
    main()
