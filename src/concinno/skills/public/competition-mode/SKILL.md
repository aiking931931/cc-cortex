---
name: competition-mode
description: DEPRECATED — renamed to general-mode in Concinno 2.6.0. This redirect will be removed 3 months from 2026-04-18. Please use `/general-mode` instead.
triggers:
  - competition-mode
  - 比賽模式
user-invocable: true
---

# /competition-mode — DEPRECATED (use `/general-mode`)

> This Skill has been renamed. Concinno 2.6.0 removed the old
> "eager inject SOP before every tool call" semantics — the unified
> agent loop in `/agent` now covers both daily operation and
> benchmark runs (OSWorld / WebArena / AgentBench / CyberGym etc.).
> The renamed entry point lives at `/general-mode` and documents the
> normal PyPI ship default (general + en + auto-compact + memory file).

**Scheduled removal**: 3 months from 2026-04-18 → **2026-07-18**.

## What to do

1. Invoke `/general-mode` for mode switching / config inspection.
2. Invoke `/agent` for the actual execution loop that used to live
   under `competition-mode`.
3. If you had `competition-mode` hard-coded in scripts, update them
   to `general-mode` before 2026-07-18 — after that, this redirect
   is removed and the stale reference falls through to the default
   Claude Code Skill resolver (no-op).

## Why the rename

`competition-mode` carried an implicit "eager inject SOP on every
tool call" contract that was already superseded by:

- `concinno.config` four-layer loader (2.6.0) — real config, not a
  Skill-side flag.
- `/agent` Skill (MEMORY #36d) — one agent loop for all scenarios,
  no mode switching.

Keeping a Skill called "competition" after the semantics moved
would be misleading, so the user-visible name now matches what it
actually does: describe the general PyPI ship default.
