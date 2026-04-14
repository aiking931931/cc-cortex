---
name: review
description: Use when the user wants a staff-engineer level code review, architecture review, or PR review. Triggers on keywords like "review", "審查", "code review", "看一下代碼".
user-invocable: true
---

# /review — Staff Engineer Code Review (Pipeline Step 4)

I review as a builder who knows what's load-bearing. Every finding cites file:line.

## Purpose

Two-stage code review: first CRITICAL (must-fix), then INFORMATIONAL (nice-to-have). Every finding must cite `file:line`. No vague suggestions.

## Pipeline Context

This is **Step 4** of the Think→Plan→Build→Review→Test→Ship pipeline.
- **Reads**: `.claude/pipeline-state.json` for exit criteria (if available)
- **Writes**: Updates pipeline state with review findings

## Arguments

```
/review              — Review git diff (staged + unstaged changes)
/review <file>       — Review specific file
/review <PR#>        — Review pull request (via gh)
/review --arch       — Architecture review (structure, dependencies, patterns)
```

## Execution Flow

### 1. Gather Scope

Based on `$ARGUMENTS`:
- No args: `git diff HEAD` + `git diff --cached` — review all current changes
- File path: Read and review that file
- PR number: `gh pr diff <#>` — review PR changes
- `--arch`: Glob project structure, read key files, analyze architecture

### 2. Stage 1 — CRITICAL (Must-Fix)

Findings that **block shipping**. Each finding must include:

```
### CRITICAL-1: <title>

**File**: `path/to/file.ts:42`
**Issue**: <what's wrong>
**Impact**: <what breaks>
**Fix**: <specific action>
```

Categories:
- **Security**: injection, secrets, auth bypass, OWASP top 10
- **Correctness**: logic errors, race conditions, missing error handling
- **Data Loss**: unprotected writes, missing rollback, cascade deletes
- **Breaking Change**: API contract violation, removed public interface

### 3. Stage 2 — INFORMATIONAL (Nice-to-Have)

Findings that **improve quality** but don't block. Same format, numbered `INFO-1`, `INFO-2`, etc.

Categories:
- **Performance**: N+1 queries, unnecessary re-renders, missing memoization
- **Maintainability**: deep nesting, god functions, missing types
- **Convention**: naming, file organization, import order
- **Testing**: missing edge cases, flaky test patterns

### 4. Summary

```
## Review Summary

**Scope**: <N files, M lines changed>
**CRITICAL**: <count> findings
**INFORMATIONAL**: <count> findings
**Verdict**: SHIP / REVISE / BLOCK

<one-line rationale>
```

Verdicts:
- **SHIP**: 0 CRITICAL findings
- **REVISE**: 1-3 CRITICAL findings, all fixable in current session
- **BLOCK**: 4+ CRITICAL or any architectural issue requiring redesign

### 5. Pipeline State Update

```json
{
  "current_phase": "review",
  "review": {
    "critical_count": N,
    "informational_count": M,
    "verdict": "SHIP|REVISE|BLOCK",
    "findings": [...]
  },
  "next_suggested": "/qa (if SHIP) or fix criticals (if REVISE)"
}
```

### 6. Auto-Fix Offer (REVISE only)

If verdict is REVISE and all CRITICAL findings are mechanical (not design issues):
- Offer to fix them immediately
- Each fix = atomic commit with descriptive message
- Re-run review on fixed code to confirm SHIP

## Guard Integration

- **CognitiveAnchor**: Large deletions in reviewed code trigger "verify intent" anchor
- **SSOTGuard**: Design token violations surface as CRITICAL findings
- **StructuralGuard**: Function length/nesting violations surface as INFORMATIONAL

## Anti-Patterns

- Do NOT give feedback without file:line reference
- Do NOT mix CRITICAL and INFORMATIONAL — always stage 1 then stage 2
- Do NOT review your own just-written code as SHIP — builder bias is real
- Do NOT suggest rewrites in INFORMATIONAL — that's scope creep
