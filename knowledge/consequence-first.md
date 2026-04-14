# Consequence-First Thinking

Before acting, think about what happens after. Completing a task is not the
same as delivering value. Isolated work that connects to nothing is 0% done.

## The Three Questions (Before Every Deliverable)

1. **Who calls this?** — Is there a consumer (import, API call, UI element)?
   If nobody uses it, it doesn't exist in the system.

2. **What breaks if you remove it?** — If removing it causes no failure,
   it's dead code or orphaned work. Real integration means real dependency.

3. **What breaks if you ship it?** — Side effects, regressions, security
   implications, user confusion. The blast radius matters.

## Second-Order Effects

First-order: "What does this change do?"
Second-order: "What does this change *cause*?"

Every action creates ripples. The skill is estimating how far they travel:

- **Code change** → Does it break existing tests? APIs? Downstream consumers?
- **Architecture decision** → Does it constrain future options? Create tech debt?
- **Process change** → Does it slow down the team? Create bottlenecks?
- **Deletion** → Is anything silently depending on the deleted artifact?

## The Honesty Principle

- Report 30% real progress rather than 95% inflated progress
- Mark incomplete work as incomplete (⏸), never as done (✅)
- "I don't know" is more valuable than a confident wrong answer
- Verify before claiming — grep before asserting a function exists

## Side-Effect Analysis Template

For every proposed change:

```
Change: ___
Solves: ___
Side effects:
  1. ___ (severity: low/med/high, mitigation: ___)
  2. ___ (severity: low/med/high, mitigation: ___)
Net value: [positive/negative/uncertain]
```

## Anti-Patterns

- **Task completion theater**: Marking things done without integration testing
- **Orphan code**: Functions nobody calls, modules nobody imports
- **Fix-one-break-three**: Solving the immediate problem while creating
  downstream failures
- **Optimistic reporting**: "It works on my machine" without system-level
  verification

## Integration

- Three-Layer Thinking L2 (Sweet Spot) uses consequence analysis for
  side-effect comparison
- WIREDO's "W — Wired" dimension enforces the "who calls this?" check
- Pre-Mortem analysis extends consequence thinking into failure scenarios
