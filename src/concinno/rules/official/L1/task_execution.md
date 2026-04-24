<!-- concinno-official-rule: do-not-edit -->

# Task execution SOP (L1)

**switches**: aggregate of the CBUA / RAG / WIREDO / red-team /
handoff switches — see the Switch Index. Any sub-rule opt-out
skips the corresponding stage.

Load this rule when starting a task, taking over one, or detecting
mid-conversation intent drift.

## 9-stage pipeline (Hard / Flex / Fallback three-state)

| # | Stage | State | Purpose |
|---|---|---|---|
| 0 | Intent anchoring (re-read prompt + grep handoff + one-sentence restate) | **Hard** | Prevents desync between the user's ask and your plan |
| 1 | Difficulty assessment + plan-mode decision | **Hard** | Classifier with hysteresis prevents self-downgrade |
| 2 | CBUA best-solve planning (side-effect prevention) | **Hard + Flex depth** | B0–B3 escalate with radius |
| 2.5 | Design-phase adversarial stress | **Flex** (Chaotic only) | Cheaper than acceptance-phase review; catches vaporware |
| 3 | Execute + meta-cognition monitoring + per-N-spawn self-doubt | **Hard monitoring / Flex frequency** | Behavioral signal, not text-regex |
| 4 | WIREDO six-dim self-verify | **Hard** | D-dim = functional verification; UI → screenshot; tsc/lint does not count |
| 5 | Acceptance red / blue / green review | **Flex** (radius-gated) | Chaotic: 3R + 1B + 1G / High: 1R + 1B / Medium: 1R + parent-as-blue / Simple: skip |
| 6 | Commander final verdict (5 states + 4-step framing check) | **Hard** | Prevents the red-team from re-framing advantages as disadvantages |
| 7 | Sedimentation (feedback / rules / memory / skills) | **Hard** | Same correction N times → change the rule |
| 8 | Per-day scheduled reflection | **Flex** (if configured) | Slow-timescale cognition (Axis B) |
| 9 | Per-week scavenger / memory cleanup | **Flex** (if configured) | Even slower-timescale cognition (Axis B) |

## Hard rules (run regardless of difficulty)

1. Honesty law — when you don't know, say so.
2. Handoff hygiene ladders — butterfly-effect / three-state status
   (done / paused / open) / verification supreme / sedimentation /
   anti-entropy.
3. WIREDO D-dim functional verification before delivery.
4. Irreversible-op authorization — package publish / registry push
   / tag push / data destruction needs a user-typed string *or*
   an explicit opt-out.
5. Intent anchoring (stage 0) — always.
6. Difficulty default = Complicated, downgrade only on whitelist
   match.
7. Commander 4-step framing check + 5-state verdict.
8. Self-correction sedimentation — correction → rule update.

## Flex rules (scale with blast radius; C0 drives)

| Radius | TODO format | Plan mode | Review intensity | Monitoring frequency |
|---|---|---|---|---|
| Simple | none | none | skip | per turn |
| Complicated | mental 3–5 | none | 1R optional | per turn |
| Complex | TodoWrite 5–10 | consider | 1R + 1B | per-N-spawn |
| Chaotic | TodoWrite + plan mode | mandatory | 3R + 1B + 1G | per spawn |

## Fallback rules (error recovery)

1. Any step fails → downgrade + roll back + replan.
2. Two consecutive failures → forced RAG (retrieve before
   retrying).
3. Three consecutive failures → stop (the three-fail iron rule).
4. Red / blue conflict → commander 5-state verdict.
5. Irreversible destruction attempt → hard deny.
6. Session death while work is in flight → emergency handoff
   (≤ 20-line stub auto-written so the next session is not
   blind).
7. Spawn infinite recursion → limit guard raises.
8. Parent-context red zone → solo forbidden; delegate everything
   until session ends.

## Commander 4-step framing check

Before accepting a FATAL from any red-team verdict, pass four
framing tests:

1. **Is the scene-premise correct?** (e.g. cost framing from a
   metered-API model attacking a flat-rate CLI subscription →
   reject the cost attack.)
2. **Is the attack "fix" or "kill"?** Red-teams often escalate
   "this spot needs strengthening" into "delete the whole
   layer". Keep it at the real scope.
3. **Platform-ceiling vs architecture flaw?** Constraints of the
   host platform are not architecture flaws of your system.
4. **Is the "flaw" actually an advantage being stigmatized?**
   High-frequency hooks, layered gates, checklists — these are
   often features, not bugs.

## Commander 5-state verdict

Accept / Accept-with-downgrade / Reject / Hold / Counter-question.
See the red-team rule for conditions and actions.
