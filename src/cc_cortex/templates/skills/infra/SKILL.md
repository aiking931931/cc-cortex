---
name: infra
description: Infrastructure patterns — IaC, cloud architecture, scaling, networking. Triggers on "infrastructure", "基礎設施", "IaC", "Terraform", "cloud", "scaling", "VPC", "networking".
user-invocable: true
disable-model-invocation: true
---

# /infra — Infrastructure Patterns

I design infrastructure that is reproducible, observable, and boring. Boring infra is good infra.

> **You MUST** codify all infrastructure — no manual console changes.
> **You MUST** separate environments (dev/staging/prod) with identical configs.
> **You MUST** encrypt at rest and in transit by default.

## Decision Tree

```
Scale?
  ├─ Single server → VPS + Docker Compose + Caddy
  ├─ Small team → Managed services (RDS, Cloud Run, Vercel)
  ├─ Medium → Kubernetes (EKS/GKE) + Terraform
  └─ Large → Multi-region + CDN + global LB

IaC tool?
  ├─ Multi-cloud → Terraform / OpenTofu
  ├─ AWS-only → CDK or Terraform
  ├─ Simple → Docker Compose + scripts
  └─ K8s → Helm + Kustomize
```

## Cost Optimization

1. Right-size instances (monitor CPU/mem, downsize if <30% avg)
2. Reserved/spot for predictable/batch workloads
3. Auto-scaling with target tracking (CPU 60-70%)
4. Delete unused resources weekly (orphan volumes, old snapshots)
5. CDN for static assets (reduce origin traffic 80%+)
