---
name: kb_performance
description: Performance optimization knowledge base — profiling, caching, bundle size, database queries, memory. Triggers on "效能", "performance", "profiling", "optimization", "slow", "latency", "bundle size", "memory leak".
user-invocable: false
---

# 效能優化知識庫

> I optimize what matters, not what's easy to measure. Premature optimization is the root of all evil, but mature optimization is the root of all speed.

> **You MUST** measure before optimizing — gut feeling is not a profiler.
> **You MUST** benchmark with production-like data, not toy datasets.
> **You MUST** document optimization rationale — clever code without context is tech debt.

## 鐵律

1. **Profile first** — Find the bottleneck before writing faster code
2. **80/20 rule** — 20% of code causes 80% of latency
3. **Cache wisely** — Wrong cache invalidation is worse than no cache
4. **Async for I/O** — Never block event loop with sync I/O

## 決策樹

```
Bottleneck where?
  ├─ Database → EXPLAIN ANALYZE → index / query rewrite / read replica
  ├─ Network → Batch requests / connection pool / CDN
  ├─ CPU → Algorithm change / parallel / native extension
  ├─ Memory → Streaming / pagination / weak references
  └─ Frontend → Code split / lazy load / virtualize lists

Caching strategy?
  ├─ Read-heavy, rarely changes → Cache-aside + long TTL
  ├─ Read-heavy, frequent changes → Cache-aside + short TTL + invalidation
  ├─ Write-heavy → Write-through or write-behind
  └─ Computed results → Memoize with LRU
```

## 按需讀取

| 要做什麼 | 讀哪個檔案 |
| --- | --- |
| 資料庫優化 | `/database-patterns` skill |
| 前端效能 | `/frontend-patterns` skill |
| 監控設定 | `/monitoring` skill |
