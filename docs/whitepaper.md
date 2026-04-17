# CC Cortex: The Cognitive Layer for Claude Code

## A Whitepaper on Persistent Cognition for AI Coding Assistants

> Version 3.6 | March 2026

---

## Abstract

AI coding assistants powered by large language models (LLMs) suffer from seven fundamental limitations when used in real-world, multi-session development workflows: session amnesia, destructive action risk, multi-instance conflicts, absence of learning loops, token waste through repetitive patterns, fragile session handoffs, and unverified execution. CC Cortex addresses all seven through a **unified Guard Pipeline** — a three-layer architecture of 43 guards spanning security, quality, and cognitive enhancement — that operates at the tool-call level, adding a persistent cognitive layer on top of Claude Code.

Version 3.6 introduces **RLHF side-effect gates** (OverflowGate, OrientationGate, HonestyGate, MultiPathGate — addressing alignment-induced cognitive distortions), **Equilibrium dynamic balance global circuit breaker** (≥5 QUALITY denials → 10-step pause to prevent defensive paralysis), **MilestoneGate** (SOP drift prevention with zone-adaptive frequency), and **19/28 defect coverage analysis** establishing CC Cortex as Layer 0 in a multi-layer architecture where all higher layers stand on its foundation. Version 3.3 introduces **subagent cognitive sharing** (three-layer cognitive injection — thinking directives, RAG memory routing, and delivery standards — shared between parent and subagent sessions), **agent gate prompt quality door** (denying code-task agent spawns missing test/export requirements), and **subagent WIREDO structural verification** (W-wiring and D-test checks on subagent output files). These build on v3.2 foundations: excuse-pattern detection, stop guard circuit breaker, tool redirect enforcement, read budget monitoring, and open-source cognitive RAG knowledge. Earlier v3.1 foundations: full lifecycle hook coverage (12 hook modules across 13 Claude Code events), WIREDO 6-dimension × 7-asset-type universal enforcement, semantic intent analysis, proactive MCP tools, and cross-machine state sync. Earlier versions established: Auto Delivery Gate, enterprise governance alignment (NIST AI RMF, ISO/IEC 42001), cognitive RAG for cross-session memory, and a cognitive offense layer (think injection, confidence gating, hypothesis tracking) grounded in recent research (TACL 2024, Reflexion NeurIPS 2023, PRM scaling ICLR 2025).

This whitepaper presents the problem space, architectural approach, module design, theoretical foundations, and empirical results from 90+ days of production usage across 4 concurrent Claude Code sessions.

### Position in the Product Hierarchy

CC Cortex is **Layer 0 — the hard-layer cognitive foundation** within a four-tier AI product architecture. Every higher layer (Aegis, Infinite Agent) stands on CC Cortex's guard pipeline, hook lifecycle, and cognitive RAG as its bedrock:

```text
┌────────────────────────────────────────────────────┐
│  L3  Infinite Agent — closed-source cognition       │  ARIA, 4-layer coordination, L4 metacognition, ARBITER
├────────────────────────────────────────────────────┤
│  L2  Aegis — Agent Organization Framework           │  40+ guards, organization, native tools, SKILL.md compat
├────────────────────────────────────────────────────┤
│  L1  CC Cortex (this project) — Layer 0 for CC      │  43 guards, hook lifecycle, RAG, WIREDO
├────────────────────────────────────────────────────┤
│  L0  Claude Code — Anthropic's CLI tool             │  Base LLM + tool use
└────────────────────────────────────────────────────┘
```

**Coverage analysis**: CC Cortex's 43 guards address **19 out of 28 known LLM-agent defect categories** (68% hard-layer coverage). This represents the architectural ceiling for a single-session hook layer — the remaining 32% (multi-agent coordination failures, cross-session strategic drift, supervisor-level governance) require higher layers (Aegis L2, Infinite Agent L3) that build on CC Cortex's foundation.

**Aegis** (`@aegis-fw/core`) is an open-source Agent Organization Framework that builds on CC Cortex's guard pipeline architecture. While CC Cortex operates as a hook layer for a single Claude Code session, Aegis extends the same principles to multi-agent organizations: teams of agents with automatic task routing, parallel sub-agent spawning, and 13,000+ OpenClaw SKILL.md compatibility. Aegis's 34 guards are architecturally inspired by CC Cortex's 43 guards but implemented independently in TypeScript for zero-dependency operation.

**Infinite Agent** is a closed-source engine that adds ARIA 5-gear reasoning, 4-layer coordination (strategic/staffing/supervision/intelligence), L4 metacognition, and the **ARBITER framework** (designed for supervisor agents — multi-agent conflict resolution, resource arbitration, and strategic override) on top of the same `TaskRouter.dispatch()` interface that Aegis exposes publicly. ARBITER operates at the supervisor level and is architecturally distinct from CC Cortex's single-session guard pipeline.

CC Cortex remains independently valuable as the cognitive layer for Claude Code — it does not require Aegis or Infinite Agent to function.

---

## 1. Problem Space

### 1.1 Session Amnesia

LLM-based coding assistants operate in stateless sessions. When a session ends, all accumulated context — project understanding, discovered patterns, resolved issues — is lost. The next session starts from zero, requiring the developer to re-explain context, re-establish conventions, and re-discover previously solved problems.

**Impact**: An estimated 5-10 minutes of context re-establishment per session, multiplied across dozens of sessions per week.

### 1.2 Destructive Action Risk

AI assistants can execute arbitrary shell commands, including `rm -rf`, `git push --force`, `DROP TABLE`, and other irreversible operations. While the model generally avoids these, edge cases in complex prompts or multi-step workflows can lead to unintended destructive actions.

**Impact**: A single destructive command can erase hours of work. In multi-instance setups, the blast radius multiplies.

### 1.3 Multi-Instance Conflicts

Modern workflows often involve multiple Claude Code sessions working in parallel on the same codebase. Without coordination, two sessions may edit the same file simultaneously, leading to silent data corruption or overwritten changes.

**Impact**: Undetected conflicts can introduce subtle bugs that are expensive to diagnose.

### 1.4 Absence of Learning Loops

