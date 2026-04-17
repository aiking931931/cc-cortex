---
name: rfc
description: RFC/ADR document generation — architecture decisions, design proposals. Triggers on "RFC", "ADR", "design doc", "設計文件", "architecture decision", "proposal".
user-invocable: true
---

# /rfc — RFC / Architecture Decision Record

I document decisions so future engineers know why, not just what. A decision without context is a trap.

> **You MUST** include "Alternatives Considered" with at least 2 rejected options.
> **You MUST** include "Consequences" (both positive and negative).
> **You MUST** assign a status: DRAFT → REVIEW → ACCEPTED / REJECTED.

## Template

```markdown
# RFC-NNN: <Title>

**Status**: DRAFT | REVIEW | ACCEPTED | REJECTED
**Author**: <name>
**Date**: <YYYY-MM-DD>
**Deciders**: <who approves>

## Context
<What problem are we solving? Why now?>

## Decision
<What we chose to do>

## Alternatives Considered
### Option A: <name>
- Pros: ...
- Cons: ...

### Option B: <name>
- Pros: ...
- Cons: ...

## Consequences
### Positive
- ...
### Negative
- ...
### Risks
- ...

## Implementation Plan
1. ...
```

## Usage
```
/rfc                    — Generate RFC from current discussion
/rfc <topic>            — Generate RFC for specific topic
/rfc --list             — List existing RFCs in project
```
