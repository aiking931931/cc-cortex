"""
Confluence Fusion MTEB wrapper (SearchProtocol).
（融合檢索 MTEB 官方 wrapper — 完全控制 index + search）

Implements SearchProtocol: index(corpus) + search(queries).
BM25 + Dense → riverbed fusion, evaluated by MTEB.

Usage:
  python confluence_mteb.py --datasets SciFact
  python confluence_mteb.py --all
"""

import argparse
import os

import bm25s
import numpy as np
import torch
from sentence_transformers import SentenceTransformer

try:
    import mteb
except ImportError:
    raise ImportError("pip install mteb")


class ConfluenceFusion:
    """MTEB SearchProtocol: BM25 + Dense → riverbed fusion.
    （MTEB 搜尋協議：BM25 + Dense → 河床融合）
    """

    def __init__(self, model_name, device="cuda",
                 bw=0.8, dw=1.0,
                 prefix_q="", prefix_d=""):
        self.model = SentenceTransformer(model_name, device=device)
        self.device = device
        self.bw = bw
        self.dw = dw
        self.prefix_q = prefix_q
        self.prefix_d = prefix_d
        self._corpus_embs = None
        self._corpus_ids = None
        self._bm25 = None

    def index(self, corpus, *, task_metadata=None,
              hf_split=None, hf_subset=None,
              encode_kwargs=None, num_proc=None):
        """Build BM25 + Dense index from corpus."""
        # Extract texts and ids
        if hasattr(corpus, "__iter__"):
            docs = list(corpus)
        else:
            docs = corpus

        self._corpus_ids = []
        texts_for_embed = []
        texts_for_bm25 = []

        for doc in docs:
            did = doc.get("_id", doc.get("id", ""))
            title = doc.get("title", "")
            text = doc.get("text", "")
            full_text = f"{title} {text}".strip()
            self._corpus_ids.append(str(did))
            texts_for_embed.append(
                f"{self.prefix_d}{full_text}".strip()
            )
            texts_for_bm25.append(full_text)

        # Dense encoding
        print(f"  Encoding {len(texts_for_embed)} docs...")
        self._corpus_embs = self.model.encode(
            texts_for_embed,
            normalize_embeddings=True,
            batch_size=256,
            show_progress_bar=True,
        )

        # BM25 indexing
        print("  Building BM25...")
        tokens = bm25s.tokenize(texts_for_bm25)
        self._bm25 = bm25s.BM25()
        self._bm25.index(tokens)
        print(f"  Index ready: {len(self._corpus_ids)} docs")

    def search(self, queries, *, task_metadata=None,
               hf_split=None, hf_subset=None,
               top_k=100, encode_kwargs=None,
               top_ranked=None, num_proc=None):
        """BM25 + Dense → riverbed fusion search.
        Returns dict[query_id, dict[doc_id, score]].
        """
        if hasattr(queries, "__iter__"):
            q_list = list(queries)
        else:
            q_list = queries

        qids = []
        q_texts = []
        for q in q_list:
            qid = q.get("_id", q.get("id", ""))
            text = q.get("text", "")
            qids.append(str(qid))
            q_texts.append(f"{self.prefix_q}{text}".strip())

        # Encode queries
        print(f"  Encoding {len(q_texts)} queries...")
        q_embs = self.model.encode(
            q_texts,
            normalize_embeddings=True,
            batch_size=256,
        )

        # Dense retrieval (GPU if large)
        print("  Dense retrieval...")
        if (
            torch.cuda.is_available()
            and self._corpus_embs.shape[0] * q_embs.shape[0] > 1e8
        ):
            ct = torch.from_numpy(self._corpus_embs).cuda()
            qt = torch.from_numpy(q_embs).cuda()
            all_sims = (ct @ qt.T).cpu().numpy()
            del ct, qt
            torch.cuda.empty_cache()
        else:
            all_sims = self._corpus_embs @ q_embs.T

        # Fusion per query
        print(f"  Fusing {len(qids)} queries...")
        results = {}
        for qi, qid in enumerate(qids):
            # Dense scores
            d_sims = all_sims[:, qi]
            d_idx = np.argsort(d_sims)[::-1][:top_k * 2]
            d_vals = d_sims[d_idx]
            d_min = d_vals[-1] if len(d_vals) > 0 else 0
            d_max = d_vals[0] if len(d_vals) > 0 else 1
            d_rng = d_max - d_min if d_max > d_min else 1.0

            # BM25 scores
            raw_q = q_list[qi].get("text", "")
            tokens = bm25s.tokenize([raw_q])
            bm25_r, bm25_s = self._bm25.retrieve(
                tokens, corpus=self._corpus_ids, k=top_k * 2,
            )
            bm25_dict = {}
            for i in range(len(bm25_r[0])):
                did = str(bm25_r[0, i])
                sc = float(bm25_s[0, i])
                if sc > 0:
                    bm25_dict[did] = sc

            if bm25_dict:
                b_vals = list(bm25_dict.values())
                b_min = min(b_vals)
                b_max = max(b_vals)
                b_rng = b_max - b_min if b_max > b_min else 1.0
            else:
                b_min = 0
                b_rng = 1.0

            # Riverbed fusion（河床融合）
            tw = self.bw + self.dw
            fused = {}

            # From dense
            for rank_i, idx in enumerate(d_idx[:top_k * 2]):
                did = self._corpus_ids[idx]
                d_norm = (d_sims[idx] - d_min) / d_rng
                b_norm = (
                    (bm25_dict.get(did, 0) - b_min) / b_rng
                    if did in bm25_dict else 0
                )
                fused[did] = (
                    self.bw * b_norm + self.dw * d_norm
                ) / tw

            # From BM25 only (not in dense top)
            for did, sc in bm25_dict.items():
                if did not in fused:
                    b_norm = (sc - b_min) / b_rng
                    idx = self._corpus_ids.index(did)
                    d_norm = (d_sims[idx] - d_min) / d_rng
                    fused[did] = (
                        self.bw * b_norm + self.dw * d_norm
                    ) / tw

            # Sort and take top_k
            sorted_fused = sorted(
                fused.items(),
                key=lambda x: x[1],
                reverse=True,
            )[:top_k]

            results[qid] = {
                did: float(score)
                for did, score in sorted_fused
            }

        print(f"  Search done: {len(results)} queries")
        return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--datasets", nargs="+", default=["SciFact"],
    )
    parser.add_argument("--all", action="store_true")
    parser.add_argument(
        "--model", default="intfloat/e5-base-unsupervised",
    )
    parser.add_argument("--prefix-q", default="query: ")
    parser.add_argument("--prefix-d", default="passage: ")
    parser.add_argument(
        "--output-dir", default="./mteb_confluence_results",
    )
    args = parser.parse_args()

    if args.all:
        datasets = [
            "SciFact", "TREC-COVID", "NFCorpus",
            "ArguAna", "SCIDOCS", "FiQA2018",
            "Touche2020", "QuoraRetrieval",
            "CQADupstackRetrieval",
        ]
    else:
        datasets = args.datasets

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Device: {device}")
    print(f"Model: {args.model}")

    cf = ConfluenceFusion(
        args.model, device=device,
        prefix_q=args.prefix_q,
        prefix_d=args.prefix_d,
    )

    os.makedirs(args.output_dir, exist_ok=True)

    for ds_name in datasets:
        sep = "=" * 50
        print(f"\n{sep}\n{ds_name}\n{sep}")

        tasks = mteb.get_tasks(
            tasks=[ds_name],
            task_types=["Retrieval"],
        )
        if not tasks:
            print(f"  Task {ds_name} not found")
            continue

        results = mteb.evaluate(
            cf, tasks=tasks,
            output_folder=args.output_dir,
            eval_splits=["test"],
        )

        for task_result in results:
            scores = task_result.scores
            if "test" in scores:
                for score_set in scores["test"]:
                    ndcg10 = score_set.get("ndcg_at_10", "?")
                    print(f"  FUSION nDCG@10 = {ndcg10}")

    print("\nDone.")


if __name__ == "__main__":
    main()
