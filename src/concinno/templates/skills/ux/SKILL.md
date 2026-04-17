---
name: ux
description: Use when the user wants to toggle streak UX celebrations on or off. Triggers on keywords like "ux", "streak", "慶祝".
user-invocable: true
disable-model-invocation: true
---

# /ux — 成癮 UX 控制

根據參數 `$ARGUMENTS` 控制 Streak UX 轉述功能。

## 參數

| 參數 | 動作 |
|------|------|
| `off` | 關閉 streak/celebration 轉述 → `cc_config.json` `ux_enabled: false` |
| `on` | 開啟 streak/celebration 轉述 → `ux_enabled: true` |

## 說明
Streak UX = 連擊計數 + 里程碑慶祝。已節流：每 5 次 milestone 才觸發，防習慣化忽略。

## 執行
1. 讀取 `.claude/hooks/cc_config.json`
2. 設定 `ux_enabled` 欄位
3. 寫回
4. 一行確認
