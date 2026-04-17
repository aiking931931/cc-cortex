---
name: database-patterns
description: Database design, indexing, migration, and query optimization patterns. Triggers on "database", "資料庫", "SQL", "migration", "indexing", "query optimization", "schema design".
user-invocable: true
disable-model-invocation: true
---

# /database-patterns — Database Design Patterns

I design schemas that tell the truth about the domain. Every index earns its keep.

> **You MUST** include rollback strategy for every migration.
> **You MUST** explain EXPLAIN output before claiming "optimized".
> **You MUST** never delete columns in production — deprecate first, remove in next release.

## Decision Tree

```
Data model?
  ├─ Relational + ACID → PostgreSQL
  ├─ Document + flexible schema → MongoDB
  ├─ Key-value + speed → Redis
  ├─ Time series → TimescaleDB / InfluxDB
  └─ Graph → Neo4j / PostgreSQL + recursive CTE

Index needed?
  ├─ WHERE clause column → B-tree (default)
  ├─ Full text search → GIN + tsvector
  ├─ JSON field query → GIN
  ├─ Range/timestamp → BRIN
  └─ Rarely queried → No index (writes matter)
```

## Migration Safety

1. **Add** columns as nullable first
2. **Backfill** data in batches (≤10k rows/batch)
3. **Add** NOT NULL constraint after backfill
4. **Deploy** code that reads new column
5. **Drop** old column in separate migration (next release)
