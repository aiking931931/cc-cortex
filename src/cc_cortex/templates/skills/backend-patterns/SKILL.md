---
name: backend-patterns
description: Backend architecture patterns — API design, auth, caching, queues, error handling. Triggers on "backend", "後端", "API pattern", "server architecture", "caching strategy".
user-invocable: false
---

# /backend-patterns — Backend Architecture Patterns

I build backends that fail gracefully, scale predictably, and communicate clearly.

> **You MUST** match pattern to actual traffic/scale requirements — no premature optimization.
> **You MUST** verify error paths, not just happy paths.
> **You MUST** consider idempotency for any state-changing operation.

## Decision Tree

```
Need → API design?
  ├─ CRUD → REST with resource naming
  ├─ Real-time → WebSocket / SSE
  ├─ Complex queries → GraphQL
  └─ Internal high-perf → gRPC

Need → Data consistency?
  ├─ Strong → DB transaction + optimistic locking
  ├─ Eventual → Event queue + idempotent consumer
  └─ Cache → Cache-aside with TTL + invalidation
```

## Patterns Catalog

| Pattern | When | Anti-pattern |
|---------|------|-------------|
| Repository | DB access abstraction | Raw SQL in handlers |
| Circuit Breaker | External service calls | Unbounded retries |
| Saga | Distributed transactions | 2PC across services |
| CQRS | Read/write asymmetry | Over-engineering simple CRUD |
| Rate Limiter | Public APIs | No limits on any endpoint |
| Bulkhead | Fault isolation | Shared thread pool for all |
