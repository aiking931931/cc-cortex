"""
Colab GPU reranker test — copy this to Google Colab and run.

Setup cell:
    !pip install beir sentence-transformers rank_bm25 numpy

Upload required:
    - Upload datasets/scifact/ folder (or let BEIR auto-download)
    - Upload .cache_e5pt_base_embs.npz
"""
from __future__ import annotations

import json
import os
import time
from collections import defaultdict

import numpy as np
from beir.datasets.data_loader import GenericDataLoader
from rank_bm25 import BM25Okapi
from sentence_transformers import CrossEncoder, SentenceTransformer

# ── BM25 helpers ──────────────────────────────────────────


def build_bm25_with_ids(corpus):
    """Build BM25 index, return (bm25, doc_ids)."""
    doc_ids = list(corpus.keys())
    texts = []
    for did in doc_ids:
        doc = corpus[did]
        text = doc.get("title", "") + " " + doc.get("text", "")
        texts.append(text.strip().lower().split())
    bm25 = BM25Okapi(texts)
    return bm25, doc_ids


def search_bm25_by_ids(bm25, doc_ids, query, top_k=100):
    """Search BM25, return [(doc_id, score), ...]."""
    tokens = query.lower().split()
    scores = bm25.get_scores(tokens)
    idx = np.argsort(scores)[::-1][:top_k]
    return [(doc_ids[i], float(scores[i])) for i in idx]


# ── Metrics ───────────────────────────────────────────────


def ndcg_at_k(ranked, relevant, k=10):
    """Compute nDCG@k."""
    dcg = 0.0
    for i, did in enumerate(ranked[:k]):
        if did in relevant:
            dcg += relevant[did] / np.log2(i + 2)
    ideal = sorted(relevant.values(), reverse=True)[:k]
    idcg = sum(r / np.log2(i + 2) for i, r in enumerate(ideal))
    return dcg / idcg if idcg > 0 else 0.0


def recall_at_k(ranked, relevant, k=100):
    """Compute Recall@k."""
    if not relevant:
        return 0.0
    found = sum(1 for d in ranked[:k] if d in relevant)
    return found / len(relevant)


# ── Fusion ────────────────────────────────────────────────


def fuse_rrf(bm25_r, dense_r, k=5, bw=1.0, dw=1.2):
    """Best simple RRF config."""
    scores = defaultdict(float)
    for rank, (did, _) in enumerate(bm25_r):
        scores[did] += bw / (k + rank + 1)
    for rank, (did, _) in enumerate(dense_r):
        scores[did] += dw / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def riverbed_tension(
    bm25_r,
    dense_r,
    k_low=3,
    k_high=10,
    top_n=20,
    boost_max=1.2,
    score_w=0.5,
    bw=0.8,
    dw=1.4,
):
    """Riverbed x Tension fusion — world #1 config."""
    b_set = {did for did, _ in bm25_r[:top_n]}
    d_set = {did for did, _ in dense_r[:top_n]}
    union = b_set | d_set
    agreement = len(b_set & d_set) / len(union) if union else 0.0
    tension = 1.0 - agreement
    adaptive_k = max(1, int(k_low + (k_high - k_low) * tension))
    boost = 1.0 + (boost_max - 1.0) * agreement

    rrf_scores = defaultdict(float)
    presence = defaultdict(int)
    for rank, (did, _) in enumerate(bm25_r):
        rrf_scores[did] += bw / (adaptive_k + rank + 1)
        presence[did] += 1
    for rank, (did, _) in enumerate(dense_r):
        rrf_scores[did] += dw / (adaptive_k + rank + 1)
        presence[did] += 1
    for did in rrf_scores:
        if presence[did] >= 2:
            rrf_scores[did] *= boost

    def norm(results):
        if not results:
            return {}
        vals = [s for _, s in results]
        mn, mx = min(vals), max(vals)
        rng = mx - mn if mx > mn else 1.0
        return {did: (s - mn) / rng for did, s in results}

    b_n = norm(bm25_r)
    d_n = norm(dense_r)
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


# ── Main ──────────────────────────────────────────────────


