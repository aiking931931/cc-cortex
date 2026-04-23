# Concinno 2.16.0 Pod Merge Checklist

> **Context**: 2026-04-22 local session shipped concinno 2.15.1 + 8 `concinno-skills-*` sub-packages to PyPI. Pod side is running GAIA benchmark and editing `concinno/agent/`, `concinno/runner/` modules. This doc coordinates the merge into **2.16.0** minor release.

## Branch Model

```
main ── v2.15.1 ─────────────────────●── v2.16.0 (target)
         \                          /
          local feat/2.3.0-red-team-round-3 ●──╮ (本機 Phase 0 ship, 2.15.0/2.15.1 + Phase 1 候選)
                                               ├─ merge into release/2.16.0
          pod feat/2.16-gaia ● (pod ships)────╯
```

## Step-by-step (any session executing merge)

### Step 1 — Pod side push (pod does)

```bash
# On pod
cd ~/concinno                       # or pod's concinno path
git fetch origin                    # pull v2.15.1
git status
git log --oneline -10
# If pod is on main or some older branch, migrate:
git checkout -b feat/2.16-gaia origin/main     # or from v2.15.1 tag
# Cherry-pick / commit pod WIP
git add -A && git commit -m "gaia(agent): <specific fix>"
git push origin feat/2.16-gaia
```

**Pod MUST NOT touch** version 三源（`pyproject.toml`/`src/concinno/__init__.py`/`CHANGELOG.md`）or `RELEASE_COORDINATION.md`. Pod edits `agent/` + `runner/` + adds new tools; version bump stays for the merge session.

### Step 2 — Merge session (local or CI-spawned)

```bash
cd /e/ai-king/projects/concinno
git fetch origin
git checkout -b release/2.16.0 origin/main
git merge --no-ff origin/feat/2.16-gaia      # pod's GAIA work
git merge --no-ff origin/feat/2.3.0-red-team-round-3  # local Phase 0/1 work beyond v2.15.1
```

### Step 3 — Resolve the 4-source conflict zone

Conflicts WILL happen at:

1. **`pyproject.toml`** `version`
2. **`src/concinno/__init__.py`** `__version__`
3. **`CHANGELOG.md`** `[Unreleased]` section (both sides wrote entries)
4. **`RELEASE_COORDINATION.md`** snapshot + Pending Queue

Resolution:
- Set all three version sources to `2.16.0`.
- Merge both sides' `[Unreleased]` entries into a single `## [2.16.0] - YYYY-MM-DD` heading, structured as `Added / Fixed / Changed / Infrastructure`.
- RELEASE_COORDINATION snapshot: update to 2.16.0 LIVE, Queue empty after ship.

### Step 4 — Verification (CI / RunPod, not local — MEMORY #86 禁本機)

```bash
# RunPod or CI:
pytest tests/ -q                    # expect ~6400+ (2.15.1 baseline 6130 + wave commits)
ruff check src/ tests/
mypy src/concinno
```

### Step 5 — Ship 2.16.0

```bash
# Verify switch
python -c "from concinno.release_authorization import describe_current_config; print(describe_current_config())"
# disabled=True → proceed without string auth

git add -A
git commit -m "release(2.16.0): merge pod GAIA + local Phase 0/1 wave"
python -m build
twine check dist/concinno-2.16.0*
twine upload --disable-progress-bar dist/concinno-2.16.0*

git tag v2.16.0
git push origin release/2.16.0
git push origin v2.16.0

# Merge back to main
git checkout main
git merge --ff-only release/2.16.0
git push origin main
```

### Step 6 — Sub-package bump (optional)

If any `concinno-skills-*` sub-package needs to bump `concinno>=2.16.0` minimum dependency, do it in separate PRs in their respective directories (or future standalone repos).

## Pod Work Inventory (when pod reports in, fill this)

| Pod commit | Module | Scope | Merges cleanly? |
|---|---|---|---|
| TBD | `agent/...` | GAIA fix | TBD |
| TBD | `runner/gaia_agent.py` | Benchmark scoring | TBD |

## Anti-patterns

- ❌ Pod bumps version locally → merge conflict guaranteed
- ❌ Local tries to pull pod state via cherry-pick without fetching — pod's origin must be pushed first
- ❌ Merge both branches, skip [Unreleased] dedup → duplicate CHANGELOG entries after release
- ❌ Ship without `release_authorization.describe_current_config()` switch-first check (L1 rule)

## Cross-reference

- [`RELEASE_COORDINATION.md`](../RELEASE_COORDINATION.md) — snapshot + Pending Queue
- [`CHANGELOG.md`](../CHANGELOG.md) — `[Unreleased]` → `[2.16.0]`
- [`~/.claude/rules/L1/release_coord.md`](../../../.claude/rules/L1/release_coord.md) — universal SOP
- MEMORY #67 outer-inner race / #71 switch-first / #86 禁本機大規模 test / #95 Concinno/Sancio boundary
- [`feedback_concinno_subpackage_ecosystem_sop.md`](../../../_AI_BRAIN/../../../C:/Users/zerox/.claude/projects/e--ai-king/memory/feedback_concinno_subpackage_ecosystem_sop.md) — Wave-* ship SOP (may apply to sub-package 2.16.0 bumps)
