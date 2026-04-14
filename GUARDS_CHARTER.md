# CC-Cortex Guards Charter

> The constitution for cc-cortex's guard system. This document is the
> **forcing function** that prevents the guard system from sliding into
> Goodhart's Law theater.
>
> **Status**: Active (effective from version 1.14.x)
> **Last revised**: see git blame
> **Authority**: This document overrides any conflicting guard implementation.
> If a guard contradicts the charter, the charter wins; the guard must be
> changed or deleted.

## 1. Purpose

cc-cortex ships ~55 guards that run on every Claude Code tool call. This
charter exists because the guard system was caught displaying classic
Goodhart symptoms: warnings stacked into ritual, regex matchers gamed by
inserting jargon, ratio thresholds without empirical backing, and tests
that verify guard mechanics rather than guard effectiveness.

The charter encodes the design rules that resist these failure modes by
construction, not by good intentions.

## 2. Three-tier severity model

Every guard MUST be classified into exactly one of three tiers. The tier is
declared in `FEATURE_META[<name>]["tier"]` and enforced by the dispatcher.

| Tier | Behavior | When to use |
|---|---|---|
| `hard_block` | Returns `decision=block`. Halts the tool call. | Security, data destruction, identity, unrecoverable state changes. |
| `advisory` | Aggregated into a once-per-session digest emitted at session end (or `/digest` on demand). NOT printed per tool call. | Quality / style / cognitive nudges. Things a reasonable engineer wants to know but does not need interrupted by. |
| `telemetry` | Silent. Recorded to `.cc_cortex_cache/audit/` for retrospective review. | Pure observation. Anything where the cost of a single false positive exceeds the marginal value of the signal. |

A guard that does not declare a tier defaults to `advisory`. A guard that
attempts to print to stderr per tool call without `tier == "hard_block"` is
a charter violation and must be refactored.

### Quotas

The number of `hard_block` guards is **capped**. The cap lives in
`guards.quota.toml` at the repo root and is checked by CI on every PR.

| Tier | Quota | Rationale |
|---|---|---|
| `hard_block` | **10** | A user who has to negotiate with more than 10 hard blocks per session is being held hostage, not protected. |
| `advisory` | unlimited (digest absorbs the noise) | But digests > 30 lines also trigger an entropy warning. |
| `telemetry` | unlimited | These never reach the user directly. |

Promoting a guard to `hard_block` when the quota is full requires
**simultaneously demoting another `hard_block` guard in the same PR**. CI
fails on quota violation. There is no exception.

## 3. Forcing functions, not metrics

The charter prefers **structural constraints** over **measured behavior**:

- A `quota.toml` is a forcing function: the build breaks when violated.
- An "average warning quality" metric is a target: it gets gamed.

Concretely:

- **P0** Charter (this file) + cron audit. Forcing.
- **P1** `guards.quota.toml` + CI check. Forcing.
- **P2** Coverage delta gate via `pytest --cov` diff against last commit.
  Forcing. *Replaces* the deprecated `delivery_ledger` proposal which was
  itself a Goodhart trap (an opt-in jsonl file is just an honor system in
  disguise).
- **P5** UIVerify scope = project marker file (e.g. `.cc-cortex/ui-verify.enabled`)
  in addition to the file-extension whitelist. Forcing.
- **P6** `override_rate` autocounter for `hard_block` guards. Forcing.

The remaining items (digest read rate, retrospective audit, churn cadence)
are honestly labeled as **discipline-dependent** in this charter and are
not pretended to be enforced.

## 4. Triangulation against single-metric Goodhart

The success of any guard reform is measured against **three independent
outcome metrics simultaneously**. Optimizing for any single one is a known
failure mode.

| Metric | Direction | Goodhart if optimized alone |
|---|---|---|
| Signal quality (warning → user fix rate) | up | drop guards entirely → 100% trivially |
| Noise rate (warnings emitted per tool call) | down | same as above |
| Override rate (`CC_CORTEX_FORCE_STOP` / per-guard escape used) | flat or down | same as above |

