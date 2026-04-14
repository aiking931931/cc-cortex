---
name: tidy
description: Use when the user wants to clean up files, compress handoffs, rotate logs, or run the scavenger cleanup. Triggers on keywords like "tidy", "清理", "清道夫", "scavenger".
user-invocable: true
disable-model-invocation: true
---

# /tidy — 全面清理（清道夫模式）

執行 Scavenger 全面清理流程。

## 執行流程（按優先度）
1. **知識庫清理** — `_AI_BRAIN/01_Memory/knowledge_base.md`：合併重複、刪 90 天無引用條目，目標 ≤200 條
2. **交接檔壓縮** — `_AI_BRAIN/06_Handoffs/*/交接_*.md`：合併重複 session 記錄。不刪 ✅/⏸/⬜ 任務
3. **MEMORY.md 衛生** — >180 行則合併/刪過期
4. **修正佇列輪轉** — `corrections-queue.jsonl` >50 行則保留最新 20
5. **暫存/備份清理** — `_temp_*`, `*.bak`, `*backup*`
6. **對話檔清理** — `~/.claude/projects/E--Cursor/` 保留最新 50 個
7. **日誌輪轉** — `~/.claude/logs/*.log` >500 行保留最新 200
8. **規則去重** — 比對 `.claude/rules/` vs `_AI_BRAIN/00_System/rules/`，只標記不刪除

## 鐵律
- 刪只刪重複和過期，**永不刪功能性內容**
- 扭曲比冗餘更糟，不確定就保留
- 不修改源碼檔案（只動 .md/.json/.jsonl/.log/.txt）
- 最多 10 次工具呼叫，策略性批量操作

詳細：`.claude/hooks/scavenger-prompt.txt`
