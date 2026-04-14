---
name: cicd
description: CI/CD pipeline design — GitHub Actions, deployment strategies, pipeline optimization. Triggers on "CI/CD", "pipeline", "GitHub Actions", "deployment strategy", "continuous integration", "持續整合".
user-invocable: true
disable-model-invocation: true
---

# /cicd — CI/CD Pipeline Patterns

I build pipelines that give fast feedback and deploy with confidence. Speed without safety is recklessness.

> **You MUST** fail fast — lint/type-check before tests, unit before integration.
> **You MUST** make every pipeline step idempotent and retryable.
> **You MUST** never store secrets in pipeline files — use vault/environment secrets.

## Pipeline Stages (ordered)

1. **Lint + Type-check** (~30s) — Catch syntax/type errors immediately
2. **Unit tests** (~2min) — Fast feedback on logic
3. **Build** (~3min) — Compile, bundle, Docker build
4. **Integration tests** (~5min) — DB, API, E2E
5. **Security scan** (~2min) — Dependency audit, SAST
6. **Deploy staging** — Auto on main merge
7. **Deploy production** — Manual approval or canary

## Deployment Strategies

| Strategy | Risk | Rollback | When |
|----------|------|----------|------|
| Rolling | Low | Slow | Default for stateless |
| Blue-Green | Low | Instant | Need instant rollback |
| Canary | Lowest | Fast | High-traffic, observable |
| Recreate | High | Slow | DB migrations, breaking changes |

## GitHub Actions Tips

- Cache: `actions/cache` for node_modules, pip, cargo
- Matrix: Test across versions in parallel
- Concurrency: `concurrency: { group: ${{ github.ref }}, cancel-in-progress: true }`
