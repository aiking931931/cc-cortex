---
name: incident
description: Incident response playbook — triage, mitigation, communication, recovery. Triggers on "incident", "事故", "outage", "down", "P0", "on-call", "emergency response".
user-invocable: true
---

# /incident — Incident Response Playbook

I respond to incidents with urgency and clarity. Mitigate first, investigate second, blame never.

> **You MUST** mitigate before root-causing — restore service first.
> **You MUST** communicate status every 15 minutes during active incident.
> **You MUST** document timeline with UTC timestamps.

## Severity Levels

| Level | Definition | Response Time | Who |
|-------|-----------|--------------|-----|
| P0 | Service down, data loss | 15 min | All hands |
| P1 | Major feature broken | 1 hour | On-call + lead |
| P2 | Degraded performance | 4 hours | On-call |
| P3 | Minor issue, workaround exists | Next business day | Assigned |

## Response Flow

```
1. DETECT → Alert fires or user reports
2. TRIAGE → Severity? Blast radius? Who's affected?
3. MITIGATE → Rollback / feature flag / scale up / redirect
4. COMMUNICATE → Status page + Slack + stakeholders
5. INVESTIGATE → Logs → metrics → traces → code change history
6. RESOLVE → Deploy fix or confirm mitigation holds
7. POSTMORTEM → Within 48 hours (see /postmortem)
```

## Quick Mitigations

| Symptom | First Try |
|---------|-----------|
| Deploy broke it | Rollback to previous version |
| Traffic spike | Auto-scale + rate limit |
| DB overload | Kill long queries + read replica |
| Memory leak | Restart instances (rolling) |
| Bad data | Feature flag off + data fix script |
