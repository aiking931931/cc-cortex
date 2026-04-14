---
name: prd
description: Use when the user wants to generate a PRD (Product Requirements Document), convert requirements into a formal spec, or create GitHub Issues from requirements. Triggers on keywords like "prd", "PRD", "需求文件", "需求文檔", "write prd", "拆 issue".
user-invocable: true
---

# /prd — PRD Generation + Issue Decomposition (Pipeline Step 2)

I turn fuzzy ideas into executable specs. Every feature starts as a document, not a branch.

## Purpose

Two-in-one: generate a PRD from /think output (or fresh input), then decompose it into vertical-slice GitHub Issues. Inspired by Pocock's `/write-a-prd` + `/prd-to-issues` — combined because splitting them wastes a round-trip.

## Pipeline Context

This is **Step 2** of the Think→**PRD**→Plan→Build→Review→QA→Ship pipeline.
- **Reads**: `.claude/pipeline-state.json` for /think output (if exists)
- **Writes**: PRD markdown file + pipeline state update + (optional) GitHub Issues

## Arguments

```
/prd                    — Interactive: interview → PRD → issues
/prd <feature>          — Generate PRD for named feature
/prd --from-think       — Use /think output as input (no interview)
/prd --issues           — Also create GitHub Issues (default: PRD only)
/prd --issues --dry-run — Show issues without creating them
```

## Execution Flow

### 1. Gather Input

**If pipeline-state.json has /think output** (`--from-think` or auto-detect):
- Load requirements, exit criteria, target, scope boundary
- Skip to Step 3 (synthesis)

**Otherwise — Interactive Interview** (max 6 questions, stop when clear):

| # | Question | Purpose |
|---|----------|---------|
| 1 | What problem does this solve? | Core value proposition |
| 2 | Who are the users? What do they do today? | User journey baseline |
| 3 | Walk me through the happy path | Primary flow definition |
| 4 | What are the error/edge cases? | Defensive scope |
| 5 | What existing code/APIs does this touch? | Integration surface |
| 6 | What does "done" look like? | Acceptance criteria |

**Codebase scan** (parallel with interview):
- `Grep` for related modules, routes, components
- `Read` key files the feature touches
- Identify integration points and potential conflicts

### 2. Read Existing Patterns

Before writing, scan the codebase:
```
- Package structure (monorepo? packages?)
- Test patterns (vitest? pytest? playwright?)
- Existing PRD/spec files (match format if found)
- Component/module naming conventions
```

### 3. Synthesize PRD

Write to `docs/prd/<feature-slug>.md` (or project's doc directory):

```markdown
# PRD: <Feature Name>

## Problem Statement
<1-2 sentences: what problem, for whom>

## User Stories
- As a <user>, I want <action> so that <benefit>
- ...

## Happy Path
1. User does X
2. System responds with Y
3. ...

## Technical Design
### Integration Points
- <module/API that changes>
- <new module needed>

### Data Model Changes
- <schema changes if any>

### Dependencies
- <external libs/services needed>

## Edge Cases & Error Handling
| Case | Expected Behavior |
|------|------------------|
| ... | ... |

## Acceptance Criteria
- [ ] <criterion 1> (from /think exit criteria if available)
- [ ] <criterion 2>
- ...

## Out of Scope
- <explicitly excluded items>

## Open Questions
- <unresolved decisions>
```

### 4. Vertical Slice Decomposition

Break PRD into independent, shippable slices. Each slice must:
- Be deployable on its own (no half-features)
- Have a clear acceptance criterion
- Be estimable (S/M/L)

Rules:
- **Max 7 issues** per PRD (if more needed, PRD is too big — split the PRD)
- **Each issue is vertical**: touches all layers needed (DB→API→UI)
- **Dependencies are explicit**: if Issue 3 needs Issue 1, say so
- **First issue is always the smallest** — unblock the team fast

Output format:
```
## Implementation Plan

### Slice 1: <title> [S]
- **What**: <description>
- **Acceptance**: <criterion>
- **Files**: <likely files to touch>
- **Depends on**: none

### Slice 2: <title> [M]
- **What**: <description>
- **Acceptance**: <criterion>
- **Files**: <likely files to touch>
- **Depends on**: Slice 1
...
```

### 5. Create GitHub Issues (if --issues)

```bash
gh issue create \
  --title "[PRD:<feature>] Slice N: <title>" \
  --body "$(cat <<'EOF'
## Context
Part of PRD: `docs/prd/<feature-slug>.md`

## What
<description>

## Acceptance Criteria
- [ ] <criterion>

## Technical Notes
- Files: <likely files>
- Dependencies: <issue links>

## Size: S/M/L

Generated from `/prd` pipeline
EOF
)"
```

### 6. Pipeline State Update

```json
{
  "current_phase": "prd",
  "timestamp": "<ISO 8601>",
  "feature": "<feature name>",
  "prd_path": "docs/prd/<feature-slug>.md",
  "slices": [
    {"title": "...", "size": "S", "depends_on": [], "issue_url": "..."},
    ...
  ],
  "next_suggested": "Pick Slice 1 and start building, or use Plan mode for complex slices"
}
```

## Guard Integration

- **ProposalGuard**: References PRD acceptance criteria during build
- **DesignTheoryGuard**: Validates vertical slice discipline (no horizontal layers)
- **DeliveryGate**: Maps slices to exit criteria at ship time

## Anti-Patterns

- Do NOT write a 20-page PRD — if it's that long, split the feature
- Do NOT create horizontal slices ("all DB changes", "all UI changes") — every slice is vertical
- Do NOT skip codebase scan — PRD must reference real code, not imagined architecture
- Do NOT create issues without PRD — issues without context become zombie tickets
- Do NOT ask more than 6 questions — if unclear after 6, the feature is too vague to spec
