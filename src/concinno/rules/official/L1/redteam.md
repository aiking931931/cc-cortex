<!-- concinno-official-rule: do-not-edit -->

# Adversarial review — red / blue / commander (L1)

I design as commander. Red and Blue are both clean-context
architects — same identity, opposing anchors.

**switch**: `redteam` — see the Switch Index. Low-radius decisions
skip by default; high-radius decisions mandate it; medium-radius
the parent plays the blue role.

## Model selection

Route the red-team and blue-team calls to the strongest frontier
model available to you. A weaker model is not a cheaper version —
empirically the delta between frontier and one tier below is large
enough that the review becomes theatre. If cost is the constraint,
raise the blast-radius bar for when you launch a review rather than
cheapening the review itself.

Set the model identity **explicitly** at the call site. Do not
depend on a default — defaults drift as models are deprecated.

## 5-axis verdict framework

When facing a FATAL / HIGH from a review, pass all five axes before
the 4-step framing check:

| Axis | Question |
|---|---|
| **Really done?** | Functionally complete, or half-written and declared finished? (Includes island-module check: does the new module connect to at least one consumer?) |
| **Wired?** | Is the new module reachable via entry points / imports / routes? |
| **Functionally works?** | Is there an end-to-end test demonstrating it runs? `tsc` / lint do not count. |
| **Capability up?** | Does this make the agent better / faster / more accurate — or only add overhead (checklist theatre)? |
| **UX easier?** | Is the user's action simpler — or more bureaucratic / higher cognitive load? |

**FATAL with ≥ 3 axes failing → must fix. ≤ 1 axis failing → log as
known carry-over and ship.**

## Core design — clean-context architect, two anchors

**Self-red-team is theatre** — empirically, a parent process acting
as its own red-team catches a small fraction of what a clean-context
frontier-model review catches. Self-blue-team is similarly weak.
The parent plays **commander only**.

Red and Blue share *the same identity* (architect / senior
reviewer / scene judge). They differ only in **anchor direction**:
red anchors on "this should not exist"; blue anchors on "this is
defensible". The tension between them is the improvement space.

### Red-team prompt template

```text
You are a frontier-model architecture attacker + top-tier venue
reviewer + competing-product PM.

Your task:
1. Aggressive disagreement — find 3+ reasons "this should not
   exist"
2. Academic / venue attack — top-tier reviewer standard on
   novelty, method, experiments, comparisons; no "could
   consider" / "perhaps improve" soft rejects; FATAL or LOW,
   no middle
3. Commercial attack — does the user actually need this? What
   does the competing product do? Why yours — with metrics,
   not "more careful"
4. Goodhart sweep — enumerate how each metric can be gamed
5. Attack design premises — should this exist? Right place?
   Real user need?

Hard requirements:
- Concrete: read files, cite line numbers, assign
  FATAL/HIGH/MEDIUM
- No hand-waving, no soft-sell
- Do not take the blue-team's angle — you only attack

Five-axis coverage — Really-done / Wired / Functionally-works /
Capability-up / UX-easier — at least one attack per axis.

Proposal: {proposal}
Constraints: {constraints}
Context: {context}
```

### Blue-team prompt template

```text
You are a frontier-model architecture defender + systems architect
+ scene judge.

Absolute prohibitions:
- Never concede passively — "red sounds right, so I'll agree" =
  failure of duty
- Don't get framed — when red's framing is wrong, attack it;
  don't modify the proposal to accommodate a wrong attack
- Honest — admit weaknesses, but don't inflate them to "cut it
  all"

Duties:
1. Verify wiring — read code, cite line numbers, show the
   system actually runs
2. Show reasonableness — 3+ real scenarios + why alternatives
   are worse
3. Honest weakness classification — is this a bug, a drawback,
   or a design choice?
4. Counter red's wrong framing, including:
   - wrong cost model (metered API vs flat subscription)
   - out-of-scope demands (academic novelty vs shipping
     engineering)
   - platform ceiling attacked as architecture flaw
   - advantages restyled as disadvantages
5. Five-axis evidence — Really-done / Wired / Functionally-
   works / Capability-up / UX-easier — evidence per axis
6. Prepare the commander's verdict packet

Proposal: {proposal}
```

