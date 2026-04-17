---
name: collab
description: 多機協作分支模式 — 決定「改同一個 branch」還是「各開 branch」。預設各開。
triggers:
  - collab
  - 協作
  - 分支模式
  - 同branch
  - 各開branch
user-invocable: true
disable-model-invocation: true
---

# /collab — 多機協作分支模式

Two machines, clear lanes. Merge conflicts are preventable, not inevitable.

**You MUST** 在開始工作前確認分支模式（same 或 split）。
**You MUST** split 模式下自動建 `collab/<task>` 分支，完成後 merge 回主線。
**You MUST** same 模式下工作前先 `git pull`，避免衝突。

## 用法

```
/collab          — 顯示當前模式
/collab split    — 各開 branch（預設）
/collab same     — 同 branch 工作
```

## 決策樹

```
開始新任務 → 當前模式？
  ├─ split → git checkout -b collab/<task-name>
  │         → 做事 → commit → push
  │         → 完成後 merge 回主線（或開 PR 看 /sync 模式）
  └─ same  → git pull（先拉最新）
            → 做事 → commit → push
            → ⚠️ 衝突風險：同檔案同時改會 conflict
```

## 模式存儲

共用 `.claude/collab-state.json`（與 /sync 共用）：

```json
{
  "sync_mode": "pass",
  "branch_mode": "split",
  "updated_at": "<ISO 8601>"
}
```

## split 模式（預設，推薦）

```bash
# 開始任務
git checkout -b collab/<task-name>

# 做完
git add <files> && git commit -m "<msg>"
git push -u origin collab/<task-name>

# 合併（依 /sync 模式）
# pass → git checkout main && git merge collab/<task-name> && git push
# review → gh pr create
```

**優點**：零衝突、可並行、可回滾
**適用**：兩台改不同功能

## same 模式

```bash
# 開始前必拉
git pull

# 做完立刻推
git add <files> && git commit -m "<msg>" && git push
```

**優點**：簡單、即時同步
**風險**：同檔案同時改 = merge conflict
**適用**：一台改完另一台接手（序列工作）

## 四情境對應

| 情境 | /collab | /sync | 流程 |
|------|---------|-------|------|
| A 改完 B 審 | split | review | A push → PR → B review → merge |
| A 改完 B 直接接手 | same | pass | A push → B pull → 繼續 |
| 兩台同時改不同東西 | split | pass | 各開 branch → 各自 push → merge |
| 兩台同時改同一個檔 | split | review | 各開 branch → PR → resolve conflict |