A reform iteration succeeds only if all three move in the right direction
at once. Any presentation that touts one metric in isolation is rejected
by the charter.

## 5. Pre-registered kill criteria

This charter pre-registers the conditions under which the reform is
considered **failed and must be rolled back**. These are checked monthly
by the CI cron job `scripts/charter_audit.py`.

1. **Hard-block creep** — six months after charter adoption, if the count
   of `hard_block` guards exceeds **12** (charter cap is 10, +2 grace),
   the reform is judged failed and the entire `hard_block` set is reset
   to the post-adoption baseline.

2. **Override leakage** — any guard whose `override_rate` exceeds **50%**
   over a rolling two-week window is automatically demoted from
   `hard_block` to `advisory`. This is non-negotiable; the demotion is
   performed by the audit script, not by humans.

3. **Digest abandonment** — if the session-end advisory digest is unread
   (no `/digest` query, no scroll past it in the terminal log) for **7
   consecutive days**, the entire `advisory` tier is dropped. A digest
   nobody reads is just a longer log file.

4. **Self-test tautology** — any new guard test that mocks the guard's
   own input to verify the guard's own output (without an external
   ground truth) is rejected at code review. The charter audit script
   greps for this pattern in `tests/` and reports violations.

A pre-registration that cannot be falsified is a wish, not a charter.

## 6. Chesterton's Fence policy

The charter rejects "delete on first miss" but also rejects "keep
indefinitely." The two-step demotion path is:

```
hard_block → advisory → telemetry → deletion
   90d         90d         90d
```

A guard that does not catch a real incident in a 90-day window is
demoted one tier. A guard at `telemetry` for 90 days with zero recorded
matches is deleted. This preserves signal during transition and gives
authors three audit windows to make their case.

Each demotion is logged to `guards.history.jsonl` so the lineage is
recoverable. Resurrecting a deleted guard is allowed but requires a
written justification in the PR description that addresses why the
prior demotion path was wrong.

## 7. What is NOT in scope

The charter deliberately does not legislate:

- Specific guard implementations. Guards are owned by their modules.
- Style preferences inside guard code. ruff handles that.
- Whether to write a guard for a given concern. That is engineering
  judgment, not charter material.
- Coverage targets, latency budgets, or performance regressions. Those
  live in the standard quality pipeline.

The charter only governs the **shape** of the guard system as a whole,
not the contents of any individual guard.

## 8. Amendment process

Changes to this charter:

1. Open a PR titled `charter: <summary>`.
2. The PR description must list which clause is being changed and why.
3. The PR must update the `charter_audit.py` script if the change
   affects an enforced clause.
4. At least one of the three triangulation metrics must be presented
   with the change (charter is not amended on hunches).

Changes that loosen quotas (raise `hard_block` cap, extend grace
periods, relax kill criteria) are subject to a **30-day cooling-off
period** between approval and merge.

## 9. Audit cadence

| Check | Frequency | Owner |
|---|---|---|
| Quota compliance | Per PR | CI |
| Override rate | Daily | `charter_audit.py` cron |
| Digest read state | Weekly | `charter_audit.py` cron |
| Demotion path execution | Monthly | `charter_audit.py` cron |
| Kill-criteria evaluation | Quarterly | maintainer |

## 10. Acknowledgments

This charter is the synthesis of a structured red/blue-team review of the
prior reform proposal. The red team caught three FATAL Goodhart traps in
the original plan (counterfactual incident counting, N=1 ablation, opt-in
delivery ledger), and the blue team's triangulation defense survived
review. The current shape — forcing functions over metrics, pre-registered
kill criteria, two-step demotion — is what remained after both critiques.

The reform that produced this charter is itself bound by the charter:
clause §5 applies to the reform, not just to the guards it touches.
