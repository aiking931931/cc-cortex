<!-- markdownlint-disable MD013 MD060 -->

# CC Cortex — Contributor Guide

This is a library. It must run for strangers, not for me.

**CC Cortex** = The Cognitive Layer for Claude Code. Open-source hook toolkit (guards, process supervisor, scheduler, skills, RAG).

## Boundary — Library, not Application

- **Concinno (this repo)** = the library. Portable, generic, zero personal state.
- **CC (my private workspace `E:\Cursor`)** = the application. Consumes Concinno via `pip install concinno`.
- The parent `E:\Cursor\CLAUDE.md` governs my personal workspace. **It does NOT apply inside this directory.** When you work here, this file is the authority.

## Boundary — Concinno vs Sancio (2026-04-22 user-corrected)

Concinno is **the full agent-capability bag for anything CC can do today**. Sancio is **only the runtime that breaks CC's platform ceiling**.

- **"Can CC do it?" is the single routing rule.** If yes → Concinno (core library or a `concinno-skills-*` sub-package). If no (CC L1-L8 is locked) → Sancio.
- **Consequence for integration skills** (chat / google / office / video / content / mobile / customer-support / SQL / CRM / …): they all live in Concinno, as `concinno-skills-*` sub-packages that auto-mount via `ToolRegistry` entry_points. A VS Code CC user who runs `pip install concinno-skills-google` gets `GoogleCalendar` in the agent loop without Sancio.
- **What Sancio actually owns** (narrow, on purpose): `PostToolUse` observation-channel deny + Pre-hook escalation (CC L6 physics constraint per GitHub anthropics/claude-code#32105: post-hook cannot block side effects in any runtime; Sancio's uniqueness = LLM-visible result rewrite via `is_error=True` + cross-iteration policy escalation, neither of which CC's `updatedToolOutput` exposes — Option C reframe 2026-05-01), cross-session `state_store`, `subagent_fork` for real-time supervision (CC L1 loses control after spawn), byte-exact cache fork. Sancio **consumes** `concinno-skills-*`; it does not re-implement them.
- **Ceiling retraction rule**: when CC ships a platform update that absorbs one of Sancio's L1-L8 bypasses, that capability migrates back into Concinno. Sancio is permanently downsized against CC's trajectory, never widened.
- **Obsoleted framing**: the old "Concinno 是磚廠、Sancio 是小工地" metaphor (and any analogue that paints Sancio as an OpenClaw-style integration runtime) is **retired** — it drifted Sancio toward owning integration surface that VS Code CC users should get natively from Concinno.

## Hard Rules

1. **Never write personal paths in Concinno source.** No `E:\Cursor\...`, no `_AI_BRAIN/...`, no `C:\Users\...`. Use `Path.home()`, env vars, or CLI args.
2. **Never put CC-specific logic in Concinno.** Anything with a `_Z` suffix is CC-private and must stay in the consumer repo.
3. **Fix bugs at the source.** If Concinno behavior is wrong, fix it in `src/concinno/` — never monkey-patch inside CC. CC copying Concinno logic is a rule violation.
4. **Zero runtime deps for the core.** Optional deps (`chromadb`, `sentence-transformers`) live under `[project.optional-dependencies]` only. `concinno-skills-*` sub-packages declare their own deps in their own `pyproject.toml`.
5. **BoundaryGuard is hardened.** CC hook with >20 lines of business logic → DENY. Concinno file containing a personal path → DENY. Don't try to work around it.
6. **Sancio-bound features must name the locked CC L#.** Any new file claimed for Sancio must document in its module docstring which CC limitation (L1 spawn-control, L6 post-tool-deny, etc.) justifies being outside Concinno. If you can't name one, it belongs in Concinno or a `concinno-skills-*` sub-package.
7. **6-point DoD for every new feature / skill / module** — (1) Switchable
   `enabled` + 6-source chain, (2) ZIQ-aligned when applicable, (3) 3-layer
   classification (index / summary / full), (4) Lazy-load on demand,
   (5) CP-optimal **or** SOTA **or** logic-maximal (pick one), (6) CBUA-optimal.
   Violation = review block. See `~/.claude/rules/00-L0.md` rule #6.
   **User-config preservation**: store user-tunable values in
   `~/.concinno/<feature>.json` or OS keyring so `pip install --upgrade concinno`
   **never resets** them. Guaranteed by `test_config_survives_upgrade`
   regression test (Concinno 2.16.0+).

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
| `TaskOrchestrator` | Session-level task decomposition and tracking |
| `CostTracker` | Per-session token/cost tracking with budget ceiling |
| `ProgressReporter` | Formatted milestone reports |
| `ErrorRecovery` | Four-level recovery: retry→degrade→escalate→pause |

Full guard pipeline (55 guards), skills (66), and agents (36) documented in `README.md`.

## Development

- **Install**: `pip install -e ".[dev]"`
- **Test**: `pytest` (currently 3138 tests, target: all green)
- **Lint**: `ruff check src/ tests/`
- **Type check**: `mypy src/concinno` (strict on `delivery/`, `cognitive/`, `process_guard/`, `guards/`)
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
