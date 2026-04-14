"""BEIR SciFact Full Ablation - AI King STAR v3.5

Runs on Kaggle T4 GPU. Results saved to /kaggle/working/
"""
# ruff: noqa: T201, S603, S607

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

# Install deps before importing them
subprocess.check_call(  # noqa: S603
    [sys.executable, "-m", "pip", "install", "-q",
     "beir", "sentence-transformers", "chromadb", "rank-bm25"],
)

import chromadb  # noqa: E402
import torch  # noqa: E402
from beir import util  # noqa: E402
from beir.datasets.data_loader import GenericDataLoader  # noqa: E402
from rank_bm25 import BM25Okapi  # noqa: E402
from sentence_transformers import CrossEncoder, SentenceTransformer  # noqa: E402

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def _pick_device() -> str:
    if not torch.cuda.is_available():
        return "cpu"
    try:
        # Smoke-test: allocate a tiny tensor to verify CUDA actually works
        torch.zeros(1, device="cuda")
        return "cuda"
    except Exception:  # noqa: BLE001
        print("CUDA available but incompatible (sm mismatch) - falling back to CPU")
        return "cpu"


DEVICE = _pick_device()
print(f"Device: {DEVICE}")
if DEVICE == "cuda":
    print(f"GPU: {torch.cuda.get_device_name(0)}")
    vram = torch.cuda.get_device_properties(0).total_memory / 1e9
    print(f"VRAM: {vram:.1f} GB")

DATASET = "scifact"
URL = "https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/scifact.zip"
OUTPUT_DIR = "/kaggle/working"

data_path = util.download_and_unzip(URL, "datasets")
corpus, queries, qrels = GenericDataLoader(data_folder=data_path).load(split="test")
print(f"Corpus: {len(corpus)} docs | Queries: {len(queries)}")


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
def ndcg_at_k(ranked_ids: list[str], relevant: dict, k: int = 10) -> float:
    dcg = sum(
        relevant.get(did, 0) / math.log2(i + 2)
        for i, did in enumerate(ranked_ids[:k])
    )
    ideal = sorted(relevant.values(), reverse=True)[:k]
    idcg = sum(r / math.log2(i + 2) for i, r in enumerate(ideal))
    return dcg / idcg if idcg > 0 else 0.0


def recall_at_k(ranked_ids: list[str], relevant: dict, k: int = 100) -> float:
    if not relevant:
        return 0.0
    return sum(1 for did in ranked_ids[:k] if did in relevant) / len(relevant)


def precision_at_k(ranked_ids: list[str], relevant: dict, k: int = 10) -> float:
    if k == 0:
        return 0.0
    return sum(1 for did in ranked_ids[:k] if did in relevant) / k


def mrr(ranked_ids: list[str], relevant: dict) -> float:
    for i, did in enumerate(ranked_ids):
        if did in relevant:
            return 1.0 / (i + 1)
    return 0.0


def rrf(result_lists: list[list], k: int = 60) -> list[tuple]:
    scores: dict[str, float] = defaultdict(float)
    for results in result_lists:
        for rank, (did, _) in enumerate(results):
            scores[did] += 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


