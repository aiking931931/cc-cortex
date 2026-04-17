---
name: ablation-runner
description: >-
  Run benchmark/ablation experiments with RunPod GPU. Use when user says
  "跑分", "ablation", "閾值驗證", "α_t", "最佳預設", or when CBUA identifies
  unvalidated thresholds. Handles pod lifecycle, parallel execution, and
  result collection.
user-invocable: true
allowed-tools: Bash(python *) Bash(ssh *) Bash(runpodctl *) Read Grep Agent(*)
---

# Ablation Runner

I don't guess thresholds. I measure them.

## 觸發條件

- CBUA 規則中的數值閾值沒有實驗數據支撐
- 用戶說「跑分」「ablation」「驗證閾值」
- 紅隊質疑某閾值「拍腦袋」

## 固定流程（SOP）

### Step 0：看以前怎麼做的

**You MUST** 先讀：
- 交接_跑分 歷史 session
- `feedback_benchmark_run_sop.md`（跑前五步）
- `feedback_benchmark_infra_laws.md`（九鐵律）
- 思考還能不能優化 → CBUA 最佳解

### Step 1：定義實驗

**You MUST** 明確：
- **變數**：要測什麼（例如 α_t 切點）
- **候選值**：至少 4 個值
- **指標**：怎麼衡量好壞
- **數據集**：≥30 samples per group
- **官方格式**：checkpoint 用官方格式
- **第三方驗證**：有就用，沒有就算了

### Step 2：配置 RunPod

**You MUST** 遵守 Pod 鐵律：
- 選最快 GPU（速度至上，不省錢）
- Network Volume ≥50GB mount `/runpod-volume`
- `nvidia-smi` preflight 確認無碰撞
- 命名 = 任務+GPU（例如 `alpha-t-ablation-A100`）

### Step 3：寫跑分腳本

```
scripts/ablation_<name>.py
```

**You MUST** 包含：
- 參數化閾值（命令列 args）
- JSON 結果輸出（每組一行）
- 計時 + token 計數
- 可重複（固定 seed）
- **所有代碼存到 network volume**（可重現）

### Step 4：並行跑

- 多 pod 並行（每個跑不同參數組）
- 定時輪巡（`/loop` 或 background monitor）
- 跑完砍掉輪巡
- **Pod 跑完不 stop**（先拿資料再決定）

### Step 5：收集結果

**You MUST** 把所有重要資料拿回來：
- 結果 JSON
- 代碼（腳本 + config）— **一定要可重現**
- 日誌（stdout/stderr）
- checkpoint 檔案（如有）

存到本地 `benchmarks/<name>/results/`

### Step 6：分析 + 沉澱

- 找最佳值（最高指標 or Pareto）
- 寫 `feedback_<name>_optimal.md`
- 更新 CBUA/規則中的硬編碼值
- **總結**：什麼跑了、結果如何、下一步
- 確認 Pod 可以砍了再 terminate

## 按需讀取

| 場景 | 參考 |
|------|------|
| ZIQ α_t ablation | [references/alpha-t-design.md](references/alpha-t-design.md) |
| FieldRead 2500t 驗證 | [references/fieldread-breakeven.md](references/fieldread-breakeven.md) |
| RunPod 鐵律 | `memory/feedback_runpod_network_volume_mandatory.md` |
| 跑前五步 SOP | `memory/feedback_benchmark_run_sop.md` |
| 九鐵律 | `memory/feedback_benchmark_infra_laws.md` |
