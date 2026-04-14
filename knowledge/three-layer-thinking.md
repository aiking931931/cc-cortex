# Three-Layer Thinking

Every decision passes through three layers: root cause, sweet spot, strategy.
Don't jump to solutions. Don't optimize before understanding.

## Layer 1: Root Cause (What's actually broken?)

Find the real problem, not the symptom.

- **Ask "why" until you hit bedrock** — the point where the answer is a
  physical constraint, not a design choice
- **Negative evidence**: What *should* be present but isn't? What *should*
  happen but doesn't? Absence is data
- **Separate cure from bandaid**: Fixing the root eliminates the class of
  problem. Fixing the symptom requires eternal maintenance
- **Never compare cure and bandaid on single-instance cost** — a bandaid
  seems cheaper per application, but total cost over time is infinite

## Layer 2: Sweet Spot (What's the best trade-off?)

Given the root cause, find the solution with the highest value per unit cost.

- **Side effects matter**: Every solution has costs — execution time, token
  budget, maintenance burden, blast radius, cognitive load
- **Time cost discipline**: Before writing a script, ask "is there a
  5-second solution?" An API call beats a polling loop. Parallel beats serial
- **Compare within class**: Compare cures against cures, bandaids against
  bandaids. Never cross-compare (one-time surgery vs. daily medication)
- **The simplest cure wins**: Among solutions that fix the root cause, pick
  the one with the fewest side effects

## Layer 3: Strategy Enhancement (Only when stuck)

When L1+L2 don't resolve, escalate to structured reasoning.

- Default: Trust natural reasoning. Most problems don't need frameworks
- **Stuck 2+ rounds**: Apply Step-Back prompting or switch strategy entirely
- **Search/comparison**: Tree of Thoughts
- **Complex multi-step**: Decompose → sub-problems → recompose
- **Logic chain**: Chain of Thought with explicit steps
- **Reference**: Prompt engineering strategy index

## Quick Decision Template

```
L1: Root cause = ___
    Cure possible? [yes → cure] [no → best bandaid]
L2: Options:
    A: ___ (cost: ___, side effects: ___)
    B: ___ (cost: ___, side effects: ___)
    → Pick: ___
L3: (only if stuck) Strategy = ___
```

## Anti-Patterns

- **Jumping to L2 without L1**: Optimizing the wrong solution
- **Comparing cures and bandaids**: "The bandaid is faster" — irrelevant
  if the problem will recur 100 times
- **L3 overuse**: Frameworks are tools, not crutches. Most decisions need
  only L1 + L2
