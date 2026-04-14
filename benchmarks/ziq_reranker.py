"""
ZIQ Reranker — FTRL 動態學習 reranker 信號的 retriever 加權。

三條路線：
  R1: BGE-reranker-v2.5 單獨 rerank（基準線）
  R2: BGE score → FTRL 動態學各 retriever 的 rerank 信號（ZIQ 創新）
  R3: BGE + Jina 雙 reranker → ZIQ 融合（多樣性增益）

R2 核心閉環：
  retriever → 初排 → reranker → score → FTRL 更新 retriever 權重 → 下個 query

Author: AI King
"""

import numpy as np

# ============================================================
# 1. Reranker Score 信號提取
# ============================================================


def extract_reranker_signals(scores: np.ndarray):
    """從 reranker score 分佈提取 ZIQ 信號。

    Args:
        scores: (n_docs,) — reranker 給每個文檔的分數

    Returns:
        dict with:
          - variance: 分數方差（高=區分度好）
          - gap: top-1 vs top-2 分差（高=置信度高）
          - mean: 平均分（高=整體相關性強）
          - entropy: 分數分佈的熵（低=聚焦，高=模糊）
    """
    if len(scores) < 2:
        return {"variance": 0, "gap": 0, "mean": float(scores[0]), "entropy": 0}

    sorted_s = np.sort(scores)[::-1]
    variance = float(np.var(scores))
    gap = float(sorted_s[0] - sorted_s[1])
    mean_s = float(np.mean(scores))

    # 歸一化為機率分佈計算熵
    shifted = scores - scores.min() + 1e-10
    probs = shifted / shifted.sum()
    probs_safe = np.clip(probs, 1e-12, 1.0)
    entropy = float(-np.sum(probs * np.log2(probs_safe)))

    return {"variance": variance, "gap": gap, "mean": mean_s, "entropy": entropy}


# ============================================================
# 2. FTRL Retriever 權重學習（ZIQ SLA 框架）
# ============================================================


class FTRLRetrieverWeights:
    """FTRL 動態學習各 retriever 的權重。

    根據 reranker score 反饋，自動調整哪個 retriever 在什麼類型的
    query 上最可靠。

    SLA 映射：
      S (Sense): reranker score 分佈 → σ²
      L (Learn): FTRL 累積 loss → 更新 λ
      A (Allocate): λ → retriever 權重 w_k

    Loss 定義：
      retriever k 的 loss = -(reranker score of its top-1 doc)
      直覺：如果 retriever k 找的 top-1 文檔被 reranker 打高分
      → loss 低 → 權重增加
    """

    def __init__(self, n_retrievers: int, eta: float = 0.5):
        self.n = n_retrievers
        self.eta = eta
        self.cum_reward = np.zeros(n_retrievers)  # 累積獎勵
        self.t = 0

    def get_weights(self) -> np.ndarray:
        """取得當前 retriever 權重（Hedge/EXP3 softmax）。"""
        # 自適應學習率
        lr = self.eta / max(1.0, np.sqrt(self.t))
        logits = lr * self.cum_reward
        logits -= logits.max()
        w = np.exp(logits)
        return w / w.sum()

    def update(self, retriever_scores: dict):
        """根據 reranker 反饋更新權重。

        Args:
            retriever_scores: {retriever_name: reranker_score_of_its_top1}
                例如 {"bm25": 0.3, "e5": 0.8, "nomic": 0.7}
        """
        self.t += 1
        names = sorted(retriever_scores.keys())
        scores = np.array([retriever_scores[n] for n in names])

        # 獎勵 = score（直接累積，不取相對值）
        self.cum_reward += scores

    def get_allocation(self, total_budget: int) -> dict:
        """根據權重分配 rerank 預算（每個 retriever 分幾個 slot）。

        高權重 retriever → 更多候選進入 rerank。
        """
        w = self.get_weights()
        budgets = np.round(w * total_budget).astype(int)
        # 確保總量正確
        diff = total_budget - budgets.sum()
        if diff != 0:
            idx = np.argmax(w) if diff > 0 else np.argmin(w)
            budgets[idx] += diff
        return dict(zip(range(self.n), budgets))


# ============================================================
# 3. ZIQ Reranker Pipeline
# ============================================================


