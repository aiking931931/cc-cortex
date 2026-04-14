# Benchmark 完整索引

> 所有實驗的代碼、結果、重現方式。防止「分數有但代碼丟了」。

## 核心成果

| 成果 | 分數 | 代碼 | 結果 JSON | 重現指令 |
| ---- | ---- | ---- | --------- | -------- |
| SciFact #1 | 0.7578 | `sweep_e5pt.py` | `e5pt_full_sweep.json` | 見下方 |
| 通用固定參數 | E5:0.7573 Mini:0.7063 | `confluence_rag.py` | `universal_sweep.json` | 見下方 |
| Auto v2 | 0.7552 | `confluence_rag.py` | `confluence_test.json` | 見下方 |
| 跨資料集 | SciFact+NFCorpus+FiQA | `cross_dataset_validate.py` | `cross_dataset_validation.json` | 見下方 |

## 重現指令

```bash
cd e:/Cursor/projects/cc-cortex/benchmarks

# 世界第一（E5-PT 專用參數）
BEIR_DEVICE=cpu python -u sweep_e5pt.py

# 三模式比較（Auto/Fixed/Simple）
BEIR_DEVICE=cpu python -u test_confluence.py

# 跨資料集驗證（一組參數打三個資料集）
BEIR_DEVICE=cpu python -u cross_dataset_validate.py

# bge-reranker GPU 測試（HF Router API）
BEIR_DEVICE=cpu python -u test_reranker_hf_v2.py
```

## 參數速查

### FIXED_UNIVERSAL（通用，專利核心）

```python
k_low=2, k_high=5, top_n=20, boost_max=1.2,
score_w=0.5, bw=0.8, dw=1.0
```

### FIXED_E5PT（E5-PT 專用，SciFact 最高分）

```python
k_low=3, k_high=10, top_n=20, boost_max=1.2,
score_w=0.5, bw=0.8, dw=1.4
```

### Simple RRF（baseline）

```python
k=5, bw=1.0, dw=1.2
```

## 代碼索引

### 產品代碼

| 檔案 | 用途 | 狀態 |
| ---- | ---- | ---- |
| `confluence_rag.py` | 產品級融合引擎，3 模式 | ✅ committed |
| `beir_runner.py` | 底層 BEIR runner | ✅ committed |
| `cross_dataset_validate.py` | 跨資料集驗證+checkpoint | ✅ committed |

### Sweep 腳本

| 檔案 | 用途 | 狀態 |
| ---- | ---- | ---- |
| `sweep_e5pt.py` | E5-PT 完整 sweep | ✅ committed |
| `sweep_fast.py` | bge-m3 快速 sweep | ✅ committed |
| `sweep_fusion.py` | 早期 fusion sweep | ✅ committed |
| `sweep_all_strategies.py` | 所有策略 sweep | ✅ committed |

### 測試腳本

| 檔案 | 用途 | 結論 | 狀態 |
| ---- | ---- | ---- | ---- |
| `test_confluence.py` | Auto/Fixed/RRF | Fixed > Auto > RRF | ✅ |
| `test_reranker_hf_v2.py` | bge-reranker GPU | ❌ 幫倒忙 | ✅ |
| `test_reranker_cpu.py` | MiniLM reranker | ❌ 幫倒忙 | ✅ |
| `test_reranker_hf.py` | HF API v1 | ❌ 410 Gone | ✅ |
| `test_reranker.py` | bge-reranker CPU | ❌ 卡死 | ✅ |
| `colab_reranker.py` | Colab GPU 版 | 未跑 | ✅ |
| `find_0.7267.py` | 找丟失的 0.7267 | ❌ 不可重現 | ✅ |
| `hunt_0.7267_v2.py` | ChromaDB 快取測試 | ❌ 不可重現 | ✅ |

### 其他

| 檔案 | 用途 |
| ---- | ---- |
| `gaia_runner.py` | GAIA benchmark runner |
| `run_remaining.py` | 批次跑剩餘實驗 |

## 結果索引

### 關鍵結果（results/）

| 檔案 | 內容 |
| ---- | ---- |
| `e5pt_full_sweep.json` | 世界第一 0.7578 完整結果 |
| `universal_sweep.json` | 通用參數雙模型 sweep |
| `confluence_test.json` | Auto/Fixed/Simple 比較 |
| `bge_reranker_gpu_results.json` | GPU reranker 完整結果 |
| `e5pt_base_results.json` | E5-PT 初始結果 |
| `sweep_fast_20260326_1700.json` | bge-m3 sweep 86K 配置 |
| `cross_dataset_validation.json` | 跨資料集（SciFact+NFCorpus） |
| `cross_dataset_fiqa.json` | FiQA 跨資料集 ✅（Colab T4 GPU） |

### 保存紀錄（results/preserved/）

| 檔案 | 內容 |
| ---- | ---- |
| `RECORDS.md` | 所有紀錄追溯鏈 |
| `WORLD_2ND_0.7267_*.json` | 丟失的 0.7267 數據 |

### 歷史結果（beir_scifact_*.json）

早期 beir_runner.py 跑的各種配置結果，按時間戳命名。
注意：這些用的是 bge-m3 + ChromaDB，不是 E5-PT。

## Embedding Cache（datasets/）

| 檔案 | 模型 | 大小 |
| ---- | ---- | ---- |
| `.cache_e5pt_base_embs.npz` | E5-PT 110M | 5183×768 |
| `.cache_minilm_embs.npz` | MiniLM 22M | 5183×384 |
| `.cache_scifact_e5pt_base_embs.npz` | 同上（跨資料集版） | 5183×768 |
| `.cache_nfcorpus_e5pt_base_embs.npz` | E5-PT NFCorpus | 3633×768 |
| `.cache_fiqa_e5pt_base_embs.npz` | E5-PT FiQA ✅ | 57638×768 |

## 已排除的方向

| 方向 | 結論 | 證據 |
| ---- | ---- | ---- |
| Reranker (任何) | 全部幫倒忙 | `bge_reranker_gpu_results.json` |
| Auto 動態調參 | 難穩定贏 RRF | `confluence_test.json` |
| 0.7267 重現 | 代碼丟失 | `find_0.7267.py` 輸出 |

## Git Commits

| Commit | 內容 |
| ------ | ---- |
| `177a42d` | 所有核心代碼+結果 |
| `54e2f21` | RECORDS.md 完整版 |
| `37a5887` | checkpoint 機制 |
