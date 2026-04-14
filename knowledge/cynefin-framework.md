# Cynefin Framework

Different problem types require fundamentally different strategies. Using
the wrong strategy for the problem type is worse than doing nothing.

## The Four Domains (Dave Snowden)

### Clear (Obvious)

Cause and effect are obvious. Best practices exist.

- **Strategy**: Sense → Categorize → Respond (identify the pattern, apply SOP)
- **Anti-pattern**: Over-analyzing (wasting time on a solved problem)
- **Example**: "Add a new field to an existing form" — follow the template
- **Execution pace**: Fast. Don't think, just do

### Complicated

Cause and effect exist but require expert analysis to find.

- **Strategy**: Sense → Analyze → Respond (gather data, expert judgment)
- **Anti-pattern**: Applying SOPs (not deep enough)
- **Example**: "Performance degraded after deployment" — profile, analyze, fix
- **Execution pace**: Steady. Measure twice, cut once

### Complex

Cause and effect can only be understood in retrospect.

- **Strategy**: Probe → Sense → Respond (small experiments, observe, amplify
  what works)
- **Anti-pattern**: Detailed upfront planning (impossible to plan for emergent
  behavior)
- **Example**: "Users aren't engaging with the new feature" — A/B test, observe,
  iterate
- **Execution pace**: Exploratory. Small bets, fast feedback loops

### Chaotic

No discernible cause and effect.

- **Strategy**: Act → Sense → Respond (stabilize first, find patterns later)
- **Anti-pattern**: Analysis (no time — act now)
- **Example**: "Production is down, multiple cascading failures" — stop the
  bleeding, investigate after
- **Execution pace**: Urgent. Stabilize, then think

## Domain Detection (First Step for Any Task)

| Signal | Domain | Action |
| --- | --- | --- |
| Done this before, template exists | Clear | Apply template |
| Understood but needs investigation | Complicated | Analyze then solve |
| Novel, unpredictable, emergent | Complex | Small experiments |
| Crisis, cascading, no clarity | Chaotic | Stabilize first |

## Integration with Other Frameworks

| Domain | Best Companion Framework |
| --- | --- |
| Clear | Direct execution (no framework needed) |
| Complicated | Three-Layer Thinking (L1→L2→L3) |
| Complex | OODA Loop (fast iteration) + Pre-Mortem |
| Chaotic | Act first, then Inversion (eliminate worst outcomes) |

## Common Mistake: Domain Confusion

The most dangerous error is treating a Complex problem as Complicated
(or vice versa). Complex problems cannot be solved by analysis alone —
they require experimentation. Complicated problems should not be handled
by experimentation — they need systematic analysis.

## When to Apply

- Task intake: Before starting, classify the problem domain
- Incident response: Determines whether to analyze or act first
- Architecture decisions: Novel (Complex) vs. well-understood (Complicated)
- Debugging: Is the bug reproducible (Complicated) or intermittent (Complex)?
