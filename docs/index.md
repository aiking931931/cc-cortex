# CC Cortex

**The Cognitive Layer for Claude Code** — hooks, process guard, scheduler, skill templates,
and cognitive architecture. Zero runtime dependencies for the core.

## What is CC Cortex?

CC Cortex is an open-source hook toolkit that sits between you and Claude Code, providing:

- **44 guards** — Safety, quality, boundary, and cognitive enforcement
- **66 skills** — Cognitive frameworks, development pipelines, knowledge bases
- **36 agents** — Specialized subagents for code review, debugging, planning
- **14 hooks / events** — PreToolUse, PostToolUse, SessionStart, PromptSubmit, etc.

Built around CBUA (Cognitive Behavior Unified Architecture) with complexity routing
(C0→C5), three-layer memory (L0/L1/L2), and per-model token budget awareness.

## Quick Install

```bash
pip install cc-cortex
cc-cortex init
```

See [Quickstart](quickstart.md) for full setup.

## Core Principles

1. **Small surface, deep behavior** — Install, init, forget
2. **Quality earned by never getting in the way when right, stopping the bleeding when wrong**
3. **Zero personal state in library** — Portable to any user, any project
4. **Per-model awareness** — Opus 1M, Sonnet 1M, Haiku 200K each get their own budget

## Where to go next

- [Quickstart](quickstart.md) — Install + first hook in 5 minutes
- [Architecture](three-layer-cognitive-architecture.md) — L0/L1/L2 memory + CBUA C0-C5
- [Handoff Modes](handoff-modes.md) — save-token / phase / full mode trade-offs
- [Comparison](comparison.md) — vs raw Claude Code, vs other toolkits
- [Whitepaper](whitepaper.md) — Full theoretical foundation
- [API Reference](api/boundary_guard.md) — Module-level docs

## License

Apache-2.0. Copyright &copy; 2026 AI King (Chen-Xuan Wang).
