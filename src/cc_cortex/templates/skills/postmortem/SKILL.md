---
name: postmortem
description: Post-incident analysis and blameless retrospective. Triggers on "postmortem", "事後分析", "retrospective", "root cause analysis", "RCA", "回顧".
user-invocable: true
---

# /postmortem — Blameless Post-Incident Analysis

I write postmortems that prevent recurrence, not assign blame. Systems fail, not people.

> **You MUST** be blameless — focus on systems and processes, not individuals.
> **You MUST** include concrete action items with owners and deadlines.
> **You MUST** complete within 48 hours of incident resolution.

## Template

```markdown
# Postmortem: <Incident Title>

**Date**: YYYY-MM-DD
**Severity**: P0/P1/P2
**Duration**: Xh Ym (HH:MM–HH:MM UTC)
**Author**: <name>
**Status**: DRAFT → REVIEWED → COMPLETE

## Summary
<2-3 sentences: what happened, who was affected, how it was resolved>

## Impact
- Users affected: N
- Revenue impact: $X or N/A
- Data loss: Yes/No

## Timeline (UTC)
| Time | Event |
|------|-------|
| HH:MM | First alert fired |
| HH:MM | On-call acknowledged |
| HH:MM | Root cause identified |
| HH:MM | Mitigation applied |
| HH:MM | Service restored |

## Root Cause
<Technical explanation of why it happened>

## Contributing Factors
1. <Factor that made it worse or harder to detect>

## What Went Well
1. <Things that worked during response>

## What Went Wrong
1. <Things that didn't work>

## Action Items
| # | Action | Owner | Deadline | Status |
|---|--------|-------|----------|--------|
| 1 | <specific action> | @name | YYYY-MM-DD | TODO |
```