## Commander duties (parent process)

- **Don't attack, don't defend — only adjudicate.**
- Red attack with evidence → accept.
- Red attack without evidence, or attacking a Goodhart metric
  itself → reject.
- Blue defence with substance → acknowledge.
- Blue defence that only covers up → expose.
- **Don't get led by the nose.** Believe neither side's
  narrative wholesale.

## 4-step framing check (before accepting any FATAL)

Red-teams reliably over-escalate, especially by stigmatising
advantages. Run all four checks before accepting:

1. **Is the scene-premise correct?** Does red's cost / constraint
   model match the actual deployment?
   - Metered-API cost framing attacking a flat-rate subscription
     system → reject the cost attack entirely
   - Academic-novelty framing attacking a shipping engineering
     tool → accept the novelty score, reject the "cut it all"
     conclusion
2. **Is the attack "fix" or "kill"?** Red tends to escalate
   "strengthen here" into "delete this layer".
   - Real weakness → strengthen (add gate / test / limit)
   - Over-escalated → reword only, keep the architecture
3. **Platform ceiling vs architecture flaw?** Host-platform
   constraints are not your architecture's flaws. Tag as
   "future bypass layer" work, don't cut the current layer.
4. **Advantages stigmatised?**
   - "High-frequency hooks = expensive" — maybe a metered-API
     misread
   - "Many layers = bureaucratic" — maybe a dynamic router
     activating a subset
   - "Checklist = jargon stacking" — maybe marker-stuffing
     edge case; fix the behavioral signal, don't cut the layer

## 5-state verdict (non-binary)

| State | Condition | Action |
|---|---|---|
| **Accept** | Real weakness + evidence + framing correct | Must fix |
| **Accept-downgrade** | Right direction + framing too strong | Reword only / add clarification |
| **Reject** | Framing error / attacking a Goodhart itself / platform ceiling mis-classed as flaw | Don't change |
| **Hold** | Unverified but plausible | Mark pending, re-review next round |
| **Counter-question** | Red's framing ambiguous | Dispatch a new red-team with narrower scope |

## Commander hardening

Any red-team verdict injected into a planning document or memory
must pass the 4 framing checks *per FATAL item*, not as a batch.
Batch-accepting reviews is how "advantages got restyled as
disadvantages" slips through.

## Trigger conditions (any hit → mandatory review)

- Architectural design (new module / system / pipeline)
- Core-logic rewrite > 200 LOC affecting other modules
- Irreversible decisions (DB schema / API surface / OSS
  release)
- Direction choice committing > 1 day of work
- User says "major decision" / "don't mess around"
- Delivery verification at Complicated or higher blast radius

## Blast-radius sizing

| Radius | Definition | Review intensity |
|---|---|---|
| **High** | New architecture / irreversible / release / paper / first-of-kind | 1 red + 1 blue (both frontier-model architects), ≤ 3 rounds |
| **Medium** | Extension of a proven architecture + reversible | 1 red (frontier), blue played by parent process |
| **Low** | Ablation / grid / tiny cost | Skip — use checkpoint monitoring |

## Process (High radius)

1. **CBUA best-solve** — C0 → B1 → ≥ 3 alternatives → pick the
   sweet spot.
2. **Dispatch red** (architecture attacker): full proposal +
   constraints, ≤ 3 rounds.
3. **Dispatch blue** (architecture defender): concurrently or
   after red returns.
4. **Commander adjudicates** — lay out both conclusions, decide on
   evidence, not vibe.
5. **Execute corrections** — fix the real bugs, reject the
   Goodhart attacks.

## Anti-patterns

- ❌ **Self red-team** — empirically much weaker than clean-
     context frontier-model red-team.
- ❌ **Self blue-team** — misses multiple FATAL bugs per review.
- ❌ **Flipping under pressure** — red attacks loudly → parent
     kills a working feature without evidence.
- ❌ **Frontier model on low-radius work** — waste.
- ❌ **Three parallel red-teams** — empirically one architect
     outperforms three specialists (who bikeshed each other).

## Skip conditions

Simple / B0 work · reversible ops · previously stress-tested same
shape · user explicitly says "fast" · low blast radius.
