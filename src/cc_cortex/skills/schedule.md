---
name: cortex-schedule
description: Manage scheduled Claude Code tasks (self-reflection, scavenger, weekly-research). Triggers on "schedule", "排程".
user-invocable: true
allowed-tools: Bash, Read, Edit
---

# /cortex-schedule — Scheduled Task Manager

Manage recurring Claude Code background tasks.

## Usage

Based on `$ARGUMENTS`:

| Argument | Action |
|----------|--------|
| `list` | Show all tasks and their last run times |
| `run <task>` | Run a task immediately |
| `install <task>` | Install recurring schedule (Task Scheduler / launchd / cron) |
| `uninstall <task>` | Remove recurring schedule |
| `enable <task>` | Enable a disabled task |
| `disable <task>` | Disable a task |
| `status` | Show schedule status across all platforms |

## Available Tasks

- `self-reflection` — Daily self-review (budget $0.50, interval 20h)
- `scavenger` — Cleanup patrol (budget $1.00, interval 68h)
- `weekly-research` — Research cycle (budget $1.50, interval 160h)

## Execution

```bash
# Run task
python -m cc_cortex.scheduler <task_name>

# Install schedule
python -m cc_cortex.scheduler --install <task_name> --interval 15

# Uninstall
python -m cc_cortex.scheduler --uninstall <task_name>
```

Config: `schedule_config.json` (model, budget, timeout, enabled per task).
