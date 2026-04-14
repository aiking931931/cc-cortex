---
name: monitoring
description: Observability patterns — logging, metrics, tracing, alerting, dashboards. Triggers on "monitoring", "監控", "observability", "logging", "metrics", "alerting", "tracing".
user-invocable: true
disable-model-invocation: true
---

# /monitoring — Observability Patterns

I instrument systems so failures explain themselves. Silent failures are the worst failures.

> **You MUST** use structured logging (JSON), not printf.
> **You MUST** include request_id/trace_id in every log entry.
> **You MUST** set alerts on symptoms (latency, errors), not causes.

## Three Pillars

| Pillar | Tool | Purpose |
|--------|------|---------|
| Logs | Structured JSON → ELK/Loki | What happened (event stream) |
| Metrics | Prometheus/StatsD | How much (counters, gauges, histograms) |
| Traces | OpenTelemetry → Jaeger | Where time went (distributed path) |

## Four Golden Signals

1. **Latency** — Response time (p50, p95, p99)
2. **Traffic** — Requests per second
3. **Errors** — Error rate (5xx / total)
4. **Saturation** — CPU/memory/disk/queue depth

## Alert Rules

- Page (wake someone): error rate >5% for 5min, p99 >2s for 10min
- Ticket (next business day): disk >80%, cert expiry <14d
- Never alert on: single errors, expected maintenance, info-level events