# ---------------------------------------------------------------------------
# BM25
# ---------------------------------------------------------------------------
def _tokenize(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


doc_ids = list(corpus.keys())
doc_texts = [
    f"{corpus[did].get('title', '')} {corpus[did].get('text', '')}".strip()
    for did in doc_ids
]
tokenized_docs = [_tokenize(t) for t in doc_texts]

t0 = time.time()
bm25 = BM25Okapi(tokenized_docs)
print(f"BM25 built in {time.time() - t0:.1f}s ({len(doc_ids)} docs)")


def search_bm25(query: str, top_k: int = 100) -> list[tuple[str, float]]:
    scores = bm25.get_scores(_tokenize(query))
    top_idx = scores.argsort()[-top_k:][::-1]
    return [(doc_ids[i], float(scores[i])) for i in top_idx if scores[i] > 0]


# ---------------------------------------------------------------------------
# Dense Search
# ---------------------------------------------------------------------------
class DenseSearch:
    def __init__(
        self,
        model_name: str,
        corpus_dict: dict,
        d_ids: list[str],
        cache_tag: str = "",
    ) -> None:
        self.model_name = model_name
        self.doc_ids = d_ids
        tag = cache_tag or model_name.replace("/", "_")
        db_path = f"{OUTPUT_DIR}/dense_cache/{tag}"
        os.makedirs(db_path, exist_ok=True)

        print(f"Loading {model_name}...")
        self.model = SentenceTransformer(model_name, device=DEVICE)

        self.client = chromadb.PersistentClient(path=db_path)
        try:
            col = self.client.get_collection("dense")
            if col.count() >= len(corpus_dict):
                self.collection = col
                print(f"  Cached ({col.count()} vectors)")
                return
        except Exception:  # noqa: BLE001
            pass

        try:
            self.client.delete_collection("dense")
        except Exception:  # noqa: BLE001
            pass
        self.collection = self.client.create_collection(
            "dense", metadata={"hnsw:space": "cosine"},
        )

        texts = [
            f"{corpus_dict[did].get('title', '')} "
            f"{corpus_dict[did].get('text', '')}".strip()
            for did in d_ids
        ]

        t_start = time.time()
        batch_size = 256
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            batch_ids = d_ids[i : i + batch_size]
            embs = self.model.encode(
                batch, show_progress_bar=False, batch_size=64,
            ).tolist()
            self.collection.add(ids=batch_ids, embeddings=embs, documents=batch)
            done = min(i + batch_size, len(texts))
            print(f"  Embedded {done}/{len(texts)}", end="\r")
        print(f"\n  Done in {time.time() - t_start:.1f}s")

    def search(self, query: str, top_k: int = 100) -> list[tuple[str, float]]:
        emb = self.model.encode([query], show_progress_bar=False).tolist()
        results = self.collection.query(
            query_embeddings=emb,
            n_results=min(top_k, self.collection.count()),
        )
        out: list[tuple[str, float]] = []
        if results and results["ids"]:
            for j, did in enumerate(results["ids"][0]):
                dist = results["distances"][0][j] if results.get("distances") else 1.0
                out.append((did, 1.0 - dist))
        return out


# ---------------------------------------------------------------------------
# Reranker
# ---------------------------------------------------------------------------
class RerankerWrap:
    def __init__(self, model_name: str) -> None:
        print(f"Loading reranker: {model_name}...")
        self.model = CrossEncoder(model_name, max_length=512, device=DEVICE)
        self.name = model_name
        print("  Reranker ready")

    def rerank(
        self,
        query: str,
        candidates: list[tuple],
        corpus_dict: dict,
        top_k: int = 100,
    ) -> list[tuple[str, float]]:
        if not candidates:
            return []
        rerank_k = min(50, len(candidates))
        top_cands = candidates[:rerank_k]
        pairs = [
            (
                query,
                corpus_dict[did].get("title", "")
                + " "
                + corpus_dict[did].get("text", ""),
            )
            for did, _ in top_cands
            if did in corpus_dict
        ]
        if not pairs:
            return candidates[:top_k]
        scores = self.model.predict(pairs)
        reranked = sorted(
            zip([did for did, _ in top_cands[: len(pairs)]], scores),
            key=lambda x: x[1],
            reverse=True,
        )
        reranked_ids = {did for did, _ in reranked}
        rest = [
            (did, s) for did, s in candidates[rerank_k:]
            if did not in reranked_ids
        ]
        return [(did, float(s)) for did, s in reranked] + rest


# ---------------------------------------------------------------------------
# Benchmark Runner
# ---------------------------------------------------------------------------
@dataclass
class BenchConfig:
    name: str
    dense_models: list = field(default_factory=list)
    use_bm25: bool = True
    reranker: Any = None
    top_k: int = 100


def run_benchmark(
    config: BenchConfig,
    queries_dict: dict,
    qrels_dict: dict,
    corpus_dict: dict,
) -> dict:
    print(f"\n{'=' * 60}")
    print(f"Running: {config.name}")
    print(f"{'=' * 60}")

    all_ndcg: list[float] = []
    all_recall: list[float] = []
    all_prec: list[float] = []
    all_mrr: list[float] = []
    total_time = 0.0

    for qi, (qid, query_text) in enumerate(queries_dict.items()):
        relevant = {
            did: rel for did, rel in qrels_dict.get(qid, {}).items() if rel > 0
        }
        t_q = time.time()

        result_lists: list[list] = []
        if config.use_bm25:
            result_lists.append(search_bm25(query_text, config.top_k))
        for ds in config.dense_models:
            result_lists.append(ds.search(query_text, config.top_k))

        if len(result_lists) > 1:
            fused = rrf(result_lists)
        elif result_lists:
            fused = result_lists[0]
        else:
            fused = []

        if config.reranker and fused:
            fused = config.reranker.rerank(
                query_text, fused, corpus_dict, config.top_k,
            )

        query_time = time.time() - t_q
        total_time += query_time

        ranked = [did for did, _ in fused[: config.top_k]]
        all_ndcg.append(ndcg_at_k(ranked, relevant))
        all_recall.append(recall_at_k(ranked, relevant))
        all_prec.append(precision_at_k(ranked, relevant))
        all_mrr.append(mrr(ranked, relevant))

        if (qi + 1) % 100 == 0 or qi == 0:
            lat = query_time * 1000
            print(f"  [{qi + 1}/{len(queries_dict)}] nDCG@10={all_ndcg[-1]:.3f} ({lat:.0f}ms)")

    n = len(queries_dict)
    results = {
        "name": config.name,
        "nDCG@10": round(sum(all_ndcg) / n, 4),
        "Recall@100": round(sum(all_recall) / n, 4),
        "P@10": round(sum(all_prec) / n, 4),
        "MRR": round(sum(all_mrr) / n, 4),
        "Avg_ms": round(total_time / n * 1000, 1),
    }

    print(f"\n  nDCG@10:    {results['nDCG@10']}")
    print(f"  Recall@100: {results['Recall@100']}")
    print(f"  Latency:    {results['Avg_ms']} ms/query")
    return results


# ============================================================
# PHASE 1: Build Dense Indices
# ============================================================
print("\n" + "=" * 60)
print("PHASE 1: Building Dense Indices")
print("=" * 60)

DENSE_MODELS = {
    "e5-large": "intfloat/e5-large-v2",
    "bge-m3": "BAAI/bge-m3",
    "bge-large": "BAAI/bge-large-en-v1.5",
}

dense_indices: dict[str, DenseSearch] = {}
for _tag, _model in DENSE_MODELS.items():
    print(f"\n--- Building {_tag} ---")
    dense_indices[_tag] = DenseSearch(_model, corpus, doc_ids, cache_tag=_tag)

print(f"\n{len(dense_indices)} dense indices ready")

# ============================================================
# PHASE 2: Load Rerankers
# ============================================================
print("\n" + "=" * 60)
print("PHASE 2: Loading Rerankers")
print("=" * 60)

RERANKER_MODELS = {
    "ms-marco-L6": "cross-encoder/ms-marco-MiniLM-L-6-v2",
    "bge-reranker": "BAAI/bge-reranker-v2-m3",
}

rerankers: dict[str, RerankerWrap] = {}
for _tag, _model in RERANKER_MODELS.items():
    rerankers[_tag] = RerankerWrap(_model)

print(f"\n{len(rerankers)} rerankers ready")

# ============================================================
# PHASE 3: Full Ablation Matrix
# ============================================================
print("\n" + "=" * 60)
print("PHASE 3: Full Ablation Matrix")
print("=" * 60)

ALL_RESULTS: list[dict] = []

# Baseline
ALL_RESULTS.append(
    run_benchmark(BenchConfig(name="BM25_only", use_bm25=True), queries, qrels, corpus),
)

# Single Dense
for _tag, _ds in dense_indices.items():
    ALL_RESULTS.append(run_benchmark(
        BenchConfig(name=f"Dense:{_tag}", dense_models=[_ds], use_bm25=False),
        queries, qrels, corpus,
    ))

# Hybrid: BM25 + each Dense
for _tag, _ds in dense_indices.items():
    ALL_RESULTS.append(run_benchmark(
        BenchConfig(name=f"Hybrid:BM25+{_tag}", dense_models=[_ds], use_bm25=True),
        queries, qrels, corpus,
    ))

# Triple: BM25 + 2 Dense
for _a, _b in [("e5-large", "bge-m3"), ("e5-large", "bge-large"), ("bge-m3", "bge-large")]:
    ALL_RESULTS.append(run_benchmark(
        BenchConfig(
            name=f"Triple:BM25+{_a}+{_b}",
            dense_models=[dense_indices[_a], dense_indices[_b]],
            use_bm25=True,
        ),
        queries, qrels, corpus,
    ))

# Quad: BM25 + ALL 3 Dense
ALL_RESULTS.append(run_benchmark(
    BenchConfig(
        name="Quad:BM25+e5+bge-m3+bge-large",
        dense_models=list(dense_indices.values()),
        use_bm25=True,
    ),
    queries, qrels, corpus,
))

# Reranker combos
for _rtag, _reranker in rerankers.items():
    for _dtag in ["e5-large", "bge-large"]:
        ALL_RESULTS.append(run_benchmark(
            BenchConfig(
                name=f"Hybrid:BM25+{_dtag}+{_rtag}",
                dense_models=[dense_indices[_dtag]],
                use_bm25=True,
                reranker=_reranker,
            ),
            queries, qrels, corpus,
        ))

    ALL_RESULTS.append(run_benchmark(
        BenchConfig(
            name=f"Triple:BM25+e5+bge-m3+{_rtag}",
            dense_models=[dense_indices["e5-large"], dense_indices["bge-m3"]],
            use_bm25=True,
            reranker=_reranker,
        ),
        queries, qrels, corpus,
    ))

    ALL_RESULTS.append(run_benchmark(
        BenchConfig(
            name=f"Quad:BM25+all3+{_rtag}",
            dense_models=list(dense_indices.values()),
            use_bm25=True,
            reranker=_reranker,
        ),
        queries, qrels, corpus,
    ))

print(f"\nTotal: {len(ALL_RESULTS)} configurations tested")

# ============================================================
# Results Table
# ============================================================
sorted_results = sorted(ALL_RESULTS, key=lambda x: x["nDCG@10"], reverse=True)

print("\n" + "=" * 90)
print("BEIR SciFact - AI King STAR v3.5 Full Ablation")
print("=" * 90)
header = f"{'Config':<45} {'nDCG@10':>8} {'R@100':>8} {'P@10':>7} {'MRR':>7} {'ms':>7}"
print(header)
print("-" * 90)

for r in sorted_results:
    flag = "*" if r["nDCG@10"] >= 0.737 else " "
    row = (
        f"{flag} {r['name']:<43} {r['nDCG@10']:>8.4f} "
        f"{r['Recall@100']:>8.4f} {r['P@10']:>7.4f} "
        f"{r['MRR']:>7.4f} {r['Avg_ms']:>7.1f}"
    )
    print(row)

print("-" * 90)
print("SOTA Reference: E5-PT_base = 0.737 | BM25 baseline = 0.665")
print(f"Total configs tested: {len(ALL_RESULTS)}")

# ============================================================
# Save
# ============================================================
gpu_name = torch.cuda.get_device_name(0) if DEVICE == "cuda" else "CPU"
output = {
    "team": "AI King",
    "system": "CCC STAR v3.5",
    "dataset": "scifact",
    "timestamp": time.strftime("%Y-%m-%d %H:%M"),
    "device": DEVICE,
    "gpu": gpu_name,
    "sota_reference": {"E5-PT_base": 0.737, "BM25": 0.665},
    "results": sorted_results,
}

json_path = f"{OUTPUT_DIR}/beir_scifact_full_ablation.json"
with open(json_path, "w") as f:
    json.dump(output, f, indent=2)

md_lines = [
    f"# BEIR SciFact Full Ablation - AI King STAR v3.5 | {time.strftime('%Y-%m-%d')}",
    "",
    f"Device: {gpu_name} | Configs: {len(ALL_RESULTS)}",
    "",
    "| # | Config | nDCG@10 | Recall@100 | P@10 | MRR | ms/q |",
    "|---|--------|---------|------------|------|-----|------|",
]
for i, r in enumerate(sorted_results, 1):
    bold = "**" if r["nDCG@10"] >= 0.737 else ""
    md_lines.append(
        f"| {i} | {bold}{r['name']}{bold} | {r['nDCG@10']:.4f} "
        f"| {r['Recall@100']:.4f} | {r['P@10']:.4f} "
        f"| {r['MRR']:.4f} | {r['Avg_ms']:.1f} |",
    )
md_lines.extend(["", "SOTA: E5-PT_base = 0.737 | BM25 = 0.665"])

md_path = f"{OUTPUT_DIR}/beir_scifact_full_ablation.md"
with open(md_path, "w") as f:
    f.write("\n".join(md_lines))

print(f"\nSaved: {json_path}")
print(f"Saved: {md_path}")