class ZIQRerankerPipeline:
    """完整 ZIQ Reranker 管線。

    流程：
    1. 多個 retriever 各自取 top-k
    2. ZIQ FTRL 分配 rerank 預算
    3. Reranker 打分
    4. ZIQ 融合（FTRL 權重 × reranker score）
    5. FTRL 反饋更新

    Usage:
        pipeline = ZIQRerankerPipeline(retriever_names=["bm25", "e5", "nomic"])
        # 每個 query:
        results = pipeline.rerank(query, retriever_results, reranker_fn)
    """

    def __init__(self, retriever_names: list, rerank_budget: int = 100):
        self.retriever_names = retriever_names
        self.n = len(retriever_names)
        self.ftrl = FTRLRetrieverWeights(self.n)
        self.rerank_budget = rerank_budget

    def rerank(self, query: str, retriever_results: dict, reranker_fn):
        """執行一次完整的 ZIQ rerank。

        Args:
            query: 查詢字串
            retriever_results: {name: [(doc_id, score), ...]}
            reranker_fn: callable(query, doc_ids) -> {doc_id: score}

        Returns:
            ranked_results: [(doc_id, final_score), ...] sorted desc
        """
        weights = self.ftrl.get_weights()

        # 1. 合併所有 retriever 的候選（去重）
        all_docs = {}
        for i, name in enumerate(self.retriever_names):
            docs = retriever_results.get(name, [])
            for doc_id, ret_score in docs:
                if doc_id not in all_docs:
                    all_docs[doc_id] = {
                        "retriever_scores": {},
                        "weight_sum": 0,
                    }
                all_docs[doc_id]["retriever_scores"][name] = ret_score
                all_docs[doc_id]["weight_sum"] += weights[i] * ret_score

        # 2. 選 top rerank_budget 進入 reranker
        candidates = sorted(
            all_docs.items(),
            key=lambda x: x[1]["weight_sum"],
            reverse=True,
        )[:self.rerank_budget]

        doc_ids = [c[0] for c in candidates]

        # 3. Reranker 打分
        reranker_scores = reranker_fn(query, doc_ids)

        # 4. 最終分數 = reranker_score（reranker 是最終裁判）
        ranked = sorted(
            reranker_scores.items(),
            key=lambda x: x[1],
            reverse=True,
        )

        # 5. FTRL 反饋：每個 retriever 的 top-1 文檔得到多少 reranker 分
        feedback = {}
        for i, name in enumerate(self.retriever_names):
            docs = retriever_results.get(name, [])
            if docs:
                top1_id = docs[0][0]
                feedback[name] = reranker_scores.get(top1_id, 0)
            else:
                feedback[name] = 0
        self.ftrl.update(feedback)

        # 6. 提取信號（供外部使用）
        scores_arr = np.array(list(reranker_scores.values()))
        signals = extract_reranker_signals(scores_arr)

        return ranked, signals, weights


# ============================================================
# 4. 本地驗證
# ============================================================

if __name__ == "__main__":
    print("=== ZIQ Reranker Pipeline Test ===\n")

    np.random.seed(42)

    # 模擬 3 個 retriever
    names = ["bm25", "e5_large", "nomic_v1.5"]
    pipeline = ZIQRerankerPipeline(names, rerank_budget=20)

    # 模擬 reranker（e5 找的文檔通常更好）
    def mock_reranker(query, doc_ids):
        scores = {}
        for did in doc_ids:
            base = np.random.uniform(0.1, 0.5)
            if did.startswith("e5"):
                base += 0.3  # e5 的文檔更好
            elif did.startswith("nomic"):
                base += 0.15
            scores[did] = base
        return scores

    # 跑 50 個 query
    for q in range(50):
        ret_results = {
            "bm25": [(f"bm25_d{i}_{q}", np.random.uniform(0.3, 0.7))
                     for i in range(10)],
            "e5_large": [(f"e5_d{i}_{q}", np.random.uniform(0.3, 0.9))
                         for i in range(10)],
            "nomic_v1.5": [(f"nomic_d{i}_{q}", np.random.uniform(0.2, 0.8))
                           for i in range(10)],
        }

        ranked, signals, weights = pipeline.rerank(
            f"query_{q}", ret_results, mock_reranker,
        )

        if q % 10 == 0:
            w_str = " ".join(f"{n}={w:.3f}" for n, w in zip(names, weights))
            print(f"  q={q:3d}: weights=[{w_str}]")
            print(f"         signals: var={signals['variance']:.4f} "
                  f"gap={signals['gap']:.4f}")

    # 最終權重
    final_w = pipeline.ftrl.get_weights()
    print("\n  Final weights:")
    for n, w in zip(names, final_w):
        print(f"    {n}: {w:.4f}")

    print("\n  Expected: e5_large > nomic > bm25")
    print("=== DONE ===")
