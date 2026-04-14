---
name: status
description: Use when the user wants to see project status dashboard, active projects overview, or task progress. Triggers on keywords like "status", "狀態", "儀表板", "dashboard".
user-invocable: true
disable-model-invocation: true
---

# /status — 全局狀態儀表板

顯示 TOP 3 活躍專案的即時狀態摘要。

## 執行流程

1. 讀取所有交接檔：`_AI_BRAIN/06_Handoffs/*/交接_*.md`（只讀主檔，不讀子檔 _A/_B/_C/_D）
2. 篩選 `status: active` 的專案
3. 從每個交接檔提取：
   - 專案名（從標題）
   - ✅ / ⬜ / ⏸ 計數
   - 最近一筆記錄（近期記錄第一條）
   - 下一步（第一個 ⬜ 項目）
4. 按 ⬜ 數量降序排列，取 TOP 3
5. 輸出儀表板

## 輸出格式

```
| # | 專案 | 進度 | 下一步 |
|---|------|------|--------|
| 1 | XXX  | ✅12 ⬜3 | 具體下一步 |
| 2 | YYY  | ✅8 ⬜2  | 具體下一步 |
| 3 | ZZZ  | ✅5 ⬜1  | 具體下一步 |

最近動態：
- [專案A] 03-10：完成了 XXX
```

## Context 使用量（每次必顯示）

在儀表板末尾加 Context 段，估算當前 session 負載：

```
⚡ Context 估算
  常駐規則：~XXX tokens（L0 + CLAUDE.md + MEMORY）
  已載入 L1：[列出已載入的 L1 檔名]
  交接檔讀取：~XXX tokens
  建議：🟢 充裕 / 🟡 過半 考慮 compact / 🔴 臨界 立即交接
```

估算方式：1 中文字 ≈ 2 tokens，1 英文詞 ≈ 1.3 tokens。
用 wc -c 讀實際檔案大小，除以 3 得 token 粗估。

## 參數

- `/status all`：顯示所有活躍專案（不限 TOP 3）
- `/status <專案名>`：顯示單一專案詳情
