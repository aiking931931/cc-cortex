---
name: evolve
description: Use when the user wants to run evolution/upgrade cycles, process correction queues, or update knowledge base. Triggers on keywords like "evolve", "進化", "升級", "研究".
user-invocable: true
disable-model-invocation: true
---

# /evolve — 研究升級

自動切換到進化模式，執行研究升級流程。

## 執行流程
1. 讀取 `_AI_BRAIN/06_Handoffs/evolution/交接_進化.md` 取得當前狀態
2. 讀取 `_AI_BRAIN/01_Memory/evolution/learnings.json` 檢查累積學習
3. 讀取 `_AI_BRAIN/01_Memory/evolution/corrections-queue.jsonl` 檢查待處理修正
4. 按優先度執行：
   - 修正佇列中的待處理項目
   - 規則/Hook 升級評估
   - 知識庫更新
   - 記憶整合
5. 更新交接檔 + learnings.json
6. 報告變更摘要 + 下一步

## 注意
- 進化 ≠ 交接進化（後者含全局審查 5 步驟）
- 遵守三敗停損
- 沉澱鐵律：修正→kb→規則三步
