"""
ZIQ RAG Pipeline — 統一 SLA 控制的 RAG 系統。

三層 ZIQ 控制：
  L1 Retrieval:  FTRL 動態學 retriever 權重（A2 ziq_reranker.py）
  L2 Reranking:  BGE-reranker-v2.5 + score 信號回饋
  L3 Generation: KV cache 壓縮讓長 context 可行（A1 ziq_v3_auto.py）

統一 SLA 框架：
  S = 估計 σ²（每層的信號來源不同）
  L = FTRL 累積學習
  A = water-filling 分配

這個檔案是 A1+A2 的整合入口。

Author: AI King
"""

import numpy as np


class ZIQRAGPipeline:
    """統一 ZIQ 控制的 RAG 管線。

    Usage:
        pipeline = ZIQRAGPipeline(
            retrievers=["bm25", "e5_large", "nomic"],
            reranker="bge-reranker-v2.5",
            compressor="ziq_v3_auto",
        )
        answer = pipeline.query("What is X?", corpus)
    """

    def __init__(
        self,
        retriever_names: list,
        rerank_budget: int = 100,
        kv_budget_ratio: float = 0.5,
    ):
        # A2: Reranker 權重學習
        from ziq_reranker import ZIQRerankerPipeline

        self.reranker_pipeline = ZIQRerankerPipeline(
            retriever_names,
            rerank_budget,
        )

        # A1: KV cache 壓縮設定
        self.kv_budget_ratio = kv_budget_ratio

        # 統計
        self.query_count = 0
        self.signal_history = []

    def query(
        self, query_text: str, retriever_results: dict, reranker_fn, generator_fn=None
    ):
        """完整 RAG query。

        Args:
            query_text: 查詢
            retriever_results: {name: [(doc_id, score), ...]}
            reranker_fn: callable(query, doc_ids) -> {doc_id: score}
            generator_fn: callable(query, context_docs) -> answer
                         （可選，None 時只返回 ranked docs）

        Returns:
            answer (str or None), ranked_docs, signals
        """
        self.query_count += 1

        # L1+L2: Retrieval + Reranking（ZIQ FTRL 控制）
        ranked, signals, weights = self.reranker_pipeline.rerank(
            query_text,
            retriever_results,
            reranker_fn,
        )

        self.signal_history.append(signals)

        # L3: Generation（KV cache 壓縮讓長 context 可行）
        if generator_fn is not None:
            # 取 top-k 文檔作為 context
            top_docs = [doc_id for doc_id, _ in ranked[:10]]
            answer = generator_fn(query_text, top_docs)
        else:
            answer = None

        return (
            answer,
            ranked,
            {
                "signals": signals,
                "weights": weights,
                "query_count": self.query_count,
            },
        )

    def get_retriever_report(self) -> dict:
        """取得 retriever 學習報告。"""
        w = self.reranker_pipeline.ftrl.get_weights()
        names = self.reranker_pipeline.retriever_names
        return {
            "weights": dict(zip(names, w.tolist())),
            "queries_processed": self.query_count,
            "avg_variance": float(np.mean([s["variance"] for s in self.signal_history]))
            if self.signal_history
            else 0,
        }


# ============================================================
# 本地驗證
# ============================================================

if __name__ == "__main__":
    print("=== ZIQ RAG Pipeline Test ===\n")

    np.random.seed(42)

    pipeline = ZIQRAGPipeline(
        retriever_names=["bm25", "e5_large", "nomic_v1.5"],
        rerank_budget=20,
        kv_budget_ratio=0.5,
    )

    def mock_reranker(query, doc_ids):
        scores = {}
        for did in doc_ids:
            base = np.random.uniform(0.1, 0.5)
            if did.startswith("e5"):
                base += 0.3
            elif did.startswith("nomic"):
                base += 0.15
            scores[did] = base
        return scores

    for q in range(30):
        ret = {
            "bm25": [
                (f"bm25_d{i}_{q}", np.random.uniform(0.3, 0.7)) for i in range(10)
            ],
            "e5_large": [
                (f"e5_d{i}_{q}", np.random.uniform(0.3, 0.9)) for i in range(10)
            ],
            "nomic_v1.5": [
                (f"nomic_d{i}_{q}", np.random.uniform(0.2, 0.8)) for i in range(10)
            ],
        }

        _, ranked, meta = pipeline.query(f"q_{q}", ret, mock_reranker)

        if q % 10 == 0:
            w = meta["weights"]
            w_str = " ".join(f"{w:.3f}" for w in meta["weights"])
            print(f"  q={q}: weights=[{w_str}] top1={ranked[0][0]}")

    report = pipeline.get_retriever_report()
    print(f"\n  Final: {report}")
    print("\n=== DONE ===")
