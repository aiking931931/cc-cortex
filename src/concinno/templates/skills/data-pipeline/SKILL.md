---
name: data-pipeline
description: ETL/ELT, data processing, streaming, and batch pipeline patterns. Triggers on "data pipeline", "資料管線", "ETL", "ELT", "batch processing", "streaming", "data engineering".
user-invocable: true
disable-model-invocation: true
---

# /data-pipeline — Data Pipeline Patterns

I build pipelines that are idempotent, observable, and recoverable. Data that arrives late is better than data that arrives wrong.

> **You MUST** make every step idempotent — rerun without side effects.
> **You MUST** validate data at ingestion (schema + nulls + ranges).
> **You MUST** implement dead letter queues for failed records.

## Decision Tree

```
Processing model?
  ├─ Batch (hourly/daily) → Airflow / Dagster / cron + scripts
  ├─ Micro-batch (minutes) → Spark Structured Streaming
  ├─ Real-time (seconds) → Kafka + Flink / consumer
  └─ Event-driven → Lambda / Cloud Functions + queue

Data volume?
  ├─ <1GB/day → PostgreSQL + Python scripts
  ├─ 1-100GB/day → Spark / DuckDB + parquet
  ├─ >100GB/day → Distributed (Spark/Flink + object storage)
  └─ Streaming → Kafka partitions scaled to throughput
```

## Pipeline Principles

1. **Idempotent**: Same input → same output, every time
2. **Schema-on-write**: Validate before storing, not after
3. **Incremental**: Process only new/changed data (watermark/CDC)
4. **Observable**: Row counts, latency, error rates per stage
5. **Recoverable**: Replay from checkpoint, not from scratch
