---
name: ship
description: Use when the user wants to ship, release, publish, merge, or push code to production. Triggers on keywords like "ship", "發布", "上線", "release", "merge", "push".
user-invocable: true
disable-model-invocation: true
---

# /ship — Non-Interactive Release (Pipeline Step 6)

I ship verified work, not hopes. Every step has a rollback plan.

## Purpose

Non-interactive release pipeline: pre-flight checks → merge → test → version → push → PR → document. Each step must pass before the next runs. Abort on first failure.

## Pipeline Context

This is **Step 6** (final) of the Think→Plan→Build→Review→Test→Ship pipeline.
- **Reads**: `.claude/pipeline-state.json` for review verdict and QA results
- **Writes**: Final pipeline state with ship status

## Arguments

```
/ship                — Full ship pipeline (pre-flight → push → PR)
/ship --dry-run      — Run all checks but don't push/merge
/ship --pr           — Create PR only (no merge)
/ship --version <v>  — Explicit version bump (semver)
/ship --skip-qa      — Skip QA gate (requires explicit user confirmation)
```

## Execution Flow

### 1. Pre-Flight Checks

All must pass. Abort on first failure.

```
## Pre-Flight

- [ ] Working tree clean (no uncommitted changes)
- [ ] On feature branch (not main/master)
- [ ] Type check passes (tsc --noEmit / mypy / ruff check)
- [ ] Lint passes (eslint / ruff / cargo clippy)
- [ ] Build succeeds (npm run build / python -m build)
- [ ] Tests pass (npm test / pytest)
- [ ] No CRITICAL findings from /review (check pipeline state)
- [ ] QA verdict = PASS (check pipeline state)
```

If pipeline state shows no /review or /qa was run:
- Warn: "No review/QA on record. Run `/review` and `/qa` first, or use `--skip-qa` with confirmation."

### 2. Version Bump (if applicable)

Detect project type and bump accordingly:

| Project Type | Version File | Tool |
|-------------|-------------|------|
| Python (pyproject.toml) | `pyproject.toml` + `__init__.py` | Manual edit |
| Node (package.json) | `package.json` | `npm version <type>` |
| Rust (Cargo.toml) | `Cargo.toml` | Manual edit |

If `--version` provided, use explicit version. Otherwise infer from changes:
- Bug fixes only → patch
- New features → minor
- Breaking changes → major

### 3. Commit & Push

```bash
# Stage all changes
git add <specific files>

# Commit with conventional format
git commit -m "feat/fix/chore(<scope>): <description>"

# Push to remote
git push -u origin <branch>
```

### 4. Create PR (if --pr or default)

```bash
gh pr create \
  --title "<type>(<scope>): <description>" \
  --body "$(cat <<'EOF'
## Summary
<bullet points from commit messages>

## Test Plan
<from /qa results or manual checklist>

## Pipeline
- Think: <status>
- Review: <verdict>
- QA: <verdict>
- Ship: <this PR>

Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

### 5. Post-Ship

- Update `.claude/pipeline-state.json` → `"current_phase": "shipped"`
- If project has deploy script: suggest running it (don't auto-deploy without confirmation)
- Clear pipeline state for next feature cycle

### 6. Ship Report

```
## Ship Report

**Version**: <version>
**Branch**: <branch> → <base>
**PR**: <URL>
**Pipeline**:
  - Think: <status or "skipped">
  - Review: <verdict or "skipped">
  - QA: <verdict or "skipped">
  - Ship: DONE

**Post-Ship**:
  - [ ] Deploy (if applicable)
  - [ ] Monitor (first 30 min)
  - [ ] Update docs (if API changed)
```

## Pipeline State (Final)

```json
{
  "current_phase": "shipped",
  "timestamp": "<ISO 8601>",
  "feature": "<feature name>",
  "version": "<semver>",
  "pr_url": "<URL>",
  "pipeline_complete": true
}
```

## Guard Integration

- **PublishScanGuard**: Scans for secrets before push
- **DepAuditGuard**: Checks dependencies for known vulnerabilities
- **DeliveryGate**: Final verification against exit criteria

## Abort Conditions

Any of these immediately abort the ship:
1. Pre-flight check fails
2. Tests fail after version bump (rollback version)
3. Push fails (network/permissions)
4. PR creation fails

On abort:
```
SHIP ABORTED at step <N>: <reason>
Rollback: <what was undone>
Fix: <suggested action>
```

## Anti-Patterns

- Do NOT ship from main/master — always from feature branch
- Do NOT skip pre-flight checks — they exist for a reason
- Do NOT auto-deploy without user confirmation — shipping ≠ deploying
- Do NOT create empty PRs — every PR must have meaningful changes
