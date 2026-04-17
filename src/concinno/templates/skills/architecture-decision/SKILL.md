---
name: architecture-decision
description: Architecture Decision Records — lightweight decision logging, trade-off analysis. Triggers on "architecture decision", "架構決策", "ADR", "trade-off record", "技術選型", "why did we choose".
user-invocable: true
disable-model-invocation: true
---

# /architecture-decision — Architecture Decision Record

I record decisions at the moment of maximum context. Future engineers deserve to know why, not just what.

> **You MUST** record the decision within the same session it was made.
> **You MUST** include rejected alternatives with specific reasons.
> **You MUST** link to relevant code, PRs, or RFCs.

## Usage

```
/architecture-decision              — Record current decision interactively
/architecture-decision <topic>      — Record decision on specific topic
/architecture-decision --list       — List all ADRs in project
```

## ADR Format (Lightweight)

```markdown
# ADR-NNN: <Title>

**Date**: YYYY-MM-DD
**Status**: Accepted | Superseded by ADR-XXX | Deprecated

## Context
<Why this decision was needed — the forces at play>

## Decision
<What we decided — one clear statement>

## Alternatives
- **<Option A>**: Rejected because <reason>
- **<Option B>**: Rejected because <reason>

## Consequences
- <What changes as a result>
- <What we gain>
- <What we give up>
```

## Storage

- Default: `docs/adr/` directory, numbered sequentially
- Index: `docs/adr/README.md` with title + status table
- Link from code comments: `// See ADR-NNN for rationale`
