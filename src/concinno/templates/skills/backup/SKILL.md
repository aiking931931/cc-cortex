---
name: backup
description: Use when the user wants to backup rules/skills/config, rollback to a previous backup, list backups, or prune old ones. Triggers on keywords like "backup", "備份", "回滾", "rollback".
user-invocable: true
disable-model-invocation: true
---

# /backup — 備份管理

根據 `$ARGUMENTS` 執行對應操作。使用 `concinno.BackupManager`。

## 參數對照

| 參數 | 動作 |
|------|------|
| （空）/ `status` | 顯示所有備份狀態 |
| `create <描述>` | 建立備份（自動清理舊版，保留最新 2 份） |
| `list` | 列出所有備份，最新在前 |
| `rollback [目標]` | 回滾到指定備份（預設最新），回滾前自動備份當前狀態 |
| `prune [N]` | 只保留最新 N 份（預設 2），刪其餘 |
| `rules` | 備份 `.claude/rules/` |
| `skills` | 備份 `.claude/skills/` |

## 執行流程

1. 解析 `$ARGUMENTS`，決定 scope（rules/skills/指定目錄）和動作
2. 用 Python 呼叫 BackupManager：

```python
from concinno.backup_manager import BackupManager

# 範例：備份 rules
mgr = BackupManager(base_dir=".claude/rules", scope="rules", keep=2)

# create
entry = mgr.create("pre-refactor")  # → backup_rules_20260321-1830_pre-refactor/

# list
backups = mgr.list_backups()  # sorted newest first

# rollback
result = mgr.rollback()  # restore latest, auto-backup current state first

# prune
deleted = mgr.prune(keep=2)  # delete all but newest 2

# status
info = mgr.status()  # summary dict
```

3. 報告結果：備份名稱、時間、保留數量、刪除了哪些

## 命名格式

`backup_<scope>_<YYYYMMDD-HHMM>_<description>`

- scope：rules / skills / config 等
- 時間：UTC+8
- description：安全化（特殊字元→連字號）

## 安全機制

- 回滾前自動備份當前狀態（`pre-rollback`）
- 每次備份後自動清理，只留最新 2 份
- 備份目錄以 `backup_` 開頭，不會被誤備份