When an AI assistant makes a mistake and is corrected, the correction exists only in the current session's context window. The same mistake will recur in future sessions. There is no mechanism for the assistant to capture corrections, identify patterns, and promote recurring lessons into persistent rules.

**Impact**: The same mistakes are corrected repeatedly, wasting both human attention and token budget.

### 1.5 Token Waste

Without metacognitive monitoring, AI assistants can enter unproductive loops: retrying the same fix repeatedly (brute-force debugging), reading files without taking action (analysis paralysis), or spawning unnecessary sub-agents. These patterns consume tokens without progress.

**Impact**: Up to 18% of token budget wasted on unproductive patterns in unmonitored sessions.

### 1.6 Handoff Fragility

When a session ends — whether due to token limits, crashes, or natural completion — the transition to the next session is a critical failure point. Without structured handoffs, the next session lacks information about what was done, what remains, and what was discovered along the way.

**Impact**: Approximately 60% of unstructured handoffs result in rework or missed tasks.

### 1.7 Unverified Execution

AI assistants produce *answers* (unverified guesses) rather than *results* (verified outcomes). A model may claim "done" without running tests, deploying, or confirming the change works. In enterprise contexts, unverified execution is a compliance and reliability risk.

**Impact**: "95% complete" status reports that are actually 30% verified, leading to false confidence and costly rework.

### 1.8 Sub-Agent Reliability (New in v3.0)

When Claude Code spawns sub-agents for parallel tasks, two systemic failures occur: (1) sub-agents lack workspace context and create files with incorrect paths or names, and (2) sub-agent outputs are accepted without verification, propagating phantom file references through the system.

**Impact**: Up to 40% of sub-agent file operations target non-existent paths, cascading into downstream failures that waste entire sub-agent budgets.

### 1.9 Context Compaction Knowledge Loss (New in v3.0)

When Claude Code compacts its context window to free memory, critical operational state — current task progress, WIREDO verification status, unresolved issues — is silently discarded. The session continues with amnesia about what it was doing and what standards it was enforcing.

**Impact**: Post-compaction sessions frequently repeat completed work, skip verification steps, and lose track of delivery standards.

---

## 2. Architecture Overview

CC Cortex operates as a **hook layer** that intercepts Claude Code events at thirteen lifecycle points through twelve hook modules:

### 2.1 Full Lifecycle Hook Coverage (v3.1)

| Event | Hook Module | Action |
| --- | --- | --- |
| **SessionStart** | `on_session_start.py` | Knowledge base injection, preference loading |
| **UserPromptSubmit** | `on_prompt_submit.py` | Clarity gating, RAG injection |
| **PreToolUse** | `on_pre_tool.py` | Guard pipeline deny/allow (43 guards) |
| **PostToolUse** | `on_post_tool.py` | Guard pipeline feedback + artifact verification |
| **PostToolUseFailure** | `on_post_tool_failure.py` | Failure pattern tracking + corrective context |
| **SubagentStart** | `on_subagent_start.py` | Workspace context injection into sub-agents |
| **SubagentStop** | `on_subagent_stop.py` | Sub-agent output artifact verification |
| **PostCompact** | `on_post_compact.py` | Critical state re-injection after compaction |
| **Stop** | `on_stop.py` | Handoff, knowledge promotion, WIREDO gate, excuse scan, stop block |
| **ConfigChange** | `on_config_change.py` | Security audit of settings modifications |
| **Elicitation** | `on_elicitation.py` | MCP interaction security audit |
| **ElicitationResult** | `on_elicitation_result.py` | Input validation + credential redaction |
| **TaskCompleted** | (via Stop) | Delivery verification |

This covers the full AI agent lifecycle: birth (SessionStart) → input (UserPromptSubmit) → action (Pre/PostToolUse/Failure) → delegation (SubagentStart/Stop) → memory (PostCompact) → configuration (ConfigChange) → external interaction (Elicitation) → death (Stop).

### 2.2 Design Principles

| Principle | Rationale |
| --- | --- |
| **Zero dependencies** | Hooks run on every tool call; import latency must be minimal |
| **Modular composition** | Each concern is an independent guard that can be enabled/disabled |
| **Fail-open** | A failing guard cannot crash the host Claude Code session |
| **Two outcomes only** | ALLOW or DENY. No WARN — soft warnings have negative ROI (TACL 2024) |
| **Prevention > Detection** | SubagentStart injection prevents errors; PostToolUse catches survivors |
| **Token budget awareness** | Every additionalContext message ≤50 tokens to preserve attention |
| **Observable behavior** | Every deny is logged to immutable JSONL audit trail |

### 2.3 Guard Pipeline (v3.0)

The unified Guard Pipeline uses a **three-layer, short-circuit architecture**:

```text
Layer 1: SECURITY  (6 guards)  — hard deny, no step-back
Layer 2: QUALITY   (27 guards) — hard deny + step-back middleware
Layer 3: COGNITIVE (6 guards)  — knowledge injection on ALLOW
```

Guards are registered via the Strategy Pattern (`BaseGuard` ABC), auto-sorted by `GuardCategory`, and executed with health tracking (3 consecutive failures → auto-disable).

**43 guards** across three layers:

| Layer | Count | Guards |
| --- | --- | --- |
| SECURITY | 6 | SecretScan, GitSafety, DepAudit, ExfilGuard, IdentityGuard, DestructionGuard |
| QUALITY | 30 | Sentinel (HijackGuard, ConsecutiveFailGuard, SentinelGuard), AgentGate, TokenGuard, ProposalGuard, UIVerify, StructuralGuard, DeliveryGuard, WiredoEnforcement, SSOTGuard, ButterflyGuard, AgentArtifactGuard, EquilibriumGuard, WindowGuard, FileTracker, BoundaryGuard, CodeGuard, LintGuard, HandoffGuard, ReadFirstGuard, ReadBudgetGuard, BashPythonGuard, DesignTheoryGuard, SiblingScanGuard, OverflowGate, OrientationGate, HonestyGate, MultiPathGate |
| COGNITIVE | 6 | CognitiveGuard, CognitiveAnchorGuard, ConfidenceGate, HypothesisTracker, ThinkInjector, WiredoGuard |

### 2.4 Ecosystem Compatibility

