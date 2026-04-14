# CC Cortex — Contributor Guide

This is a library. It must run for strangers, not for me.

**CC Cortex** = The Cognitive Layer for Claude Code. Open-source hook toolkit (guards, process supervisor, scheduler, skills, RAG).

## Boundary — Library, not Application

- **CCC (this repo)** = the library. Must be portable, generic, zero personal state.
- **CC (my private workspace `E:\Cursor`)** = the application. Consumes CCC via `pip install cc-cortex`.
- The parent `E:\Cursor\CLAUDE.md` governs my personal workspace. **It does NOT apply inside this directory.** When you work here, this file is the authority.

## Hard Rules

1. **Never write personal paths in CCC source.** No `E:\Cursor\...`, no `_AI_BRAIN/...`, no `C:\Users\...`. Use `Path.home()`, env vars, or CLI args.
2. **Never put CC-specific logic in CCC.** Anything with a `_Z` suffix is CC-private and must stay in the consumer repo.
3. **Fix bugs at the source.** If CCC behavior is wrong, fix it in `src/cc_cortex/` — never monkey-patch inside CC. CC copying CCC logic is a rule violation.
4. **Zero runtime deps for the core.** Optional deps (`chromadb`, `sentence-transformers`) live under `[project.optional-dependencies]` only.
5. **BoundaryGuard is hardened.** CC hook with >20 lines of business logic → DENY. CCC file containing a personal path → DENY. Don't try to work around it.

## Core Modules (v1.1)

| Module | Purpose |
|---|---|
| `PromptEngine` | Dynamic prompt assembly with anti-drift re-injection |
| `ThinkingDepthGuard` | Read:Edit ratio degradation detection (ref #42796) |
| `AgentSupervisor` | Contract-based subagent verification |
| `ZIQRetrieval` | EMA adaptive RAG with per-source weights |
| `MemoryPalace` | Spatial structured memory (MemPalace-inspired) |
| `C0Router` | CBUA complexity classifier (Simple/Complicated/Complex/Chaotic) |
| `Facade` | Subsystem packages: `inject/`, `token/`, `agent/`, `memory/`, `prompt/`, `handoff/` |

| `PremiseGate` | Block execution when external constraints unverified |
| `HallucinationGuard` | Detect unsourced claims in written content |
| `IntentAnchorGuard` | Periodic re-injection of user's original intent |
| `InitialIntentProbe` | Probe user's root purpose for Complex+ tasks |
| `VerifyBeforeWriteGuard` | Verify external references before writing |
| `SedimentationGate` | Block stop when corrections not sedimented |
| `TaskOrchestrator` | Session-level task decomposition and tracking |
| `CostTracker` | Per-session token/cost tracking with budget ceiling |
| `ProgressReporter` | Formatted milestone reports |
| `ErrorRecovery` | Four-level recovery: retry→degrade→escalate→pause |

Full guard pipeline (55 guards), skills (66), and agents (36) documented in `README.md`.

## Development

- **Install**: `pip install -e ".[dev]"`
- **Test**: `pytest` (currently 3138 tests, target: all green)
- **Lint**: `ruff check src/ tests/`
- **Type check**: `mypy src/cc_cortex` (strict on `delivery/`, `cognitive/`, `process_guard/`, `guards/`)
- **Coverage floor**: 80% (see `[tool.coverage.report]`)

## Before Publishing

Every release step lives in `CHANGELOG.md` and the release checklist. **Read it before bumping version.** In short:

1. All tests green (`pytest`)
2. `CHANGELOG.md` updated with the new version
3. `pyproject.toml` version bumped
4. `python -m build` then `twine check dist/*`
5. Tag + push + `twine upload`

## Philosophy

Small surface, deep behavior. Users should be able to install, init, and forget — the cognitive layer should earn trust by never getting in the way when it's right, and stopping the bleeding when it's wrong.
