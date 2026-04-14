---
name: think
description: Use when the user wants to challenge requirements before coding, define what to build, or run an office-hours style requirements session. Triggers on keywords like "think", "需求", "想清楚", "辦公時間".
user-invocable: true
---

# /think — Requirements Challenge (Pipeline Step 1)

I don't build until I know what I'm building. The cheapest bug to fix is the one never written.

## Purpose

Challenge and refine requirements **before** any code is written. Inspired by gstack's `/office-hours` — but with Guard Pipeline integration and learning loop.

## Pipeline Context

This is **Step 1** of the Think→Plan→Build→Review→Test→Ship pipeline.
- **Reads**: Nothing (entry point)
- **Writes**: `.claude/pipeline-state.json` with `think` phase output

## Execution Flow

### 1. Load Context
- Read `$ARGUMENTS` for the requirement/feature description
- If no arguments: ask ONE question — "What are you trying to build, and for whom?"
- Read `.claude/pipeline-state.json` if exists (check for prior think sessions on same topic)

### 2. Challenge Requirements (5-Question Framework)

Ask these sequentially, stop when answers are clear:

| # | Question | Why |
|---|----------|-----|
| 1 | **Who benefits?** Target user/persona | Prevents building for nobody |
| 2 | **What's the success signal?** How do we know it works? | Forces binary exit criteria |
| 3 | **What do we NOT build?** Explicit scope boundary | Prevents scope creep |
| 4 | **What breaks if we get it wrong?** Blast radius | Calibrates effort level |
| 5 | **Is there prior art?** Existing code/pattern to leverage | Prevents reinventing |

### 3. Synthesize Requirements Doc

After Q&A, output a structured requirements block:

```
## Requirements: <Feature Name>

**Target**: <who>
**Success Signal**: <binary pass/fail>
**Scope Boundary**: <what's out>
**Blast Radius**: <what breaks if wrong>
**Prior Art**: <existing code/patterns>
**Exit Criteria**:
  - [ ] <criterion 1>
  - [ ] <criterion 2>
  ...
```

### 4. Save Pipeline State

Write to `.claude/pipeline-state.json`:

```json
{
  "current_phase": "think",
  "timestamp": "<ISO 8601>",
  "feature": "<feature name>",
  "requirements": {
    "target": "...",
    "success_signal": "...",
    "scope_boundary": "...",
    "blast_radius": "...",
    "prior_art": "...",
    "exit_criteria": ["..."]
  },
  "next_suggested": "/review or direct implementation"
}
```

### 5. Suggest Next Step

Based on complexity:
- **Simple** (1-2 exit criteria, low blast radius): "Requirements clear. Start building."
- **Medium** (3-5 criteria): "Consider `/review` after implementation for a second pass."
- **Complex** (5+ criteria, high blast radius): "Recommend Plan mode before coding."

## Guard Integration

- **ProposalGuard** will enforce that plans reference exit criteria from this phase
- **DeliveryGate** will use exit criteria as verification checklist at ship time

## Anti-Patterns

- Do NOT ask all 5 questions if the first 2 make the scope obvious
- Do NOT write code during /think — this is pure requirements
- Do NOT repeat what the user said — challenge and refine it
