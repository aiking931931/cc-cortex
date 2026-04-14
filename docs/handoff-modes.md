# Handoff Modes

CC Cortex supports three handoff modes that trade off **token conservation**
against **execution continuity**. Mode is read from
`${CLAUDE_PROJECT_DIR}/.claude/hooks/cc_config.json` via
`cc_cortex.handoff_engine.get_handoff_mode()`. Default is `phase`.

## Quick comparison

| Aspect | `save-token` | `phase` | `full` |
|---|---|---|---|
| Agent-spawn gate | 140K (block) / 160K (critical) | 180K hard ceiling | **none** |
| Handoff reminder | 80K + ≥3 files modified | 150K (file-count ignored) | **none** |
| Autonomous authority | Ask often | Ask at task boundary | **Pre-authorised for the full project flow** |
| Philosophy | Preserve tokens; hand off early | Finish current task; hand off cleanly | CBUA-driven continuous execution |

## `save-token` — Conservative default

Best for exploratory sessions, unfamiliar codebases, and user-driven review
loops where the cost of a bad direction is high.

Rules:

- Agent tool is blocked at `GATE_AGENT = 140_000` context tokens.
- Critical warning + block at `GATE_CRITICAL = 160_000`.
- `check_handoff_reminder` fires at `REMINDER_TOKEN_MIN = 80_000` when the
  session has modified at least `REMINDER_FILE_MIN = 3` files without
  writing a handoff.
- Reminder fires once per session.

## `phase` — Task-boundary aware

Best for sessions that know the current task list and want to finish it
without being interrupted, but still respect a hard ceiling as a safety net.

Rules:

- No Agent gate until `_PHASE_GATE = 180_000` context tokens (then block
  with the same critical guidance as `save-token`).
- Reminder fires only at `_PHASE_REMINDER = 150_000`, ignoring file count.
- Everything else identical to `save-token`.

## `full` — Maximum autonomous execution

Best when the user has explicitly delegated the entire project flow to the
agent and wants uninterrupted CBUA-driven execution.

**Semantic**: "the user has pre-authorised autonomous execution of the
entire project flow. The agent's job is to judge and move."

Rules:

- **No token gating.** Agent spawns are always allowed. Context bandwidth
  is treated as a tool, not a warning signal.
- **No handoff reminders.** Writing a handoff remains available as a
  deliberate action, but no nag mechanism fires.
- **Autonomous decision authority.** Every choice point runs the full
  CBUA pipeline (C0 route → C2/C3 think → A0-A5 act) without pausing to
  ask the user. The agent picks direction, technique, ablation plan, and
  rollback triggers by itself.
- **Execution discipline.** Decide fast, act fast, verify fast. No
  "should I continue?" questions. No waiting for review between
  sub-tasks. Unblocked todos are picked up automatically.
- **Hard stops that still interrupt `full` mode**:
  1. Destructive / irreversible actions gated by
     `DestructionGuard` (R0-R4) — dropping tables, force-pushing to main,
     deleting uncommitted work, etc.
  2. Butterfly-effect rule violations — a discovered pre-existing bug
     that blocks the current task must be addressed before the task can
     continue (the rule applies in all modes, `full` does not suspend it).
  3. Genuinely unknown unknowns — situations the agent cannot resolve via
     further research, ablation, or sub-agent delegation within its
     current toolset. In those cases the agent reports and waits.
- **Not a license to skip tests, skip verification, or skip documentation.**
  `full` lifts *forcing* mechanisms (token gates, reminders, ask-user
  prompts). It does not lift *quality* rules (WIREDO, verification,
  handoff hygiene).

### Typical `full` mode invocations

From the user:

- "full 模式繼續" / "full mode continue"
- "所有問題你自己決定並執行"
- "CBUA 自主判斷完成整個流程"
- "直接跑到結果出來再報告"

From the agent (internal behaviour under `full`):

- Never asks multi-option choice questions to the user; runs CBUA
  `three_layer` / `judgment` skills silently and commits to a direction.
- Kills unproductive sub-processes without confirmation (they are
  reversible — can be restarted).
- Writes handoff checkpoints opportunistically at natural milestones
  rather than under threshold pressure.
- Uses sub-agents freely for independent parallel work.

## Programmatic check

```python
from cc_cortex.handoff_engine import is_full_autonomous, get_handoff_mode

if is_full_autonomous():
    # Skip ask-user prompts, skip soft reminders,
    # default to direct CBUA-driven execution.
    ...

mode = get_handoff_mode()  # "save-token" | "phase" | "full"
```

## Setting the mode

The mode is persisted in `cc_config.json`:

```json
{
  "handoff_mode": "full"
}
```

It can also be toggled via the `handoff` skill / slash command, or
programmatically with `set_handoff_mode("full")`.

Changing the mode takes effect on the next tool call.
