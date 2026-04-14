# Pre-Mortem Analysis

Before starting, assume the project has already failed. Then work backward
to identify what caused the failure. This surfaces risks that optimism bias
would otherwise hide.

## The Process (Gary Klein, 2007)

### 1. Assume Failure

"It is six months from now. This project has failed completely.
What happened?"

### 2. Generate Causes Independently

Each perspective (or each mental pass, for solo work) generates failure
causes independently. Don't filter — quantity over quality at this stage.

Categories to consider:
- **Technical**: Wrong technology choice, scalability limits, integration failures
- **Process**: Unclear requirements, scope creep, communication breakdown
- **Resource**: Key person unavailable, budget cut, time underestimated
- **External**: Market shift, platform change, competitor move, legal issue
- **Human**: Burnout, skill gap, misaligned incentives, user rejection

### 3. Prioritize

For each cause, assess:
- **Likelihood**: How probable is this failure mode? (1-5)
- **Impact**: How devastating if it occurs? (1-5)
- **Detectability**: How late would we notice? (1=early, 5=too late)

Risk score = Likelihood × Impact × Detectability

### 4. Mitigate Top Risks

For the top 3–5 risks:
- Define a concrete prevention action
- Define a detection mechanism (how would you know it's happening?)
- Define a recovery plan (what if prevention fails?)

## When to Apply

- Project kickoff (before committing resources)
- Architecture design (before writing code)
- Release planning (before shipping)
- Major refactors (before touching load-bearing code)

## Why Pre-Mortem > Post-Mortem

Post-mortems learn from past failures. Pre-mortems prevent future ones.

Post-mortems suffer from hindsight bias ("of course that would fail").
Pre-mortems leverage prospective hindsight — imagining a future failure
makes it vivid and concrete, bypassing optimism bias.

Research (Klein, 2007): Teams using pre-mortems identified 30% more
potential risks than teams using standard risk assessment.

## Anti-Patterns

- **Optimism override**: "That won't happen to us" — the whole point is
  to overcome this bias
- **Too many risks**: Focus on top 3–5. Tracking 50 risks is tracking none
- **No mitigation**: Identifying risks without action plans is theater
- **Solo bias**: Even in solo work, force yourself through multiple
  perspectives (technical, user, business, adversarial)
