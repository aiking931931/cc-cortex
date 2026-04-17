# CC Cortex — Quickstart Guide

Get up and running with CC Cortex in under 5 minutes.

## Prerequisites

- Python 3.10 or later
- Claude Code installed and configured
- A project workspace (any directory where you use Claude Code)

## Step 1: Install

```bash
pip install concinno
```

Verify installation:

```bash
concinno --version
# concinno 0.3.0
```

## Step 2: Initialize

Navigate to your project workspace and run:

```bash
concinno init
```

This will:
1. Detect your Claude Code workspace (looks for `.claude/` directory)
2. Create a `cc_config.json` with sensible defaults
3. Install hook entry points into `.claude/hooks/`
4. Set the hook mode to `auto` (adapts to your usage pattern)

**Expected output:**
```
CC Cortex v0.3.0 — The Cognitive Layer for Claude Code

Detecting workspace...
Found Claude Code workspace at /home/user/my-project

Installing hooks:
  [+] hook_destruction_guard   ... installed
  [+] hook_sentinel            ... installed
  [+] hook_memory              ... installed
  [+] hook_instance_lock       ... installed
  [+] hook_evolution           ... installed
  [+] hook_handoff             ... installed
  [+] hook_token_warning       ... installed

Created cc_config.json with default settings (mode: auto)

Done! CC Cortex is now active.
```

## Step 3: Enable Modules

By default, `concinno init` enables the most commonly used modules. To see all available modules:

```bash
concinno list
```

To enable a specific module:

```bash
concinno enable hook_task_pool
```

To disable a module:

```bash
concinno disable hook_rules_bloat
```

You can also configure modules via `cc_config.json`:

```jsonc
{
  "hook_mode": "auto",
  "hook_overrides": {
    "task_pool": true,       // force enable
    "rules_bloat": false     // force disable
  }
}
```

## Step 4: Verify

Run the status dashboard to confirm everything is working:

```bash
concinno status
```

You should see a table listing all active modules, their trigger counts, and the current hook mode.

**Quick smoke test:** Open Claude Code in your workspace and make a tool call. CC Cortex hooks will run automatically. Check the status dashboard again — you should see trigger counts incrementing.

## Step 5: Customize (Optional)

### Change Hook Mode

CC Cortex supports five modes that control which modules are active:

| Mode | Description |
|------|-------------|
| `auto` | **(Default)** Adapts automatically — `balanced` for single sessions, `full` for multi-instance, `minimal` at high token usage |
| `off` | All hooks disabled except conflict detection |
| `minimal` | Only critical safety hooks (sentinel, destruction guard) |
| `balanced` | Safety + memory + basic optimization |
| `full` | All modules active |

Set mode via CLI:

```bash
concinno mode balanced
```

Or edit `cc_config.json`:

```jsonc
{
  "hook_mode": "balanced"
}
```

### Configure Thresholds

Fine-tune behavior in `cc_config.json`:

```jsonc
{
  "thresholds": {
    "max_handoff_lines": 80,      // max lines per handoff file
    "token_warn_at": 60000,       // token count to trigger warning
    "sentinel_loop_count": 3,     // same-tool repetitions before alert
    "max_knowledge_entries": 200   // knowledge base size cap
  }
}
```

See [examples/cc_config_example.jsonc](../examples/cc_config_example.jsonc) for all available options.

## What's Next?

- **Write custom hooks** — See [examples/custom_hooks.py](../examples/custom_hooks.py)
- **Read the whitepaper** — See [docs/whitepaper.md](whitepaper.md)
- **Contribute** — See [CONTRIBUTING.md](../CONTRIBUTING.md)
- **Multi-instance setup** — Enable `hook_instance_lock` and `hook_task_pool` for parallel Claude Code sessions

## Troubleshooting

### Hooks not firing

1. Make sure `.claude/hooks/` contains the CC Cortex entry points (re-run `concinno init` if needed)
2. Check that `cc_config.json` exists in your workspace root
3. Verify hook mode is not `off`: `concinno status`

### Permission errors

CC Cortex hooks are Python scripts that need execute permission:

```bash
chmod +x .claude/hooks/on-pre-tool.py
chmod +x .claude/hooks/on-post-tool.py
```

### Module not loading

Check `concinno status` for error indicators. Common causes:
- Python version < 3.10 (f-string features required)
- Corrupted `cc_config.json` (delete and re-run `concinno init`)
