# Verification Obligation

When you discover you cannot verify something, that inability is a new
problem that must be solved before continuing. Skipping verification is
not saving time — it's creating blind spots that cost more later.

## The Anti-Pattern

1. Fix bug in component A
2. Try to verify → verification method doesn't cover this case
3. Skip verification, move to next task
4. Bug resurfaces (or was never actually fixed)
5. Debug again from scratch — 2x the cost

## The Correct Pattern

1. Fix bug in component A
2. Try to verify → verification method doesn't cover this case
3. **Stop. "Cannot verify" is now the primary problem**
4. Solve the verification gap (new tool, new test, new access path)
5. Verify the original fix
6. Continue to next task — with the verification gap permanently closed

## Core Principle

**Unverified work is unfinished work.** The moment you realize you can't
prove something works, you have two problems:

1. The original problem (the bug/feature)
2. The verification gap (why you can't prove it)

Problem #2 must be solved first, because solving #1 without #2 means
you don't actually know if #1 is solved.

## Common Verification Gaps and Solutions

| Gap | Solution |
|-----|----------|
| Can't see the UI change | Build a screenshot tool / headless browser test |
| Can't test the logged-out state | Add logout-and-screenshot capability |
| Can't reproduce the error | Add logging to capture the exact conditions |
| Can't test on mobile | Add responsive viewport testing |
| Can't verify the API response | Add integration test with real endpoint |
| Can't confirm deployment | Add health check / smoke test |

## Relationship to Other Principles

- **Butterfly Effect**: "Cannot verify" is an anomaly discovered during
  work — must be handled immediately, not skipped
- **Consequence-First**: Unverified changes have unknown consequences —
  shipping them is gambling
- **WIREDO D-dimension**: "Defended & Verified" requires evidence, not
  assumptions. No evidence = D fails = not WIREDO = not done

## Anti-Patterns

- **"It compiled, so it works"**: Compilation ≠ correctness
- **"I'll verify later"**: Later never comes, or the context is lost
- **"The test passes"**: Does the test actually test what you changed?
- **"It worked on my machine"**: Does it work where it matters?
