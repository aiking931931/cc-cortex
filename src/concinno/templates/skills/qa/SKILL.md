---
name: qa
description: Use when the user wants QA testing, browser testing, visual verification, or bug hunting on changed code. Triggers on keywords like "qa", "QA", "測試", "驗收", "browser test".
user-invocable: true
---

# /qa — Diff-Aware QA (Pipeline Step 5)

I test what changed, not everything. Every bug gets an atomic fix and a regression test.

## Purpose

Targeted QA on changed code: identify what changed → test affected paths → fix bugs atomically → add regression tests. Playwright for UI, pytest/vitest for logic.

## Pipeline Context

This is **Step 5** of the Think→Plan→Build→Review→Test→Ship pipeline.
- **Reads**: `.claude/pipeline-state.json` for review findings and exit criteria
- **Writes**: Updates pipeline state with QA results

## Arguments

```
/qa                  — QA all current git changes (diff-aware)
/qa <file>           — QA specific file/component
/qa --visual         — Visual regression (screenshot comparison)
/qa --full           — Full test suite (not diff-aware)
```

## Execution Flow

### 1. Identify Change Scope

```bash
git diff --name-only HEAD  # What files changed
git diff --stat HEAD       # How much changed
```

Classify changes:
- **Frontend** (.tsx/.jsx/.vue/.svelte/.html/.css) → Browser QA path
- **Backend** (.py/.ts server/.go) → Unit/Integration QA path
- **Config** (.json/.yaml/.env/.toml) → Smoke test path
- **Mixed** → Both paths

### 2. Generate Test Plan

Based on change scope, generate targeted test plan:

```
## QA Plan

**Changed**: <N files, M lines>
**Type**: Frontend / Backend / Mixed
**Risk Areas**: <components/functions affected>

### Tests to Run
1. [ ] <existing test that covers changed code>
2. [ ] <new scenario to verify the change>
3. [ ] <edge case / regression check>
```

### 3. Execute Tests

#### Backend Path
```bash
# Run existing tests that cover changed files
pytest <changed_files> -v --tb=short
# or
npx vitest run <changed_files>
```

#### Frontend Path (Visual + Maestro)
```bash
# Priority 1: Maestro (mobile/web black-box testing)
# Check: which maestro || maestro --version
# If available and .maestro/ flows exist:
maestro test .maestro/<relevant_flow>.yaml

# Priority 2: Playwright (browser automation)
npx playwright test <relevant_spec>

# Priority 3: Project screenshot tool
node scripts/tools/psyche-screenshot.js <url> <output>
```

#### Maestro Flow Generation (if no .maestro/ exists)
```bash
# Auto-generate from change scope:
mkdir -p .maestro
# Write YAML flow based on changed components
# See /tdd SKILL.md "Maestro Integration" for flow format
maestro test .maestro/<feature>.yaml
```

#### Smoke Path (Config changes)
```bash
# Type check
npx tsc --noEmit  # or mypy / ruff check
# Build check
npm run build  # or python -m build
```

### 4. Bug Handling (Per Bug)

For each bug found:

1. **Document**:
   ```
   ### BUG-1: <title>
   **File**: `path:line`
   **Symptom**: <what's wrong>
   **Root Cause**: <why>
   ```

2. **Fix**: Atomic edit (smallest possible change)

3. **Commit**: One commit per bug
   ```
   fix(<scope>): <what was fixed>
   ```

4. **Regression Test**: Add test that catches this specific bug
   ```
   test(<scope>): add regression test for BUG-1
   ```

### 5. QA Report

```
## QA Report

**Scope**: <N files tested>
**Tests Run**: <M tests>
**Tests Passed**: <P> / <M>
**Bugs Found**: <B>
**Bugs Fixed**: <F> / <B>
**Regression Tests Added**: <R>

**Verdict**: PASS / FAIL

### Exit Criteria Verification (if /think was run)
- [x] <criterion 1> — verified by <test/screenshot>
- [ ] <criterion 2> — NOT verified, reason: <...>
```

### 6. Pipeline State Update

```json
{
  "current_phase": "qa",
  "qa": {
    "tests_run": N,
    "tests_passed": P,
    "bugs_found": B,
    "bugs_fixed": F,
    "verdict": "PASS|FAIL",
    "exit_criteria_status": {...}
  },
  "next_suggested": "/ship (if PASS) or fix remaining (if FAIL)"
}
```

## Guard Integration

- **UIVerifyGuard**: Frontend changes trigger mandatory visual verification
- **CodeGuard**: Lint debt must be zero before QA passes
- **DeliveryGate**: Exit criteria from /think are verified here

## Anti-Patterns

- Do NOT run full test suite when only 2 files changed — diff-aware first
- Do NOT fix bugs in batch — one bug, one fix, one commit, one test
- Do NOT skip regression tests — the bug you don't test will return
- Do NOT mark PASS if any exit criterion is unverified
