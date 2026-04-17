---
name: <skill-name>
description: <One sentence describing what this skill does and when it triggers. Triggers on "<keyword1>", "<keyword2>", "<中文觸發詞>".>
user-invocable: true
allowed-tools: Bash, Read, Edit
---

# /<skill-name> — <Short Title>

<!--
CCC Skill template. Three-layer architecture:
  L1 = frontmatter (auto-loaded every conversation, used for trigger matching)
  L2 = this SKILL.md body (loaded when triggered, ≤50 lines, decision tree + routes)
  L3 = topic files (read on demand, e.g. <skill-name>-<topic>.md)
-->

> I <gaseous language: first-person belief about why this skill matters. Build identity, not instructions. Example: "I verify before I hand over." or "I never let an orphan process linger.">

> **You MUST** <solid language: hard rule that cannot be violated. Example: "You MUST run `--dry-run` before any destructive cleanup."> Add 1-3 such rules.

## Why

<2-3 sentences explaining the problem this skill solves and the cost of not having it. Keep it concrete — what breaks without this?>

## Usage

Based on `$ARGUMENTS`:

| Argument | Action |
|----------|--------|
| (empty) | <default behavior> |
| `status` | <show current state> |
| `--dry-run` | <preview without side effects> |

## How to apply

1. <Step 1 — what the user/agent does first>
2. <Step 2 — verification step>
3. <Step 3 — recovery if step 2 fails>

## Execution

```bash
python -m concinno.<module> $ARGUMENTS
```

Reports: <what the skill outputs so the caller can verify success>.

## Triggers

- English: `<keyword1>`, `<keyword2>`
- 中文：`<觸發詞1>`, `<觸發詞2>`

<!--
Writing rules — DO NOT VIOLATE:
  1. NO personal paths. No `E:\...`, no `C:\Users\...`, no `_AI_BRAIN/...`. Use `Path.home()` or env vars.
  2. NO `_Z` suffix logic. `_Z` is CC-private and must never appear in CCC source.
  3. NO CC-specific assumptions. CCC must run for strangers.
  4. ≤50 lines for L2 body (everything between frontmatter and writing-rules comment).
  5. Gaseous + solid language are both required. Gaseous builds identity, solid prevents drift.
  6. Topic files (L3) live next to this file as `<skill-name>-<topic>.md` and are read on demand only.
  7. Update `SKILL_INDEX.md` when adding a new skill. An unindexed skill is invisible to users.
-->
