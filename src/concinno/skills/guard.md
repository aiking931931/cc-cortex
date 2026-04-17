---
name: guard
description: Run process guard to scan and clean up orphaned/stale Claude processes. Triggers on "guard", "process guard", "cleanup processes".
user-invocable: true
allowed-tools: Bash
---

# /guard — Process Guard

Scan and clean up Claude Code processes (orphans, zombies, stale sessions).

## Usage

Based on `$ARGUMENTS`:

| Argument | Action |
|----------|--------|
| (empty) | Run guard with defaults |
| `--dry-run` | Scan only, don't kill |
| `status` | Show current Claude processes |

## Execution

```bash
python -m concinno.process_guard $ARGUMENTS
```

Reports: scanned count, killed count, freed MB, lock entries cleaned.
