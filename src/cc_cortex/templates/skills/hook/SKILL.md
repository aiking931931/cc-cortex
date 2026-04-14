---
name: hook
description: Use when the user wants to control hook modes, enable/disable specific hook features, or check hook status. Triggers on keywords like "hook", "關閉hook", "開啟hook".
user-invocable: true
disable-model-invocation: true
---

# /hook — Hook 模式控制

根據參數 `$ARGUMENTS` 執行對應操作：

## 參數對照

| 參數 | 動作 |
|------|------|
| `off` | 全關（僅衝突偵測）→ 寫 `cc_config.json` `hook_mode: "off"` |
| `on` / `default` | 回預設 auto → `hook_mode: "auto"` |
| `min` | 最小模式 → `hook_mode: "minimal"` |
| `max` | 全開模式 → `hook_mode: "full"` |
| `auto` | 自動模式 → `hook_mode: "auto"` |
| `status` | 讀 `.claude/hooks/cc_config.json` 回報當前 hook_mode + overrides |
| `silent on` | 靜默模式 → 設 `CC_CORTEX_SILENT=1`。Hooks 正常跑，LLM 內部看到但不回話 |
| `silent off` | 關閉靜默 → 設 `CC_CORTEX_SILENT=0`。恢復正常 hook 顯示 |
| `<feature> on` | 單功能覆寫啟用 → `hook_overrides.<feature>: true` |
| `<feature> off` | 單功能覆寫停用 → `hook_overrides.<feature>: false` |
| `<feature> reset` | 移除覆寫 → 刪除 `hook_overrides.<feature>` |

## 設定檔路徑
`.claude/hooks/cc_config.json`

## 可控功能
awareness, scavenger, sentinel, agent_gate, token_warn, char_limit, rules_bloat, autonomy, wiredo

## 執行
1. 讀取 `cc_config.json`
2. 按參數修改對應欄位
3. 寫回檔案
4. 回報變更結果（一行摘要）

詳細規則：`_AI_BRAIN/00_System/rules/25-hooks.md`
