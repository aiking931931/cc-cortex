---
name: schedule
description: Use when the user wants to manage scheduled tasks, enable/disable schedules, change timing, or run tasks immediately. Triggers on keywords like "schedule", "排程", "排程管理".
user-invocable: true
disable-model-invocation: true
---

# /schedule — 排程管理

根據參數 `$ARGUMENTS` 操控排程任務。

## 無參數
顯示所有排程狀態（讀 `.claude/hooks/schedule_config.json` + Windows Task Scheduler 查詢）

## 帶參數

格式：`/schedule <名> <操作>`

| 操作 | 說明 |
|------|------|
| `on` | 啟用排程 → `schedule_config.json` enabled: true |
| `off` | 停用排程 → enabled: false |
| `time <HH:MM>` | 修改執行時間 |
| `freq <格式>` | 修改頻率（daily/every2d/every3d/weekly/15min） |
| `run` | 立即執行一次（呼叫 `scheduled_launcher.ps1`） |

## 排程名稱對照

| 簡寫 | 任務名 | 頻率 | 時間 |
|------|--------|------|------|
| `reflection` | Claude-SelfReflection | 每天 | 09:00 |
| `scavenger` | Claude-Scavenger | 每 3 天 | 12:00 |
| `research` | Claude-WeeklyResearch | 每週日 | 10:00 |
| `guard` | Claude-ProcessGuard | 每 15 分 | — |
| `agent` | ClaudeAutoAgent | 每天 | 06:00 |

## 執行
1. 讀 `schedule_config.json`
2. 按參數修改
3. 呼叫 `register-schedules.ps1` 註冊變更到 Windows Task Scheduler
4. 回報結果

設定檔：`.claude/hooks/schedule_config.json`
