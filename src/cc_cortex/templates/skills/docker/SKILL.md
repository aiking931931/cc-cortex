---
name: docker
description: Container best practices — Dockerfile optimization, compose, multi-stage builds. Triggers on "docker", "container", "容器", "Dockerfile", "compose", "image size".
user-invocable: true
disable-model-invocation: true
---

# /docker — Container Best Practices

I build images that are small, secure, and reproducible. Every layer earns its place.

> **You MUST** use multi-stage builds for compiled languages.
> **You MUST** never run as root in production containers.
> **You MUST** pin base image versions (no :latest in production).

## Dockerfile Checklist

1. **Base**: `FROM python:3.12-slim` (not full, not alpine unless needed)
2. **Order**: System deps → app deps → app code (cache layers)
3. **User**: `RUN useradd -r app && USER app`
4. **Copy**: `COPY --chown=app:app . .` (minimal context)
5. **Health**: `HEALTHCHECK CMD curl -f http://localhost:8080/health`
6. **Size**: Target <100MB for microservices, <500MB for ML

## Decision Tree

```
Language?
  ├─ Python → slim + venv + pip install --no-cache-dir
  ├─ Node → node:lts-slim + npm ci --omit=dev
  ├─ Go → multi-stage (build on golang, run on scratch/distroless)
  └─ Rust → multi-stage (build on rust, run on distroless)

Compose?
  ├─ Dev → volumes for hot-reload, no resource limits
  └─ Prod → resource limits, restart: unless-stopped, no volumes for code
```
