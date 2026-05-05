---
name: general-mode
description: 一般 LLM 使用習慣：context 跑到底 + 到閾值 auto-compact + memory file + 開新對話。適合除 AI King 以外的所有人使用（AI King 個人用 handoff mode）。Concinno 2.6+ PyPI ship default。
triggers:
  - general
  - 一般
  - normal
  - 預設
  - general-mode
  - 一般模式
  - standard
user-invocable: true
---

# /general-mode — 一般 LLM 使用習慣

> PyPI 公開版 Concinno 的 ship default。context 跑到底 → 到閾值 auto-compact → 開新對話時還原 memory file。這是除 AI King 外所有 Claude Code 使用者的 baseline。

> **You MUST** 尊重使用者的 `concinno config` 設定 — `mode=general` + `locale=en` 是 ship default，不要在使用者沒明確要求下切成 handoff
> **You MUST** `auto_compact` 到閾值自動跑；memory file 一律啟用
> **You MUST** handoff mode 只在使用者明確偏好（或 AI King 本人）時才啟用 — 透過 `concinno config set mode handoff` 切

## 模式對照

| 項目 | **general（ship default）** | handoff（AI King 個人偏好） |
|------|----------------------------|-------------------------|
| Context | 跑到底，觸發 auto-compact | 到閾值寫 handoff 檔，開新 session 讀回 |
| Memory file | 永遠啟用 | 永遠啟用 |
| 新對話 | 接 auto-compact 摘要 | 讀 `_AI_BRAIN/06_Handoffs/<project>/交接_*.md` |
| Locale default | `en` | `zh-TW`（user config 覆寫） |
| 適用族群 | 所有 PyPI 下載者 | AI King 本人 + 明確要 handoff 的人 |

## CLI 切換

```bash
concinno config               # show merged config + source
concinno config get mode      # => "general"（ship default）
concinno config set mode handoff   # 寫入 ~/.concinno/config.json
concinno config set locale zh-TW   # 同上
concinno config unset mode         # 回到 general ship default
```

## 執行

1. 讀 `$ARGUMENTS` — 若使用者要求切 handoff，呼叫 `concinno config set mode handoff`；否則確認 `general` 模式已生效
2. 顯示 `concinno config` 當前狀態 + 每 key 來自哪一層（env/project/user/default）
3. 提醒：PyPI 公開版 ship default 永遠是 `general` + `en`，任何個人偏好走 user config（`~/.concinno/config.json`）
4. AI King 工作區 `E:\Cursor` 既有偏好 `handoff + zh-TW` — 透過 user config 保留，不影響其他使用者

## 背景脈絡

Concinno 2.6.0 新增 `concinno.config` 四層 loader（env > project > user > default）。此 Skill 是 `competition-mode` 的繼任者 —— 把 Skill 定位從「賽道 SOP eager 注入」改成「一般 LLM 使用習慣說明 + 模式切換入口」。competition-mode 的 eager 注入需求由 `agent` Skill 統一 loop 取代（MEMORY #36d）。
