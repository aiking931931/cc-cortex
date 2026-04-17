---
name: tdd
description: Use when the user wants test-driven development, red-green-refactor cycles, or to build a feature test-first. Triggers on keywords like "tdd", "TDD", "測試驅動", "test first", "紅綠", "red green".
user-invocable: true
---

# /tdd — Test-Driven Development (Pipeline Step 3)

I write the test first. The test defines the contract. The code fulfills it.

## Purpose

Red-Green-Refactor cycle: write a failing test → write minimal code to pass → refactor. One slice at a time. Inspired by Pocock's `/tdd` — enhanced with vertical slice enforcement and Guard Pipeline integration.

## Pipeline Context

This is **Step 3** of the Think→PRD→**TDD/Build**→Review→QA→Ship pipeline.
- **Reads**: `.claude/pipeline-state.json` for PRD slices (if exists)
- **Writes**: Tests + implementation + pipeline state

## Arguments

```
/tdd                      — Start TDD on current PRD slice 1 (or ask what to build)
/tdd <feature>            — TDD a specific feature/function
/tdd --slice <N>          — TDD specific PRD slice number
/tdd --continue           — Resume from last red/green checkpoint
/tdd --cycle-only         — One red-green-refactor cycle then stop
```

## Execution Flow

### 0. Setup — Detect Test Framework

Auto-detect from project:

| Signal | Framework | Run Command |
|--------|-----------|-------------|
| `vitest.config.*` / `vite.config.*` | vitest | `npx vitest run` |
| `jest.config.*` | jest | `npx jest` |
| `pytest.ini` / `pyproject.toml [tool.pytest]` | pytest | `python -m pytest` |
| `Cargo.toml` | cargo test | `cargo test` |
| `*.test.ts` pattern | vitest (default TS) | `npx vitest run` |
| `*_test.py` pattern | pytest (default PY) | `python -m pytest` |

If ambiguous, ask ONE question: "Which test framework? (vitest/pytest/jest/other)"

### 1. Define the Contract

**If PRD exists**: Load slice N's acceptance criteria → these become test cases.
**Otherwise**: Ask "What should this function/component DO?" → derive test cases.

Output before writing any code:

```
## TDD Contract: <feature/slice>

### Test Cases (derived from acceptance criteria):
1. ✅ Given <precondition>, when <action>, then <expected>
2. ✅ Given <edge case>, when <action>, then <expected>
3. ✅ Given <error condition>, when <action>, then <error handling>

### NOT testing (out of scope):
- <explicitly excluded scenarios>
```

### 2. RED — Write Failing Tests

Write ALL test cases from the contract. They MUST fail.

```bash
# Run tests — expect ALL RED
npx vitest run <test-file> --reporter=verbose
```

Verify: every test fails with the RIGHT reason (missing function, not syntax error).

**Commit checkpoint:**
```
test(<scope>): add failing tests for <feature> [RED]
```

### 3. GREEN — Minimal Implementation

Write the MINIMUM code to make tests pass. Rules:
- **No premature abstraction** — hardcode if one test, generalize when forced by second test
- **No extra features** — if no test requires it, don't write it
- **One test at a time** — make test 1 pass, then test 2, then test 3

```bash
# Run after each change — watch tests go green one by one
npx vitest run <test-file> --reporter=verbose
```

When ALL green:

**Commit checkpoint:**
```
feat(<scope>): implement <feature> [GREEN]
```

### 4. REFACTOR — Clean Without Breaking

Now improve the code:
- Extract duplication
- Rename for clarity
- Optimize hot paths
- Apply design patterns (only if justified by actual complexity)

**Rule**: Tests must stay green throughout refactor. Run after every change.

```bash
npx vitest run <test-file> --reporter=verbose
```

**Commit checkpoint:**
```
refactor(<scope>): clean up <feature> [REFACTOR]
```

### 5. Cycle Complete — Next Slice or Done

Check pipeline state:
- More slices? → "Slice N done. Starting Slice N+1." → Go to Step 1
- All slices done? → "All slices complete. Run `/review` for code review."
- `--cycle-only`? → Stop after one cycle

### 6. Pipeline State Update

```json
{
  "current_phase": "tdd",
  "timestamp": "<ISO 8601>",
  "feature": "<feature name>",
  "tdd": {
    "current_slice": N,
    "completed_slices": [1, 2],
    "test_files": ["src/__tests__/feature.test.ts"],
    "implementation_files": ["src/feature.ts"],
    "cycle_state": "green",
    "total_tests": 12,
    "passing": 12
  },
  "next_suggested": "/review (if all slices done) or /tdd --slice N+1"
}
```

## Test Placement Convention

Follow project convention. If none exists:

| Project Type | Test Location | Naming |
|-------------|--------------|--------|
| TypeScript/JS | `src/__tests__/` or colocated `*.test.ts` | `<module>.test.ts` |
| Python | `tests/` | `test_<module>.py` |
| Rust | Same file (`#[cfg(test)]`) or `tests/` | `test_<module>.rs` |

## Guard Integration

- **CodeGuard**: Lint runs after every edit (ruff/eslint/tsc)
- **UIVerifyGuard**: If tests touch UI files → deploy+screenshot after GREEN
- **ProposalGuard**: Test cases must trace back to PRD acceptance criteria
- **DesignTheoryGuard**: Refactor phase checked for Deep Module principles

## Maestro Integration (Mobile/Web UI)

If the slice involves UI and Maestro CLI is installed:

```bash
# Check if maestro is available
which maestro || maestro --version

# Generate flow test from acceptance criteria
# Write to .maestro/<feature>.yaml
```

Maestro flow format:
```yaml
appId: <from package.json or detect>
---
- launchApp
- tapOn: "<element text or id>"
- assertVisible: "<expected text>"
- takeScreenshot: "verify_<feature>"
```

Run: `maestro test .maestro/<feature>.yaml`

## Anti-Patterns

- Do NOT write implementation before tests — that's not TDD, that's retroactive testing
- Do NOT write tests that test implementation details — test BEHAVIOR
- Do NOT refactor while RED — get green first, then clean
- Do NOT skip the refactor step — "it works" ≠ "it's good"
- Do NOT write 20 tests before any implementation — max 5 per cycle, then implement
- Do NOT mock everything — mock boundaries (APIs, DBs), test logic directly
