---
name: ps
description: Use when the user wants to check Claude process status, running tasks, scheduled tasks, or instance locks. Triggers on keywords like "ps", "進程", "process".
user-invocable: true
disable-model-invocation: true
---

# /ps — Claude 進程狀態

顯示當前所有 Claude 相關進程的狀態。

## 執行流程
1. 查詢 Claude 相關進程：
   ```bash
   powershell -Command "Get-Process | Where-Object { $_.ProcessName -match 'claude|node' } | Select-Object Id, ProcessName, CPU, WS, StartTime | Format-Table -AutoSize"
   ```
2. 查詢排程任務狀態：
   ```bash
   powershell -Command "Get-ScheduledTask | Where-Object { $_.TaskName -match 'Claude' } | Select-Object TaskName, State, LastRunTime, LastTaskResult | Format-Table -AutoSize"
   ```
3. 檢查 instance lock：讀 `_AI_BRAIN/cognition_shared/instance_lock.json`
4. 整理輸出：進程列表 + 排程狀態 + 鎖定資訊

## 輸出格式
簡潔表格，標示活躍/閒置/異常狀態。
