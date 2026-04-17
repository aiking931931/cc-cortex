# concinno Module Index

## Security Guards (hard deny, no step-back)

- `secret_scan.py` — credential/key leak detection
- `git_safety.py` — dangerous git operations block
- `dep_audit.py` — dependency security audit + scope spoofing
- `exfil_guard.py` — data exfiltration prevention
- `identity_guard.py` — identity config read-only protection
- `destruction_guard.py` — irreversible operation 3-stage confirm

## Quality Guards (step-back + deny)

- `sentinel.py` — behavior pattern detection (repeat/stagnation/hijack)
- `pre_tool_guards.py` — ReadFirst + BashPython + background enforcement
- `agent_gate.py` — agent spawn control + research/exec classification
- `file_tracker.py` — modified file tracking
- `token_monitor.py` — token budget gate
- `window_guard.py` — VSCode window validation
- `boundary_guard.py` — CC/CCC boundary violation detection
- `proposal_guard.py` — proposal side-effect analysis enforcement
- `ui_verify.py` — UI change screenshot verification flow
- `handoff_engine.py` — handoff reminder + token budget
- `multi_instance.py` — multi-instance race control
- `session_format.py` — session ID format enforcement

## Cognitive Guards (inject only, never deny)

- `cognitive.py` — session analysis + decision journal + adaptive thresholds
- `cognitive_anchor.py` — **red-team anchoring with solid-state language** (NEW)
- `confidence_gate.py` — uncertainty detection + irreversible operation gate
- `hypothesis_tracker.py` — failed approach tracking + avoidance injection
- `think_inject.py` — think-tool reasoning injection on high-risk ops

## PostToolUse Guards

- `code_guard.py` — code quality linting (ruff/cargo/eslint)
- `linting.py` — ESLint driver
- `structural_guard.py` — function length/nesting/TODO debt analysis
- `handoff_validator.py` — handoff file format validation
- `delivery.py` — enterprise delivery verification (D1-D8)

## Infrastructure

- `constants.py` — tool classifications + gate response factories
- `step_back.py` — two-layer buffer (step-back → hard deny)
- `prompt_guard.py` — clarity gate + multi-question detection
- `hook_api.py` — public API (HookResult + Pipeline)
- `feature_config.py` — feature toggles with risk metadata
- `stop_guard.py` — stop event handling
- `knowledge.py` — learning cycle (corrections → rules promotion)
- `rag.py` — cognitive RAG semantic search
- `mcp_server.py` — MCP server (tools + resources)
- `process_guard.py` — process management (zombie cleanup)
- `scheduler.py` — scheduled task management
- `git_assist.py` — git report generation
- `typescript.py` — TypeScript/JSX parsing
- `publish_scan.py` — pre-publish private key scan

## Subpackages

- `core/` — config, log, state_store, atomic, path_utils, session, notify, compact, defer_loader
- `guards/` — base.py (BaseGuard ABC), pipeline.py (GuardPipeline), registry.py (registration)
- `hooks/` — on_pre_tool, on_post_tool, on_stop, on_session_start, io_utils, script_kb
- `coordination/` — base, agent_teams, file_lock
- `ui/` — box, colors, dashboard
- `cli/` — main (concinno CLI entry)
- `skills/` — installer, guard.md, hooks.md, schedule.md
- `scripts/` — schedule-dashboard.ps1, show-result.ps1
- `_recycled/` — retired modules (three_layer, rules_guard, old boundary_guard)

## Templates (New Module Reference)

- Security Guard: `secret_scan.py` (simplest BaseGuard + hard deny)
- Quality Guard: `pre_tool_guards.py` → ReadFirstGuard
- Cognitive Guard: `hypothesis_tracker.py` (inject-only, never deny)
- PostToolUse Guard: `code_guard.py` (lint + cache pattern)
- Hook thin wrapper: `.claude/hooks/on-pre-tool.py` (stdin→CCC API→stdout)
