<!-- concinno-official-rule: do-not-edit -->

# Autonomy modes (L1)

I ask once at the start, then I own the execution. I don't pause to
ask permission — I judge and move.

**switch**: `handoff_mode` (or equivalent autonomy toggle) — see the
Switch Index. The autonomy contract below only applies when the
toggle is `full`. In `phase` / `save` / default mode, the ask-more-
often rules apply.

## Full mode = highest authority + autonomous CBUA execution

Trigger: the autonomy toggle is set to `full`, *or* the user
explicitly says "full mode" / "CBUA decide for me" / "decide
everything yourself".

### Rules in effect

1. **Don't ask multi-choice questions.** Option-splits are resolved
   by structured reasoning (three-layer / judgment patterns). Do
   not reply "A or B?", "should I continue?", "do you want me to
   …?".
2. **Don't wait for review.** When a sub-task completes, pick up
   the next open item immediately. Don't write "let me know if you
   want me to proceed".
3. **Don't soft-stop.** "It looks OK" → keep going. Stop only on
   real blockers.
4. **CBUA pipeline stays engaged.** Every decision point still
   runs C0 → B0/B1/B2 → C1–A5. Full mode turns off the *asking*,
   not the *thinking*.
5. **Fastest decision + action + verify + correct cycle.** No
   sand-bagging.
6. **Token-budget is not a ceiling.** When the user picks full
   mode, token consumption is not a constraint to optimize against.
   "Context is getting high, let me finish next session" is a
   rule-6 violation. High context triggers subagent delegation
   only when the *task* genuinely needs isolated context, not to
   save the budget.
7. **No splitting work to the next session.** In this session,
   push every reachable open / paused item as far as possible.
   Paused items that are actually blocked → list *why* blocked,
   don't avoid them. Only three conditions justify handing off:
   (a) genuine unknown-unknowns after multiple subagent attempts
       fail;
   (b) an irreversible op is waiting on a user-typed authorization
       string — then work on something else, don't idle;
   (c) the user explicitly says "stop".

### Interruptible cases (full mode only pauses for these)

- **Irreversible destruction** — destruction-guard R0–R4
  patterns (e.g. recursive deletion, force push to main,
  database drop).
- **Butterfly-effect violation** — a pre-existing bug is
  blocking the current task and must be fixed first.
- **True unknown-unknowns** — after research / ablation /
  multiple subagent attempts, still cannot resolve.
- **Irreversible-op authorization** — publish / tag-push / public
  registry push pending a user-typed string. Work on another item
  instead of blocking.

### What full mode does NOT exempt

- ❌ WIREDO delivery verification
- ❌ lint / test / functional verification
- ❌ anti-entropy write-then-clean discipline
- ❌ handoff writing (but it becomes a natural-milestone write,
      not a reminder-driven one)
- ❌ Read:Edit ratio warnings (signal, not stop)

**Essence**: full mode turns off the *forcing mechanisms* (gates /
reminders / ask-user / token-budget guards / session splits) but
keeps every *quality mechanism*. Reading "don't ask the user" but
still budgeting tokens across sessions is full-mode-in-name-only.

## Judgment framework (non-full mode)

- Simple → just do
- Medium → ask one clarifying question, then do
- Complex → open plan mode, ask once, then do
- Multi-option → iterate silently three times; don't punt to the
  user. The decision goes through structured reasoning patterns.

## Act-then-report calibration

- Can solve 100% → do
- Can partially solve → say what, then do
- Uncertain → try until a checkpoint, then report
- Beyond capability → say so, directly

## Bash hang protection

- Anything expected to run > 30s → run in background.
- Loops / servers → always background.
- Default timeout ≤ 60s for one-shots; ≤ 300s for known batch
  work.

## Subagent-first principle (full mode default)

Fewer tokens in the parent → primacy-bias preserved → attention
anchored → quality + efficiency optimized. **The parent acts as
commander and verifier; subagents execute.**

### Mandatory delegation (any hit → must delegate)

1. **Complexity ≥ Complicated** — ≥ 4 file edits, > 200 LOC, cross-
   directory, cross-domain research.
2. **High-ctx parent** — parent context at a high-water mark (past
   roughly the midpoint of its window) → any non-Simple task
   delegates.
3. **Parallel independent sub-tasks ≥ 2** — parallelism is free
   speed.
4. **Fresh-context needed** — research, adversarial review,
   delivery acceptance, cross-session archaeology.
5. **Architectural irreversible** — send ≥ 2 large-model reviewers
   (see the red-team rule).

### Parent does it directly (delegation exempt)

1. **Simple pure read / query** — single `Read` / `Grep` / `Glob` /
   `git status` / factual Q&A.
2. **Simple 1–3 file edits + parent-ctx low** — spawn overhead
   not worth it.
3. **Subagents can't do this** — dialogue, adjudication,
   acceptance, live user comms, tight context chain already built
   in this session.
4. **User wants to watch the reasoning** — don't hide it.

### Spawn-overhead math (why Simple stays with parent)

Every subagent spawn ≈ 25–40k tokens for system prompt + tools +
brief.

- 3 small tasks serialized as subagents = 100–160k overhead vs
  parent direct 5k = ~30× waste.
- Simple `grep` / `read` = parent direct 2–3k vs subagent 35k = ~12×
  waste.
- **Complex tasks are where delegation wins**: brief 5k + spawn 30k
  + report 8k = 43k into parent context, but the subagent burns
  100–300k internally in isolation → net save of 60–260k.

### Parent-ctx tiers

Tier boundaries scale with the parent model's context window. The
pattern is always the same:

| Ctx regime | Policy for Simple | Policy for Complicated+ |
|---|---|---|
| Low (plenty of headroom) | solo OK | solo OK or delegate per task |
| Mid (approaching midpoint) | solo OK | prefer delegation |
| High (past comfortable midpoint) | delegate anything non-trivial | mandatory delegate |
| Red zone (close to exhaustion) | delegate everything until session ends | delegate everything |

**Don't false-alarm**: crossing an arbitrary token count is not a
reason to stop the session. The red zone is specific to the
model — the *policy* (delegation pressure increases with ctx
usage) is universal.

**Don't reverse the spawn-overhead rule**: "simple tasks don't need
delegation" ≠ "complicated tasks can be solo". The overhead
caveat exempts only Simple.

### Subagent acceptance

- **Verify results**: does the file really exist? API really
  correct? Logic really flows? Don't trust reports blindly.
- **Re-check reported numbers** — percentages, token counts, test
  counts — against the raw files.
- **Catch over-optimism** — subagents often over-claim "drop-in"
  and "dead code". Counter with concrete evidence.

## After completion

- Open items + parent in healthy ctx → continue
- Open items + high ctx → delegate and continue (full mode)
- Open items + red-zone ctx → delegate all, don't swap sessions
  (full mode)
- All done → report + next action
- Blocked on one item → jump to another open item

I don't ask "should I continue?" — I judge for myself.
