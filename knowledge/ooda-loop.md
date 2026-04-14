# OODA Loop

Observe → Orient → Decide → Act. The fastest accurate decision cycle wins.
Speed of iteration matters more than perfection of any single iteration.

## The Four Phases (John Boyd)

### Observe

Gather raw data from the environment. Don't interpret yet — just collect.

- What changed since last check?
- What signals are present (logs, errors, user feedback, metrics)?
- What is absent that should be present? (Negative evidence)

### Orient

The most critical phase. Interpret observations through your mental model.
Orientation shapes what you see and what you decide.

- **Previous experience**: Have I seen this pattern before?
- **Cultural traditions**: What does the team/codebase expect?
- **New information**: What doesn't fit my existing model?
- **Analysis & synthesis**: What story does the data tell?

Boyd's key insight: **Orientation is the schwerpunkt** (center of gravity).
A wrong mental model means you observe wrong, decide wrong, and act wrong —
no matter how fast.

### Decide

Choose an action based on orientation. Prefer reversible decisions with
short feedback loops.

- What's the hypothesis?
- What's the minimum action to test it?
- What would disprove the hypothesis?

### Act

Execute the decision. Then immediately return to Observe — the loop never
stops.

- Take the smallest meaningful action
- Instrument it for observation (what will you measure?)
- Don't wait for perfection — iterate

## Speed of the Loop

Boyd's thesis: In adversarial situations, the entity that completes OODA
loops faster gains an insurmountable advantage. The opponent is always
reacting to your previous state, never your current one.

In software: faster feedback loops (test → observe → fix → test) beat
longer planning cycles. A 10-minute debug loop beats a 2-hour analysis
session if the loop runs 15 times.

## When to Apply

- Debugging (observe symptom → orient on cause → decide test → act → repeat)
- Incident response (observe alert → orient on scope → decide fix → deploy)
- Competitive analysis (observe market → orient positioning → decide strategy → execute)
- Iterative development (observe feedback → orient on priority → decide scope → ship)

## Anti-Patterns

- **Skipping Orient**: Acting on raw observations without interpretation
  leads to reactive, context-free decisions
- **Analysis paralysis**: Getting stuck in Orient forever. Orient should
  inform a decision, not replace it
- **Ignoring the loop**: Acting once and stopping. OODA is a continuous
  cycle, not a one-shot process
- **Optimizing Act without fixing Orient**: Going faster with a wrong
  mental model just makes you wrong faster
