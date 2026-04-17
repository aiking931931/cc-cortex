# CCC Built-in Skills Index

CCC built-in skills index — slash commands shipped with `pip install concinno`.

## Available Skills

| Skill | Triggers | Purpose | File |
|-------|----------|---------|------|
| `guard` | `guard`, `process guard`, `cleanup processes`, `防護` | Scan and clean up orphaned/zombie/stale Claude Code processes. Reports scanned, killed, freed MB, lock entries cleaned. | `guard.md` |
| `cortex-hooks` | `cortex hook`, `hook mode`, `鉤子` | Configure concinno hook behavior — switch modes (off/auto/min/max), toggle individual features (destruction_guard, session_lock, token_warn, streak_ux, lint). | `hooks.md` |
| `cortex-schedule` | `schedule`, `排程`, `cortex schedule` | Manage recurring background tasks (self-reflection, scavenger, weekly-research). Install/uninstall on Task Scheduler / launchd / cron. | `schedule.md` |

## Loading Mechanism

CCC loads these skills via `SkillRouter` / `SkillRegistry` at startup. Each `*.md` file in this directory with valid frontmatter becomes a slash command. Trigger keywords in the `description` field drive the matching.

## How to Add a New Skill

1. Copy `SKILL_TEMPLATE.md` to `<skill-name>.md` in this directory.
2. Fill in the frontmatter (`name`, `description`, `user-invocable`, `allowed-tools`).
3. Keep the L2 body (this file) ≤50 lines. Push details into `<skill-name>-<topic>.md` topic files read on demand.
4. Use both gaseous language (first-person belief, e.g. "I verify before I hand over.") and solid language ("You MUST ..."). Both are required.
5. Add the new skill as a row in the table above. **An unindexed skill is invisible to users.**
6. No personal paths, no `_Z` suffix logic, no CC-specific assumptions. CCC must run for strangers.
7. Run `pytest tests/skills/` to verify the registry picks it up.

## Future Skills (placeholder)

Reserved for upcoming additions. When adding a skill, replace this section with its row in the table above.

- _(none yet)_

## See Also

- `SKILL_TEMPLATE.md` — copy this when starting a new skill.
- `../CLAUDE.md` (repo root) — CCC contributor boundary rules (library, not application).
- `../installer.py` — how skills are discovered and installed into a host project.