CC Cortex's guard pipeline architecture is designed for ecosystem portability:

- **Aegis adoption**: Aegis (`@aegis-fw/core`) implements a TypeScript guard pipeline inspired by CC Cortex's Python architecture. The four-phase model (pre-tool, post-tool, pre-llm, post-llm) and short-circuit semantics are preserved.
- **OpenClaw SKILL.md**: Aegis reads OpenClaw's SKILL.md format (Apache 2.0 open standard), enabling access to 13,000+ community skills. A built-in Security Scanner (15+ dangerous patterns) automatically vets each skill before activation.
- **Guard pipeline as API**: CC Cortex guards follow a `BaseGuard` ABC with `evaluate(context) → GuardResult`. This pattern is deliberately simple enough for third-party guard authoring.

### 2.5 Hook Modes

| Mode | Active Modules | Use Case |
| --- | --- | --- |
| `off` | Conflict detection only | Debugging hook issues |
| `minimal` | Safety core (sentinel, destruction guard) | High-token sessions |
| `balanced` | Safety + memory + basic optimization | Default for single sessions |
| `full` | All modules | Multi-instance parallel workflows |
| `auto` | Dynamically selected | Adapts to session count and token usage |

---

## 3. Key Mechanisms

### 3.1 Knowledge Persistence

CC Cortex maintains a structured knowledge base that persists across sessions. The lifecycle:

1. **Capture** — Corrections and patterns are logged during sessions
2. **Promote** — Recurring patterns (count ≥ 5) auto-promote to persistent rules via `auto_promote()`
3. **Consolidate** — Staleness detection (>90 days) + conflict detection (same context, different corrections)
4. **Load** — Each session auto-loads relevant knowledge via Cognitive RAG (§3.7)

Multi-language support: English, Chinese, Japanese, Korean, Spanish patterns.

### 3.2 Destruction Prevention

Risk-based classification with 5 levels (R0-R4):

| Level | Action | Example |
| --- | --- | --- |
| R0 | Allow | `rm temp.txt` |
| R1 | Allow + backup | `rm -r build/` |
| R2 | Deny + backup | `rm -rf src/` |
| R3 | Deny + backup + notify | `git push --force` |
| R4 | Deny + backup + notify + audit | `DROP TABLE users` |

Key characteristics:

- Context-aware pattern matching (40+ patterns across Bash, Write, Edit)
- Auto-backup before any risky operation
- Immutable JSONL audit log
- Zero false positives in 80-day production testing
- Configurable via `feature_config` with risk metadata

### 3.3 Multi-Instance Coordination

- **Write conflict detection** — File-level session locking with zombie detection
- **Task distribution** — Shared task pools with atomic claim semantics
- **Role assignment** — Named roles (A/B/C/D) with TTL-based expiration
- **Process guard** — ctypes-based Windows process tree enumeration + orphan cleanup
- **Activity-aware cleanup** (v3.0) — Before terminating long-running sessions, verifies 30-minute file activity window; active sessions get lifetime extensions instead of kills

### 3.4 Metacognitive Monitoring (Sentinel)

Six-layer behavior detection:

| Layer | Detection | Response |
| --- | --- | --- |
| L1 | Repeat edits (same file 3+) | Deny + prescription |
| L2 | Brute-force debugging (identical diffs) | Deny + "Decompose" prescription |
| L3 | Analysis paralysis (reads without writes) | Deny + "Step-Back" prescription |
| L4 | Attention hijack (tool entropy ↓ + path convergence ↑) | Deny + context reset |
| L5 | Context drift (topic wandering) | Deny + refocus |
| L6 | Diminishing returns (effort ↑, progress ↓) | Deny + "switch strategy" |

Prescriptions are injected into deny messages — not just "you're stuck," but "try X instead" (based on Reflexion NeurIPS 2023).

### 3.5 Structured Handoffs

Every session ending triggers a handoff protocol:

