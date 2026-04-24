<!-- concinno-official-rule: do-not-edit -->

# Feature switches (L1)

I check the switch before I apply the rule. A rule-text absolute
("must AskUser", "always deny", "mandatory") is overridden by an
opt-out switch unless the switch is off. The check takes five lines
and reverses an entire day of wrong prompts.

## Switch-first principle

Every L1 rule that declares a hard behavior also declares the
feature switch that controls it. Before applying the rule, query the
current value of that switch. `disabled` / `off` / `false` means the
hard behavior is skipped and the permissive path runs. Violating
this order means ignoring the user's prior opt-out and asking the
same blocked question every session.

## Switch Index (template)

Every switchable feature appears in a single index table with these
columns:

| # | Feature | Rule file | Switch location | Default | Opt-out | How to query |
|---|---|---|---|---|---|---|

Rows are instances; the schema is the method. A few illustrative
rows (taken from the reference `concinno` runtime):

| # | Feature | Rule file | Switch location | Default | Opt-out | How to query |
|---|---|---|---|---|---|---|
| 1 | `release_authorization` | `release_coord.md` | `~/.concinno/release_auth.json` | gate-on | `{"disabled": true}` / env `CONCINNO_RELEASE_AUTH_DISABLED=1` | `describe_current_config()` |
| 2 | `premise_gate` | `rag_sop.md` | `FEATURE_META` | on | `enabled=False` / env `CONCINNO_PREMISE_GATE=0` | `cfg.feature('premise_gate','enabled')` |
| 3 | `handoff_mode` | `handoff.md` + `autonomous.md` | runtime API | `phase` | `set_handoff_mode('full'|'save'|'phase')` | `get_handoff_mode()` |

The table belongs in a single place per project (not duplicated into
every rule file). Each L1 rule's header points at it.

## Source precedence (later overrides earlier)

1. Rule-hardcoded default
2. Feature registry default (`FEATURE_META` or equivalent)
3. Per-project config file
4. User-level config file (`~/.concinno/<feature>.json` or
   equivalent)
5. Environment variable (`<FRAMEWORK>_<FEATURE>_<PARAM>`)
6. User's explicit statement in the current session

This is the 12-factor precedence chain — the *same pattern* used by
Django settings, Go's `flag`, and most production config systems.

## Rule-application SOP

Before reading any L1 rule, two steps:

1. Locate the rule's `**switch**:` declaration (usually in the first
   five lines).
2. Run the switch's "how to query" lookup (column 7 of the index).

Then:

- `disabled` / `off` / `false` → skip the rule's hard-rules section,
  take the permissive path.
- `enabled` / `on` / `true` → apply the rule.

Violating this is *rule-staleness from the LLM's side* — the user
already answered "no, don't ask me about X" and the LLM keeps
asking because it didn't look up the switch.

## Two-layer gate check (irreversible operations)

A library authorization switch only controls that library's layer.
Any agent harness (Claude Code, Cursor, Aider, custom CLI) carries
its own permission sandbox. The two layers are **independent**: an
opt-out at one does not propagate to the other.

Before an irreversible operation (package publish, registry push,
production deploy, data destruction), list *both* layers' current
state. When the library layer is green but the harness has no
matching rule, report the state to the user and let them choose:
(a) approve in the harness UI once, (b) add an allow rule to the
harness config, (c) type the library's auth string. Do not
blind-run the command and hope; the harness will reject and the
wasted turn emits a misleading "blocked on library layer" report.

## Anti-patterns

- **Primacy-bias reflex** — reading the first sentence of a hard
  rule and applying it without checking the switch. Deny-first
  before opt-out-check.
- **Repeatedly asking an already-opted-out user** — the user
  disabled the gate once; the agent re-asks every session because
  the rule's prose and the gate's actual state diverged.
- **Scattered settings** — the same concept configured in N
  different places (locale: env var, config file, runtime global,
  session override). Consolidate to the 6-source chain.

## Definition-of-Done for a new switchable feature

Every new feature / skill / module must ship with all of the
following. Missing any one is a review-block.

1. **Toggle** — `enabled: bool` turns the whole feature off.
2. **Parameters** — every behavior threshold, frequency, or mode is
   exposed to config (no hardcoded magic numbers).
3. **Sources** — the 6-source precedence chain above is honored.
4. **Index entry** — one row in the Switch Index table.
5. **Rule header** — if the feature has hard rules in an L1 file,
   that file's header declares the switch.
6. **Sedimentation** — when the feature was born from a
   correction, a note in the project's feedback or changelog
   captures the origin.

Any hard rule the file claims (in prose) must trace to a switch in
the index. Rule prose is the user-facing contract; the switch is
the enforcement knob.

## Auto-tune vs manual override (when the system has both)

When a feature has both (a) an online-learner auto-tuning a
parameter and (b) a user-settable value for the same parameter:

```text
Is there a conflict?
├─ No → use the single value.
└─ Yes → Has the user explicitly opted the feature out
         (enabled=False)?
    ├─ Yes → feature off; auto-tuner does not override.
    └─ No → Has the user explicitly pinned the parameter
            this session?
        ├─ Yes → manual value locks; auto-tuner does not
                 override (until the user unlocks or the
                 session ends).
        └─ No → Is the parameter cosmetic / UX / i18n
                (does not affect task-completion quality)?
            ├─ Yes → manual value wins (auto-tuner should
                     not spend budget learning cosmetic
                     preferences).
            └─ No → **auto-tuner wins** (the system aims
                    for SOTA; manually-set non-cosmetic
                    parameters are starting guesses, not
                    locks).
```

The auto-tuner's overwrite is logged to stderr with the old value,
new value, and reason signal. The user restores the manual value
with one explicit statement.

Every feature entry in the registry carries two meta flags:
`ziq_autotunable: bool` and `cosmetic: bool`. The auto-tuner skips
features where `ziq_autotunable=False` or `cosmetic=True`.

"ZIQ" names the specific auto-tuner in the reference runtime; the
*pattern* above — user-explicit > opt-out > cosmetic-manual >
auto-tuner — applies to any online-learner + user-override system.

## SOP for adding a new switch

1. Pick a name (reverse-DNS or `snake_case`).
2. Register in the feature meta table with `enabled`,
   `ziq_autotunable`, `cosmetic` filled.
3. Add a user opt-out file schema if user-tunable.
4. Add one row to the Switch Index.
5. Add the `**switch**:` declaration to the L1 rule file if the
   feature has hard rules.
6. If the feature was born from a correction, write a feedback
   note linking the switch to the incident.

I check switches before I apply rules.
