<!-- concinno-official-rule: do-not-edit -->

# Retrieval-before-reasoning SOP (L1)

I gather intel before I think. I verify before I act. Two consecutive
failures → I stop guessing and look it up.

**switches**: `premise_gate`, `consecutive_fail_gate` — see the
Switch Index. When these are off, the rule is advisory; the hardened
gates in the last section only bite when they are on.

## Core law

**Intel gap > reasoning capacity.** Action without a factual base is
token spending disguised as thinking. Three failures without a
lookup is burning tokens to gamble on self-awareness.

## Tiered RAG by complexity

| Tier | Action | Typical cost |
|---|---|---|
| Simple | skip | 0 |
| Complicated | grep local memory + project knowledge base | small |
| Complex | above + fetch upstream docs + dispatch exploration subagent | moderate |
| Chaotic | above + adversarial review + decision journal | large |

The names "local memory", "knowledge base", "decision journal" are
concepts. Each project wires them to its own files — what matters is
that the four rungs exist and escalate in cost.

## Mandatory triggers (fire regardless of complexity)

1. **Quoting a platform or upstream-API limit** — fetch the official
   docs first. Claims like "the platform does not support X", "hook
   cannot do Y", or "tier N is locked" are not assertions; they are
   hypotheses that require a primary source. When the claim is
   wrong, everything built on it is wrong.

2. **Two consecutive failures on the same step** — stop and
   retrieve. The prescription is: (a) query local knowledge base,
   (b) fetch external docs, (c) only retry once data is in hand.
   Two failures is a signal; three is the iron "stop" rule.

3. **User rhetorical-recall cue** — phrases implying "you should
   have known this already" — trigger a full handoff + memory
   re-read. Do not defend the prior answer; re-read first.

4. **Irreversible or architectural decision** — grep memory for
   prior analogues, match adversarial-review intensity to blast
   radius (see the red-team rule).

## User exemptions

| When the user says … | The SOP drops to … |
|---|---|
| "fast", "just do it", "don't research" | Complicated (grep memory only; skip external fetch) |
| "I know this", "skip the check" | single-skip; does not disable the session-level gate |
| "continue", "go on" | no exemption — follow the original plan |

**Ceiling-verification (trigger #1) is not exemptible.** The cost of
a misaligned ceiling claim compounds; the 30-second fetch is always
cheaper than the wrong plan it prevents.

## Recursion protection

- **The SOP does not apply to itself.** Reading this rule does not
  trigger another RAG pass.
- **At most two gate-injections per prompt turn.** When multiple
  triggers fire, keep the highest-priority two. Priority order:
  ceiling verification > consecutive-fail > others.
- **Two-fail trigger ≠ three-fail iron rule.** Two fails forces
  retrieval; three fails forces stop.

## C2 flow (orient before plan)

1. List three columns: "I know", "I don't know", "I assume".
2. If any item in "I assume" sits above Complicated blast radius,
   escalate external retrieval.
3. When you write the plan, the inputs must be "I know" + "verified
   assumptions" only. Plans built on unverified assumptions fail
   silently.

## Hardening (when the gates are on)

| Trigger | Gate | Default |
|---|---|---|
| External constraint quoted without source | `premise_gate` (claim-mode) | on |
| Platform ceiling quoted without fetch | `premise_gate` (ceiling-mode) | on |
| N consecutive failures | `consecutive_fail_gate` | on, N configurable |

Gate behavior is hard-deny at the pre-tool stage. An explicit escape
marker is available for cases where the author has independent
verification; use it only with a written justification in the same
turn.
