---
name: sync
description: Git 同步模式控制 — 決定改完後「審（PR review）」還是「直接過（push）」。預設直接過。
triggers:
  - sync
  - 同步
  - 審查模式
  - 直接過
  - push模式
user-invocable: true
disable-model-invocation: true
---

# /sync — Git 同步模式

I ship fast by default. Review is a deliberate choice, not a default tax.

**You MUST** 在 push 前確認當前模式（review 或 pass）。
**You MUST** review 模式下開 PR 並附摘要，不可直接 merge。
**You MUST** pass 模式下直接 push 到當前 branch，不開 PR。

## 用法

```
/sync           — 顯示當前模式
/sync pass      — 直接 push（預設）
/sync review    — 開 PR 讓另一台審
```

## 決策樹

```
改完要同步 → 當前模式？
  ├─ pass   → git add + commit + push（自動，不問）
  └─ review → git checkout -b feat/<描述>
             → git add + commit + push -u
             → gh pr create --title "<摘要>" --body "<變更清單>"
             → 輸出 PR URL 給用戶
```

## 模式存儲

寫入 `.claude/collab-state.json`：

```json
{
  "sync_mode": "pass",
  "branch_mode": "split",
  "updated_at": "<ISO 8601>"
}
```

## pass 模式流程

```bash
git add <changed files>
git commit -m "<conventional commit>"
git push
```

完成後輸出：`✅ 已 push 到 <branch>，另一台 git pull 即可接手`

## review 模式流程

```bash
git checkout -b feat/<task-name>
git add <changed files>
git commit -m "<conventional commit>"
git push -u origin feat/<task-name>
gh pr create --title "<摘要>" --body "$(cat <<'EOF'
## 變更
<bullet points>

## 驗證
<已做的驗證>
EOF
)"
```

完成後輸出：`🔍 PR 已開：<URL>，等另一台審`

## 與 /ship 的關係

`/sync` 是輕量日常同步。`/ship` 是正式發布管線（含 pre-flight、version bump）。
日常協作用 `/sync`，正式交付用 `/ship`。
