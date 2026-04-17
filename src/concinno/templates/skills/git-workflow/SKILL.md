---
name: git-workflow
description: Git branching strategies, commit conventions, PR workflow. Triggers on "git workflow", "branching", "commit convention", "分支策略", "conventional commits", "PR flow".
user-invocable: true
disable-model-invocation: true
---

# /git-workflow — Git Workflow & Conventions

I use git as a communication tool. Every commit tells a story. Every branch has a purpose.

> **You MUST** use conventional commit format: `type(scope): description`.
> **You MUST** keep PRs under 400 lines diff — split if larger.
> **You MUST** never force-push shared branches without team confirmation.

## Commit Types

| Type | When |
|------|------|
| feat | New feature |
| fix | Bug fix |
| refactor | Code restructure, no behavior change |
| docs | Documentation only |
| test | Adding/fixing tests |
| chore | Build, CI, deps, tooling |
| perf | Performance improvement |

## Branching Strategy

```
main (production)
  └─ develop (integration)
       ├─ feat/user-auth
       ├─ fix/login-redirect
       └─ refactor/db-layer
```

- `main` = always deployable
- Feature branches from `develop` (or `main` for trunk-based)
- Squash merge for clean history, merge commit for preserving context

## PR Checklist

1. Title follows conventional commit format
2. Description explains WHY, not just WHAT
3. Tests pass, no type errors
4. Self-reviewed diff before requesting review
5. Screenshots for UI changes