- **Task state** — Three-state tracking: ✅ done, ⏸ partial (with sub-status), ⬜ todo
- **Knowledge retention** — Mandatory "unresolved" section (what's stuck + why + how far)
- **Token-aware** — Graduated urgency as context window fills
- **Delivery gate integration** — Exit criteria verification before handoff
- **WIREDO block decision** (v3.0) — If session contains code edits but no WIREDO table, Stop hook returns `{"decision": "block"}` forcing the agent to complete verification before exiting

### 3.6 Enterprise Delivery Gate

Ensures AI agents deliver **results** (verified outcomes) rather than **answers** (unverified guesses).

Six capabilities:

| # | Capability | Mechanism |
| --- | --- | --- |
| D1 | Exit Criteria | Binary pass/fail defined before task starts |
| D2 | Mechanical Verifier | Bool / exit code / string + dual verification (primary + safety) |
| D3 | Three-State Report | ✅ pass / ⏸ partial / ❌ fail — with evidence |
| D4 | Karpathy Loop | `should_retry` + `rollback_decision` cycle |
| D5 | Audit Log | Immutable JSONL (every step: reasoning + verification) |
| D6 | Gate Check API | `gate_check()` for PreToolUse integration |

**Auto Delivery Gate** (v0.8+): CCC eats its own dog food — at session end, `auto_delivery_gate()` automatically runs D1→D6 on all session-edited code files: auto-generates exit criteria based on file types, gathers wired/test/screenshot evidence, verifies, reports, and audits. Zero manual intervention.

### 3.7 Cognitive RAG

Not knowledge-QA RAG — **Cognitive Prosthesis**. Indexes the AI's own correction history, rules, and handoffs. Each prompt auto-recalls relevant memory, preventing cross-session regression.

- **Model**: `paraphrase-multilingual-MiniLM-L12-v2` (Chinese 0.915 score)
- **Anti-bloat**: top_k=3, min_score=0.5, 200 char cap ≈ 200 tokens/injection
- **Knowledge pruning**: `hit_log.json` → `stale_report(90d)` → `prune()`
- **Optional dependency**: `pip install concinno[rag]` (chromadb + sentence-transformers)

### 3.8 Cognitive Offense Layer

Shifts from pure defense (blocking mistakes) to offense (guiding better reasoning):

| Module | Mechanism | Source |
| --- | --- | --- |
| **ThinkInjector** | High-risk ops → inject "use think tool" prompt | Extended thinking research |
| **ConfidenceGate** | Uncertainty markers + irreversible op → deny + "verify first" | PRM scaling (ICLR 2025) |
| **HypothesisTracker** | Record failed approaches → inject "already tried X, Y" | Reflexion (NeurIPS 2023) |
| **DiversityGate** | Stagnation → "list 3 different hypotheses" | ToT / diversity sampling |

Theoretical ceiling: external scaffolding yields ~20-40% reasoning improvement (Reflexion +22%, PRM SOTA).

### 3.9 WIREDO Delivery Standard (New in v3.0, Universalized in v3.6)

A **6-dimension × 7-asset-type** universal verification framework. WIREDO applies to ALL deliverable types — not just code. Each (asset type, dimension) pair uses **selective compliance**: `strict` / `warn` / `skip` / `na`.

**Six Dimensions**:

| Dimension | Question | Mechanical Check |
| --- | --- | --- |
| **W** — Wired | Who calls/references it? | grep confirms import/call/link exists |
| **I** — Inherited & Aligned | Does it follow the template? | File in architecturally correct module/directory |
| **R** — Responsive & Performant | Any performance traps? | Regex scan for O(n²), N+1, blocking; or latency/throughput for protocols |
| **E** — Extensible | Hardcoded values? | Regex scan for hardcoded URLs, ports, timeouts; or schema validation for configs |
| **D** — Defended & Verified | Functionally verified? | **Functional verification** (runs, does what it should). tsc/lint = prerequisite, not D. Can't verify → ⏸ defer to milestone |
| **O** — Observable | Can you see it running? | Files >50 lines checked for logging/metrics; N/A for standalone assets |

**Seven Asset Types** (with default dimension modes):

| Asset Type | W | I | R | E | D | O |
| --- | --- | --- | --- | --- | --- | --- |
| **code** | strict | strict | strict | strict | strict | strict |
| **image** | strict | strict | strict | strict | strict | na |
| **video** | strict | strict | strict | strict | strict | na |
| **audio** | strict | strict | strict | strict | strict | na |
| **document** | strict | strict | strict | strict | strict | na |
| **protocol** | strict | strict | strict | strict | strict | **warn** |
| **config** | strict | strict | **na** | strict | strict | **na** |

**Three-Layer Enforcement**:

1. **WiredoGuard** (COGNITIVE) — Detects asset type, injects type-specific WIREDO checklist into context at task start
2. **`wiredo_full_check()`** (Delivery) — Runs all 6 mechanical checks per asset type, returns per-dimension pass/fail with mode-aware evaluation
3. **WiredoEnforcementGuard** (QUALITY) — Hard denies handoff/report file writes missing WIREDO table (≥4/6 dimensions required)

**Key principle**: A dimension that doesn't apply physically = `na` (e.g., CONFIG has no performance dimension). A dimension that's nice-to-have but not required = `warn`. **Never exclude WIREDO entirely from any asset type** — use selective compliance instead.

Configurable via `cc_config.json` → `wiredo.enabled` toggle + per-asset-type switches. Supports cascade inheritance for multi-project stacks.

### 3.10 Sub-Agent Lifecycle Management (New in v3.0)

Addresses the systemic reliability gap in Claude Code's sub-agent architecture through prevention and detection:

**Prevention (SubagentStart hook)**:

- Injects workspace absolute path into every sub-agent at spawn time
- Lists existing files in key directories (src/, tests/, docs/)
- Enforces naming conventions: "All file writes MUST use absolute paths under this workspace"

**Detection (SubagentStop hook + AgentArtifactGuard)**:

- Extracts file paths from sub-agent output text (4 formats: Windows/Unix/Git Bash/relative)
- Verifies each path exists on disk
- Injects manifest: ✅ confirmed / ❌ missing into context for the parent agent
- Fires at two points: SubagentStop (precise, per-agent) and PostToolUse Agent (fallback)

**Result**: Prevention eliminates ~80% of path errors at source; detection catches the remaining 20% before they propagate.

### 3.11 Cognitive Anchor (New in v3.0)

When Claude Code compacts its context window, CC Cortex re-injects critical operational state via the PostCompact hook:

- **Current task list** — Active tasks with completion status
- **WIREDO progress** — Which dimensions have been verified
- **Unresolved issues** — Problems discovered but not yet fixed
- **Session identity** — What role this session plays in multi-instance coordination

This transforms context compaction from a knowledge-destroying event into a knowledge-preserving checkpoint.

### 3.12 Configuration Security Audit (New in v3.0)

The ConfigChange hook monitors settings modifications in real-time:

- **Security-critical changes** — Permission modifications, hook disabling, tool access changes
- **Audit trail** — Every configuration change logged with timestamp, old value, new value
- **Unauthorized modification blocking** — Prevents sub-agents or automated processes from weakening security settings

### 3.13 Semantic Intent Analysis (New in v3.1)

Breaks through the regex pattern-matching ceiling. While guards like DestructionGuard use regex for known dangerous patterns, `concinno-analyze-intent` provides **heuristic NLP analysis** for ambiguous commands:

- **Destructive verb detection** — `rm`, `delete`, `drop`, `kill`, `destroy`, `wipe`, `purge`
- **Scope amplifier analysis** — `-rf`, `--force`, `--hard`, `--all`, `*`, `--recursive`
- **Irreversibility flag scanning** — `--no-verify`, `--skip`, bypass patterns
- **Exfiltration pattern recognition** — `curl -d`, `wget | sh`, `nc` connections
- **Privilege escalation detection** — `sudo`, `chmod 777`, `chown root`
- **Pipe-to-shell detection** — `| sh`, `| python` remote code execution

Returns a composite risk score (SAFE/LOW/MEDIUM/HIGH) with specific indicators. Available as an MCP tool that Claude can proactively consult before executing ambiguous commands — flipping from "CCC blocks after the fact" to "Claude asks CCC first."

### 3.14 Proactive MCP Tools (New in v3.1)

Breaks through the passive-interception ceiling. Traditional hooks only fire when Claude uses a tool. The MCP server adds **agent-initiated consultation** — Claude can proactively ask CCC for analysis:

| MCP Tool | Purpose |
| --- | --- |
| `concinno-recommendations` | Session health analysis + actionable suggestions |
| `concinno-failure-patterns` | Recurring failure analysis with prescriptions |
| `concinno-guard-report` | Guard pipeline statistics and tuning insights |
| `concinno-analyze-intent` | Semantic command risk assessment (§3.13) |
| `concinno-sync-state` | Cross-machine state export/import (§3.15) |

Total MCP surface: 4 resources + 9 tools. The agent no longer waits for hooks to fire — it can consult CCC at any decision point.

### 3.15 Cross-Machine State Sync (New in v3.1)

Foundation for breaking through the single-machine ceiling. The `concinno-sync-state` MCP tool provides:

- **Export**: Portable JSON bundle containing session state, token usage, quality metrics, knowledge stats, guard config, and failure pattern summary
- **Import** (dry-run): Preview what a remote state merge would affect

This enables multi-machine coordination patterns: Machine A exports state → shared storage → Machine B imports and continues. Full bi-directional merge is planned for v3.2.

### 3.16 Open-Source Cognitive RAG Knowledge (New in v3.2)

Some cognitive frameworks can never be hardened into hooks or type systems — they are inherently soft cognitive guidance that must persist as retrievable knowledge. CC Cortex ships a bundled `knowledge/` directory containing universal thinking frameworks indexed by Cognitive RAG:

- **First Principles Thinking** — Decompose to fundamentals, rebuild from ground truth
- **Three-Layer Thinking** — Root cause → sweet spot → strategy enhancement
- **Three-Iteration Refinement** — Silent internal iteration before presenting
- **Socratic Questioning** — Six types of structured inquiry for assumption testing
- **Consequence-First Thinking** — Second-order effects before action
- **Dynamic Equilibrium** — Opposing forces, attention budget conservation, complexity budgets
- **WIREDO Delivery Standard** — Six-dimension × seven-asset-type universal verification with selective compliance (strict/warn/skip/na)
- **Three-State Language** — Gas (inspirational) / Liquid (guideline) / Solid (hard rule)
- **Auto Knowledge Distillation** — Correction → pattern → rule → automation pipeline
- **Inversion Thinking** — Solve problems by working backward from failure
- **Pre-Mortem Analysis** — Assume failure first, then identify causes
- **OODA Loop** — Observe → Orient → Decide → Act for rapid decision cycles

These frameworks are automatically included in RAG index builds (`RAGIndex(include_bundled=True)` by default). When a user's prompt semantically matches a framework, RAG injects a ~200-token nudge — complementing explicit Skill invocation (`/three_layer`, `/first_principles`) with automatic, context-aware cognitive support.

**Design principle**: Skill = full framework on explicit invocation. RAG = automatic nudge when context matches. They complement, not replace.

### 3.17 Excuse Scanner (New in v3.2)

Addresses the "not my fault" anti-pattern — when an AI assistant discovers pre-existing issues during a task, acknowledges them, but exits the session without fixing them.

- **28 excuse patterns** — 16 Chinese + 12 English regex patterns detecting phrases like "不是我造成", "pre-existing issue", "out of scope", "already broken before"
- **Resolution tracking** — After detecting an excuse, checks if Edit/Write/NotebookEdit followed (issue was actually fixed)
- **On-stop block** — Unresolved excuses → `EXCUSE_BLOCK:` prevents session exit, forcing the agent to either fix the issues or explicitly record them in the handoff "unresolved" section
- **Circuit breaker integration** — Shares the one-block-per-session circuit breaker with stop_guard to prevent infinite block loops

### 3.18 Stop Guard Circuit Breaker (New in v3.2)

Upgrades the stop hook from warn-only to block for premature session termination:

| Stop Category | Action | Rationale |
| --- | --- | --- |
| `clean` (completion keywords) | Allow | Session finished normally |
| `continuation` (mid-task signals) | **Block** (1st time) → Warn (2nd time) | Agent trying to stop mid-task |
| `pending` (uncompleted items) | Warn only | Could be false positive |
| `question` (asking user) | Allow | Agent deferring to user |

**Circuit breaker**: Max 1 block per session (5-minute cooldown), persisted to `~/.claude/stop_guard_block.json`. Prevents pathological stop→block→stop loops while still catching genuine premature stops.

### 3.19 Tool Redirect Guard (New in v3.2)

Enforces Claude Code's own best practices — using dedicated tools instead of Bash equivalents:

| Bash Command | Redirect To | Rationale |
| --- | --- | --- |
| `grep`, `rg` | Grep tool | Better permissions, structured output |
| `cat`, `head`, `tail` | Read tool | Line numbers, image support |
| `find` | Glob tool | Faster, modification-time sorting |
| `sed`, `awk` | Edit tool | Atomic replacements, review-friendly |
| `echo >`, `printf >` | Write tool | Content tracking, permission checks |

**Smart filtering**: Only simple commands are redirected. Complex pipes (`|`), chains (`&&`), and semicolons (`;`) pass through — these are legitimate Bash usage patterns.

### 3.20 Read Budget Guard (New in v3.2)

Detects aimless browsing — consecutive Read calls without any productive action (Edit/Write/Bash):

- **Threshold**: 8 consecutive Reads without action → context injection nudge
- **Not a deny** — Injects "Are you exploring with a goal, or browsing?" as `GuardResult.allow(context=...)`. Reading is valuable; reading without acting suggests analysis paralysis
- **Auto-reset**: Any non-Read tool (Edit, Write, Bash, Agent) resets the counter
- **Stateful per-session**: Instance-level counter, no file I/O overhead

This complements Sentinel L3 (analysis paralysis detection) with a lighter, earlier signal.

### 3.21 Subagent Cognitive Sharing (New in v3.3)

Addresses the root cause of subagent quality failures: subagents receive rules (what to do) but lack **cognitive capability** (how to think) and **shared memory** (what to know). Rules loaded into subagent context are diluted by the task prompt, resulting in "island code" — modules with no imports, no tests, and no integration.

**Three-layer cognitive injection** via `cognitive_inject.py`:

| Layer | Content | Token Budget |
| --- | --- | --- |
| L0 — Hard rules | Fix errors, don't guess, CP-rank | ~40 tokens |
| L1 — Anti-bias | Check first instinct, seek counter-evidence, causal reasoning | ~60 tokens |
| L2 — Deep cognition | Root cause → sweet spot, counterfactual, inversion | ~120 tokens |

**RAG memory routing** (three-tier, token-efficient):

1. **Index** (always injected, ~50 tokens) — Correction entry titles, relevant KB file names
2. **Summary** (on keyword hit, ~150 tokens) — One-line summaries of high-frequency corrections
3. **Pointer** (path only, ~20 tokens) — "Read `<path>` if needed" — subagent reads on demand

**Delivery standards** (all asset types, ~100 tokens) — Detects asset type (code/image/video/audio/document/protocol/config), injects type-specific WIREDO checklist with selective compliance modes.

**Agent gate prompt quality door**: When a parent spawns an execution agent for code tasks, the prompt is scanned for test/export/schema requirements. Missing ≥2 of 3 → deny with prescription. Research agents bypass this gate entirely.

**SubagentStop structural verification**: After subagent completion, new `.py`/`.ts` files are verified:

- **W-check**: `grep` confirms the file is imported by at least one other module
- **D-check**: Source files have a corresponding `test_*.py` or `*.test.ts`
- Warnings are injected into parent context as `⚠ W ❌` / `⚠ D ❌` markers

**Design principle**: Parent and subagent share the same cognitive layer — not "give subagent a copy of the rules" but "connect subagent to the same brain." The `build_cognitive_context()` function is called identically by SessionStart and SubagentStart hooks.

### 3.22 RLHF Side-Effect Gates (New in v3.5)

RLHF alignment introduces systematic side effects that degrade agent effectiveness. CC Cortex addresses these with dedicated gates — with honest assessment of what each can and cannot do:

| Gate | Target Defect | Mechanism (honest) | Effectiveness |
| --- | --- | --- | --- |
| **OverflowGate** | B1 Attention Overflow — spawning side-quest agents under cognitive overload | Token zone check (YELLOW+ → deny Agent spawn) + burst detection (≥4 in 30s). Fail-closed: missing zone data = YELLOW. | **Medium** — blocks Agent spawn when token budget strained. Cannot prevent attention overflow via non-Agent tools (Read/Grep/Bash). |
| **OrientationGate** | B2/B3 Myopia + Action Bias — rushing into long operations without planning | Regex check for long-running Bash commands; requires recent planning evidence in tool output. | **Low** — `run_in_background` bypasses it; planning evidence is keyword match on tool output, not AI reasoning. |
| **HonestyGate** | A5 Loss Aversion — downplaying errors with euphemisms | Two-phase: (1) PostToolUse records error signals, (2) PreToolUse denies Write/Edit containing euphemism regex when recent errors exist. | **Low** — covers ~20 fixed phrases; LLM trivially rephrases. Cannot detect omission (not mentioning errors). |
| **MultiPathGate** | B4/B5 Premature Convergence — committing to first solution | Denies Write/Edit to planning files if content has decision language but <3 alternatives listed. | **Medium** — correct positioning (checks AI output). But LLM generates strawman alternatives to satisfy format. |

**Honest limitations**: These gates use regex pattern matching, not semantic understanding. They catch the most obvious manifestations of RLHF side effects but cannot detect sophisticated evasion. True sycophancy detection (C1) requires a second LLM evaluation (see Aegis ARBITER framework). True hallucination detection requires external fact-checking.

**Soft-Warning Law v2** (validated 2026-03-26): Vague reminders ("are you on track?") have negative ROI. Specific guidance ("you skipped X, do Y instead") has positive ROI even under attention hijacking. All gates use deny (hard block) or specific guidance injection — never vague warnings.

**Subagent Identity System**: Six identity archetypes (Precision Craftsman, Architect, Surgeon, Logic Inquirer, Recorder, Engineer) dynamically assigned based on agent_type, each with calibrated cognition depth (minimal/standard/full). Experiment-validated: identity injection works via SubagentStart hook.

These gates operate in the QUALITY layer and complement the existing Sentinel metacognitive monitors (§3.4). They represent **Layer 0 hard-layer defenses** — the 68% of defects addressable through mechanical pattern matching. The remaining 32% require soft-layer (LLM-supervised) or multi-agent architectures (Aegis L1, Infinite Agent L2+).

### 3.23 Dynamic Equilibrium — Global Circuit Breaker (New in v3.6)

The `EquilibriumBreaker` implements a **global circuit breaker** based on the principle that opposing forces (security vs. productivity, caution vs. speed) must be dynamically balanced rather than statically configured.

**Mechanism**: When the QUALITY layer accumulates ≥5 deny decisions within a rolling window, the EquilibriumBreaker triggers a **10-step QUALITY pause** — temporarily relaxing QUALITY-layer guards to prevent the pipeline from over-constraining the agent into paralysis.

```text
Normal:    SECURITY → QUALITY → COGNITIVE  (all active)
Triggered: SECURITY → [QUALITY paused] → COGNITIVE  (10 steps)
Recovery:  SECURITY → QUALITY → COGNITIVE  (auto-restored)
```

**Rationale**: A guard pipeline that blocks too aggressively is as harmful as one that blocks too little. When an agent receives 5+ denials in quick succession, it typically enters a defensive spiral — trying to satisfy guards rather than making progress. The pause allows the agent to regain momentum, after which QUALITY guards re-engage with a clean slate.

**Safety invariant**: SECURITY layer is **never** paused. Destructive actions, secret leaks, and identity violations remain hard-blocked regardless of equilibrium state.

### 3.24 CBUA Cognitive Router (New in v3.4)

**CBUA (Cognitive-Behavioral Unified Architecture)** is CC Cortex's unified cognitive OS — a single architecture that replaces 50+ fragmented thinking frameworks with a coherent system of 6 cognitive levels, 6 action phases, and 9 AI-native capabilities.

**C0 Perception Router** (`cognitive/router.py`, ~50 token overhead):

Every task is automatically classified by complexity (Cynefin-inspired: Simple/Complicated/Complex/Chaotic), model capability tier (T1 Strong/T2 Medium/T3 Weak), and asset types (code/image/video/audio/document). The router then allocates reasoning/action/metacognition budgets:

| Complexity | Entry Level | Reasoning% | Action% | Meta% | Scaffolding (T1/T3) |
| --- | --- | --- | --- | --- | --- |
| Simple | C1 Fast | 15 | 75 | 10 | none / standard |
| Complicated | C2 Structured | 30 | 50 | 20 | minimal / maximum |
| Complex | C3 Deep | 35 | 40 | 25 | standard / maximum |
| Chaotic | C3 Stabilize | 40 | 25 | 35 | standard / maximum |

**Five Laws**: (1) Cognitive Conservation — optimize ratio, don't maximize thinking. (2) Complexity Matching — match depth to difficulty. (3) Side-Effect Awareness — evaluate consequences before committing. (4) Verification Supremacy — unverified output = nonexistent output. (5) Adaptive Evolution — learn from every interaction.

**Nine AI-Native Capabilities** (beyond human cognition): Parallel Hypothesis, Quantified Self-Monitoring, Graph-Structured Reasoning, Cognitive Budget Enforcement, Instant Domain Switch, Self-Modification, Perfect Session Memory, **Skeptical Overturn** (direction questioning at every checkpoint), **Consequence Foresight** (macro/micro toggle + short-pain-vs-long-pain analysis).

**Philosophical foundations**: Consciousness Tension Theory (R=T/M), Cognitive Riverbed Theory, Stake Theory, Dynamic Equilibrium — providing psychological and philosophical grounding beyond pure engineering.

**ArtifactPipeline** (`delivery/artifact_pipeline.py`): Unified multi-asset-type WIREDO verification with mechanical + deep checks, graceful degradation (missing dependencies → SKIP, not FAIL), and per-dimension gate decisions.

### 3.25 Per-Product Cognitive Profiles (New in v3.4)

`cognitive/profiles.py` provides per-product configuration:

| Product | Primary Cognitive | Primary Action | WIREDO | Special |
| --- | --- | --- | --- | --- |
| CC Cortex | C2+C3 | A2+A3 | Full | Multi-asset verification |
| Aegis | C2+C3 | A2+A3 | Full (synced) | Guard Pipeline |
| PSYCHEFORGE | C0+C1+C2 | A2 | Inherited | Emotion+Identity engine |
| Infinite Agent | C3+C4 | A0+A1+A4 | Cross-agent aggregation | Multi-agent coordination |

---

## 4. Theoretical Foundations

### 4.1 Soft Warning Negative ROI Law

**Claim**: PostToolUse soft warnings (`additionalContext` with advisory text) have **negative** return on investment.

**Evidence**: TACL 2024 confirms that when an LLM is stuck, adding "you might be stuck" to context does not change behavior. The attention consumed by the warning is wasted — and attention is finite.

**Implication**: CC Cortex uses only two outcomes: ALLOW (with optional knowledge context) or DENY (hard gate). The entire warn pipeline was deleted (-526 lines).

### 4.2 Attention Budget Conservation

**Principle**: When a behavior is hardened (Hook/TypeScript/CI gate), the corresponding rule must be deleted, returning attention budget.

**Four stages**: Learning (rule) → Post-it (Hook) → Muscle memory (TypeScript) → Release (delete rule)

**Implication**: CC Cortex's `additionalContext` messages are budgeted at ≤50 tokens each. Total hook context injection ≤200 tokens/turn.

### 4.3 TADS (Tension-Attention Defense System)

**Formula**: R = T/M (Resilience = Tension capacity / M-load). Reducing M is more effective than increasing T.

Three capabilities:

1. **Awareness** — Four-level circuit breaker (L0→L2→L3→L4, skip L1 per §4.1)
2. **Prevention** — Poka-Yoke (contact/fixed-value/motion-step) → auto-generate Hook guards
3. **Need sensing** — Three-layer (preference injection / ambiguity gate / post-hoc calibration)

### 4.4 Prevention > Detection Principle (New in v3.0)

**Claim**: Injecting correct context at agent spawn (SubagentStart) is strictly superior to detecting errors after execution (PostToolUse).

**Evidence**: In production, SubagentStart context injection reduced sub-agent path errors by ~80%. The remaining ~20% caught by PostToolUse detection represent edge cases where the sub-agent ignored injected context — a fundamentally different (and rarer) failure mode than context absence.

**Implication**: CC Cortex's hook architecture follows a "prevention sandwich": SubagentStart (prevent) → PostToolUse/SubagentStop (detect) → Stop (enforce). Each layer catches what the previous layer missed, with decreasing frequency.

---

## 5. Empirical Results

Collected over 90 days of continuous usage with 4 concurrent Claude Code sessions.

### 5.1 Safety

| Metric | Result |
| --- | --- |
| Destructive commands blocked | 127 (100% catch rate) |
| False positives | 0 |
| Brute-force loops interrupted | 68 |
| Average tokens saved per interruption | ~2,400 |
| Secret leak attempts blocked | 17 |
| Configuration tampering blocked | 4 |

### 5.2 Memory & Learning

| Metric | Result |
| --- | --- |
| Knowledge entries accumulated | 340+ |
| Patterns auto-promoted to rules | 52 |
| Context re-establishment eliminated | ~350 min/month saved |
| Handoff success rate | 95% (up from ~40%) |
| Cross-session regression prevented | 72% reduction |
| Post-compaction knowledge retention | 90%+ (via cognitive anchor) |

### 5.3 Coordination

| Metric | Result |
| --- | --- |
| Write conflicts prevented | 85 |
| Task distribution collisions | 0 (with atomic claims) |
| Stale sessions auto-cleaned | 31 |
| Zombie processes cleaned | 24 |
| False kills prevented (activity check) | 6 |

### 5.4 Efficiency

| Metric | Result |
| --- | --- |
| Token waste reduction | 83% (from 18% to 3% of budget) |
| Average session productivity | +40% (more tasks per session) |
| Total hook overhead per tool call | <15ms |
| additionalContext budget compliance | 100% (all ≤50 tokens) |
| Sub-agent path error reduction | ~80% (via SubagentStart injection) |

### 5.5 Enterprise Delivery

| Metric | Result |
| --- | --- |
| Verified delivery rate | 94% (up from ~55% unverified) |
| Delivery gate catches | 41 "claimed done but not verified" |
| UI deploy-without-screenshot blocks | 11 |
| WIREDO enforcement blocks | 8 (handoff without verification table) |
| Stop-hook continuation forces | 5 (code session ending without WIREDO) |

### 5.6 Code Quality

| Metric | Result |
| --- | --- |
| Total tests | 2,713 |
| Guard count | 43 |
| Hook module count | 12 |
| Hook event coverage | 13/21 (62%) |
| MCP tools | 9 |
| Bundled cognitive frameworks | 15 |
| Python module count | 135+ |
| Type annotation coverage | 94% |
| Docstring coverage (public API) | 100% |
| ruff lint | 0 errors |

---

## 6. Enterprise Governance Alignment

CC Cortex aligns with emerging enterprise AI governance standards:

| Standard | Alignment | CC Cortex Feature |
| --- | --- | --- |
| **NIST AI RMF** (Govern/Map/Measure/Manage) | Measure + Manage | Guard audit logs (JSONL) + gate deny enforcement |
| **NIST AI Agent Standards** (2026) | Auth + Privilege Control | Identity guard + agent gate + confidence gate + config audit |
| **ISO/IEC 42001** | AI Management System | Feature config + delivery gate + structured handoffs |
| **EU AI Act** | Human Oversight + Audit Trail | Destruction guard confirm flow + immutable audit log |
| **SOC 2 Type II** | Change Management | ConfigChange hook audit trail + settings tamper detection |

---

## 7. Comparison with Alternatives

| Feature | CC Cortex | Guardrails AI | NVIDIA NeMo | Superpowers | Manual Scripts |
| --- | --- | --- | --- | --- | --- |
| Zero dependencies | Yes | No (heavy) | No (heavy) | Partial | Varies |
| Claude Code native | Yes | No | No | Yes | Partial |
| Guard Pipeline (three-layer) | 43 guards | Yes (rails) | Yes (colang) | No | No |
| Full lifecycle hooks (13 events) | Yes | No | No | No | Partial |
| Sub-agent lifecycle mgmt | Yes | No | No | No | No |
| Multi-instance coordination | Yes | No | No | No | Manual |
| Learning loop | Automatic | No | No | No | No |
| Cognitive RAG | Built-in | No | No | No | No |
| WIREDO 6-dim × 7-type enforcement | Built-in | No | No | No | No |
| Enterprise delivery gate | Built-in | No | No | No | No |
| Context compaction recovery | Built-in | No | No | No | No |
| Structured handoffs | Enforced | No | No | No | No |
| Token waste detection | Real-time | No | No | No | No |
| NIST/SOC2 alignment | Documented | Partial | Partial | No | No |
| Setup time | <5 minutes | Hours | Hours | Minutes | Hours-days |

---

## 8. Limitations and Future Work

### Current Limitations

- **Python-only** — Hook runtime requires Python 3.10+
- **Single-machine** — Multi-instance coordination assumes shared filesystem (sync-state export/import provides foundation, full mesh pending)
- **Semantic analysis latency** — Full LLM-based intent analysis requires CLI round-trip; currently reserved for high-risk (R3/R4) scenarios
- **Platform ceiling** — Claude Code does not expose Compact Priority API or subagent tool restriction

### Roadmap

- **MCP Elicitation** — Interactive confirm dialogs for destruction guard (pending platform support)
- **Pluggy-level Plugin System** — Evolving from Guard Pipeline to community plugin ecosystem
- **Distributed coordination** — Multi-machine setups via lightweight network protocol
- **Semantic safety analysis** — LLM-assisted command intent understanding
- **Agent Communication Protocol (ACP)** — Structured inter-agent messaging for multi-agent orchestration

---

## 9. Conclusion

CC Cortex demonstrates that the fundamental limitations of LLM-based coding assistants — amnesia, safety risks, coordination failures, learning absence, token waste, handoff fragility, unverified execution, sub-agent unreliability, and context compaction knowledge loss — can be systematically addressed through a unified Guard Pipeline operating at the tool-call level, combined with full lifecycle hook coverage spanning 13 Claude Code events.

The five theoretical contributions — Soft Warning Negative ROI Law, Attention Budget Conservation, TADS, Prevention > Detection Principle, and Dynamic Equilibrium (global circuit breaker preventing guard over-constraint) — provide a principled foundation that extends beyond Claude Code to any LLM-based agent system. The RLHF side-effect gates (Overflow, Orientation, Honesty, MultiPath) address a previously unrecognized defect class: alignment-induced cognitive distortions where RLHF training incentivizes suboptimal agent behavior. The cognitive offense layer (ThinkInjector, ConfidenceGate, HypothesisTracker) demonstrates that external scaffolding can meaningfully improve LLM reasoning quality without modifying the underlying model. The WIREDO enforcement framework proves that delivery quality standards can be mechanically verified and physically enforced, not merely suggested. The bundled cognitive RAG knowledge library introduces a new category: **permanent cognitive prostheses** — thinking frameworks that can never be hardened into hooks or compilers, but must persist as retrievable knowledge, automatically surfaced when context demands.

As Layer 0 in a multi-layer architecture, CC Cortex covers 19/28 known LLM-agent defect categories (68% hard-layer coverage) — the architectural ceiling for a single-session hook layer. The remaining 32% (multi-agent coordination, strategic drift, supervisor governance) are addressed by higher layers (Aegis, Infinite Agent with ARBITER) that build on CC Cortex's foundation. With 43 guards, 12 hook modules covering 13 events, 9 MCP tools, 15 bundled cognitive frameworks, 2,629+ tests, NIST/SOC2 alignment, and zero dependencies, CC Cortex establishes a blueprint for cognitive layers in any AI agent system.

---

*CC Cortex is open source under the Apache-2.0 License.*
*Source: [github.com/anthropics-community/concinno](https://github.com/anthropics-community/concinno)*
*Documentation: [quickstart guide](quickstart.md) | [migration guide](migration-v05-v06.md)*
