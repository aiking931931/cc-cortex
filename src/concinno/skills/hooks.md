---
name: cortex-hooks
description: Control concinno hook modes and features. Triggers on "cortex hook", "hook mode".
user-invocable: true
allowed-tools: Read, Edit, Write
---

# /cortex-hooks — Hook Mode Control

Configure concinno hook behavior.

## Usage

Based on `$ARGUMENTS`:

| Argument | Action |
|----------|--------|
| `off` | Minimal mode (conflict detection only) |
| `on` / `auto` | Default auto mode |
| `min` | Minimal hooks |
| `max` | All hooks active |
| `status` | Show current mode + feature overrides |
| `<feature> on` | Enable specific feature |
| `<feature> off` | Disable specific feature |

## Features

- `destruction_guard` — R0-R4 risk protection on destructive commands
- `session_lock` — Multi-instance coordination
- `token_warn` — Token usage warnings
- `streak_ux` — Milestone celebrations
- `lint` — PostToolUse code quality checks

## Config Path

`cc_config.json` in hooks directory. Keys: `hook_mode`, `hook_overrides.<feature>`.