def main():
    # Download SciFact
    from beir import util

    base = "datasets"
    os.makedirs(base, exist_ok=True)
    url = "https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/scifact.zip"
    data_path = util.download_and_unzip(url, base)
    corpus, queries, qrels = GenericDataLoader(data_path).load(split="test")
    print(f"Loaded: {len(corpus)} docs, {len(queries)} queries")

    # BM25
    bm25, doc_ids = build_bm25_with_ids(corpus)
    print("BM25 ready")

    # Dense: E5-PT_base
    device = "cuda" if __import__("torch").cuda.is_available() else "cpu"
    print(f"Using device: {device}")
    model = SentenceTransformer("intfloat/e5-base-unsupervised", device=device)

    # Encode passages
    cache_path = os.path.join(base, ".cache_e5pt_base_embs.npz")
    if os.path.exists(cache_path):
        passage_embs = np.load(cache_path)["embs"]
        print(f"Loaded cached embeddings: {passage_embs.shape}")
    else:
        print("Encoding passages...")
        doc_id_list = list(corpus.keys())
        texts = []
        for did in doc_id_list:
            doc = corpus[did]
            texts.append("passage: " + doc.get("title", "") + " " + doc.get("text", ""))
        passage_embs = model.encode(texts, normalize_embeddings=True, show_progress_bar=True)
        np.savez_compressed(cache_path, embs=passage_embs)
        print(f"Saved embeddings: {passage_embs.shape}")

    doc_id_list = list(corpus.keys())

    def e5pt_search(qt, top_k=100):
        q = model.encode(["query: " + qt], normalize_embeddings=True)
        sims = (passage_embs @ q.T).flatten()
        idx = np.argsort(sims)[::-1][:top_k]
        return [(doc_id_list[i], float(sims[i])) for i in idx]

    # Reranker
    print("Loading bge-reranker-v2-m3...")
    reranker = CrossEncoder("BAAI/bge-reranker-v2-m3", max_length=512, device=device)
    print("Reranker loaded!")

    # Cache BM25 + Dense results
    print("Caching search results...")
    cached = {}
    for qid, qt in queries.items():
        rel = {d: r for d, r in qrels.get(qid, {}).items() if r > 0}
        cached[qid] = {
            "bm25": search_bm25_by_ids(bm25, doc_ids, qt, top_k=100),
            "dense": e5pt_search(qt, top_k=100),
            "rel": rel,
            "text": qt,
        }
    print(f"Cached {len(cached)} queries")

    # ── Test reranker on top of Riverbed×Tension ──
    results = {}
    for rerank_top in [10, 20, 30, 50]:
        print(f"\n=== Reranker top-{rerank_top} on Riverbed×Tension ===", flush=True)
        all_ndcg = []
        all_recall = []
        t0 = time.time()

        for qi, (qid, entry) in enumerate(cached.items()):
            fused = riverbed_tension(entry["bm25"], entry["dense"])

            # Rerank top candidates
            top_cands = fused[:rerank_top]
            rest = fused[rerank_top:]

            pairs = []
            valid_dids = []
            for did, _ in top_cands:
                if did in corpus:
                    doc = corpus[did]
                    text = doc.get("title", "") + " " + doc.get("text", "")
                    pairs.append([entry["text"], text.strip()])
                    valid_dids.append(did)

            if pairs:
                ce_scores = reranker.predict(pairs).tolist()
                reranked = sorted(
                    zip(valid_dids, ce_scores),
                    key=lambda x: x[1],
                    reverse=True,
                )
                reranked_set = {d for d, _ in reranked}
                remaining = [(d, s) for d, s in rest if d not in reranked_set]
                final = [(d, float(s)) for d, s in reranked] + remaining
            else:
                final = list(fused)

            ranked = [d for d, _ in final[:100]]
            all_ndcg.append(ndcg_at_k(ranked, entry["rel"], k=10))
            all_recall.append(recall_at_k(ranked, entry["rel"], k=100))

            if (qi + 1) % 50 == 0:
                elapsed = time.time() - t0
                avg_n = sum(all_ndcg) / len(all_ndcg)
                print(f"  [{qi+1}/300] nDCG@10={avg_n:.4f} ({elapsed:.0f}s)", flush=True)

        avg_ndcg = sum(all_ndcg) / len(all_ndcg)
        avg_recall = sum(all_recall) / len(all_recall)
        total = time.time() - t0
        print(f"  RESULT: nDCG@10={avg_ndcg:.4f} R@100={avg_recall:.4f}")
        print(f"  Time: {total:.0f}s")
        print(f"  vs baseline 0.7578: {avg_ndcg - 0.7578:+.4f}")

        results[f"rerank_top_{rerank_top}"] = {
            "ndcg10": round(avg_ndcg, 4),
            "recall100": round(avg_recall, 4),
            "time_s": round(total, 1),
        }

    # Also test: reranker on simple RRF
    print("\n=== Reranker top-20 on Simple RRF ===", flush=True)
    all_ndcg = []
    t0 = time.time()
    for qid, entry in cached.items():
        fused = fuse_rrf(entry["bm25"], entry["dense"])
        top_cands = fused[:20]
        rest = fused[20:]
        pairs = []
        valid_dids = []
        for did, _ in top_cands:
            if did in corpus:
                doc = corpus[did]
                text = doc.get("title", "") + " " + doc.get("text", "")
                pairs.append([entry["text"], text.strip()])
                valid_dids.append(did)
        if pairs:
            ce_scores = reranker.predict(pairs).tolist()
            reranked = sorted(zip(valid_dids, ce_scores), key=lambda x: x[1], reverse=True)
            reranked_set = {d for d, _ in reranked}
            remaining = [(d, s) for d, s in rest if d not in reranked_set]
            final = [(d, float(s)) for d, s in reranked] + remaining
        else:
            final = list(fused)
        ranked = [d for d, _ in final[:100]]
        all_ndcg.append(ndcg_at_k(ranked, entry["rel"], k=10))
    avg_ndcg = sum(all_ndcg) / len(all_ndcg)
    print(f"  RESULT: nDCG@10={avg_ndcg:.4f}")
    results["rrf_rerank_top_20"] = {"ndcg10": round(avg_ndcg, 4)}

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print("Baseline Riverbed×Tension (no reranker): 0.7578")
    print("Baseline Simple RRF (no reranker):       0.7541")
    for key, val in results.items():
        print(f"{key}: nDCG@10={val['ndcg10']}")

    # Save
    os.makedirs("results", exist_ok=True)
    with open("results/reranker_results.json", "w") as f:
        json.dump(results, f, indent=2)
    print("\nSaved to results/reranker_results.json")


if __name__ == "__main__":
    main()
