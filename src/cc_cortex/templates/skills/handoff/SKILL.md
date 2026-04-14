---
name: handoff
description: Use when the user wants to switch handoff modes (save-token, phase, full). Triggers on keywords like "交接模式", "handoff mode", "handoff".
user-invocable: true
disable-model-invocation: true
---

# /handoff — 交接模式切換

根據參數 `$ARGUMENTS` 執行對應操作：

## 模式說明

| 模式 | 參數 | 行為 |
|------|------|------|
| **省 Token** | `save` / `省token` / `1` | 80K 提醒、140K 擋 Agent、160K 強制交接 |
| **跑階段** | `phase` / `跑階段` / `2` | **預設**。做完當前清單才交接。150K 提醒、180K 安全上限 |
| **跑完整** | `full` / `跑完整` / `3` | 整個專案跑到結束。無 token 限制 |
| 查看 | 空 / `status` | 顯示當前模式 |

## 執行流程

1. 解析 `$ARGUMENTS`
2. 若無參數或 `status`：讀取 `.claude/hooks/cc_config.json` 的 `handoff_mode`，顯示當前模式
3. 若有模式參數：

### 參數對照表
```
save / 省token / 1  →  save-token
phase / 跑階段 / 2  →  phase
full / 跑完整 / 3   →  full
```

4. 修改 `.claude/hooks/cc_config.json` 的 `handoff_mode` 欄位
5. 顯示確認：

```
🔄 交接模式：<中文名>
  └ <一行說明>
```

## 何時該用哪個？（情境指南）

### 省 Token（save-token）
- 探索性工作、不確定方向、先試試看
- 多個小任務互不相關（改完一個就能交接）
- 剛開始接觸陌生 codebase
- **訊號**：「先看看」「試一下」「不確定能不能」

### 跑階段（phase）— 預設
- 有明確任務清單，要做完一輪
- 一般功能開發、bug 修復批次
- 任務之間有關聯，交接會丟失脈絡
- **訊號**：「把這些做完」「清完待辦」「做完這批」

### 跑完整（full）
- 深度除錯（根因很深，需要完整思路鏈）
- 整個專案自動化流程（CI/CD、部署、驗證一條龍）
- 大規模重構（拆了要裝回去才有意義）
- 翻譯/內容批量處理（量大中斷會不一致）
- **訊號**：「跑到完」「做完整個」「一次搞定」「不要中斷」

## 跑完整（full）的跳過規則

full 模式下遇到以下情況 **不停下來問，直接跳過**，繼續做能做的，最後統一報告：

| 阻擋類型 | 範例 | 處理 |
|----------|------|------|
| **花費 > 1000 TWD** | API 付費、購買服務 | 跳過，記錄預估金額 |
| **公開上架/發布** | PyPI publish、npm publish、App Store | 跳過，記錄準備度 |
| **多語言翻譯** | 除 zh-TW + en 外的語言 | 跳過，靠 AI 翻譯腳本處理 |
| **用戶明確說不准** | 任何被用戶擋過的項目 | 跳過，記錄原因 |
| **不可逆高風險** | 刪 production 資料、force push main | 跳過，記錄操作內容 |

**報告格式**（session 結束時）：
```
⏸ 跳過項目（N 項）：
1. [項目] — 原因：[為什麼跳過] | 準備度：[X%]
2. ...
→ 需要用戶決定後才能推進
```

## 自動偵測

用戶說「省 token」→ save | 「跑到完」「做完整個」→ full | 「做完這批」「清完待辦」→ phase
