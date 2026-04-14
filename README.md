# CC Cortex

```text
 ██████╗ ██████╗     ██████╗ ██████╗ ██████╗ ████████╗███████╗██╗  ██╗
██╔════╝██╔════╝    ██╔════╝██╔═══██╗██╔══██╗╚══██╔══╝██╔════╝╚██╗██╔╝
██║     ██║         ██║     ██║   ██║██████╔╝   ██║   █████╗   ╚███╔╝
██║     ██║         ██║     ██║   ██║██╔══██╗   ██║   ██╔══╝   ██╔██╗
╚██████╗╚██████╗    ╚██████╗╚██████╔╝██║  ██║   ██║   ███████╗██╔╝ ██╗
 ╚═════╝ ╚═════╝     ╚═════╝ ╚═════╝ ╚═╝  ╚═╝   ╚═╝   ╚══════╝╚═╝  ╚═╝
```

**The Cognitive Layer for Claude Code**

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![Zero Dependencies](https://img.shields.io/badge/dependencies-zero-brightgreen.svg)](#zero-dependency-philosophy)
[![Tests: 3430](https://img.shields.io/badge/tests-3430-brightgreen.svg)](#architecture)
[![Guards: 55+](https://img.shields.io/badge/guards-55%2B-orange.svg)](#guard-pipeline--eslint-for-ai-behavior)
[![PyPI](https://img.shields.io/pypi/v/cc-cortex.svg)](https://pypi.org/project/cc-cortex/)
[![A2A](https://img.shields.io/badge/A2A-v1.0-blue.svg)](#why-cortex)
[![Skills: 66](https://img.shields.io/badge/skills-66-blueviolet.svg)](#modules)
[![Agents: 36](https://img.shields.io/badge/agents-36-blue.svg)](#modules)
[![NIST AI RMF](https://img.shields.io/badge/NIST_AI_RMF-aligned-blue.svg)](#enterprise-governance)

> **cc-cortex** is a modular hook toolkit for [Claude Code](https://docs.anthropic.com/en/docs/claude-code). It gives your AI coding assistant safety guardrails, memory, multi-instance coordination, and autonomous self-improvement — all through a zero-dependency, drop-in Python package.

---

## Why "Cortex"?

The **cortex** is the outermost layer of the brain — responsible for perception, decision-making, and higher-order cognition. **CC Cortex** transforms raw Claude Code sessions into a coherent, self-improving cognitive system:

- **Guardrails** that prevent destructive actions (like the prefrontal cortex inhibiting impulsive behavior)
- **Memory** that persists across sessions (like hippocampus-cortex memory consolidation)
- **Coordination** across multiple instances (like the corpus callosum connecting brain hemispheres)
- **Self-improvement** through reflection and learning (like neuroplasticity reshaping neural pathways)

---

## The Six Pain Points

| # | Pain Point | Without CC Cortex | With CC Cortex |
|---|-----------|-------------------|----------------|
| 1 | **Destructive Actions** — AI can delete files, force-push, or overwrite work | Hope for the best | `destruction_guard` blocks `rm -rf`, `git push --force`, and 40+ patterns |
| 2 | **Secret Leaks** — API keys hardcoded into Bash commands | Manual review | `secret_scan` detects API keys, tokens, and passwords in real-time |
| 3 | **Multi-Instance Conflicts** — Two Claude sessions edit the same file | Silent data corruption | `multi_instance` detects conflicts, denies concurrent writes |
| 4 | **Token Waste** — AI goes in circles, brute-forces bugs | Burn through your budget | `sentinel` detects loops, analysis paralysis, and brute-force debugging |
| 5 | **Amnesia** — Every new session starts from zero | You re-explain context every time | `knowledge` auto-loads corrections and learnings |
| 6 | **Handoff Fragility** — Session ends, context is lost | Start over next time | `handoff_engine` enforces structured handoffs with three-state tracking |

---

## Quick Start (30 seconds)

```bash
pip install cc-cortex    # zero-dep core, or: pip install cc-cortex[all]
cc-cortex init           # auto-detects workspace, installs hooks
```

**What just happened?** CC Cortex registered 4 hooks into your Claude Code
`settings.json` — every tool call now passes through 55+ guards before
execution. Try it:

```python
from cc_cortex import create_default_pipeline, GuardContext

pipe = create_default_pipeline()  # 55 guards, sorted by category
result = pipe.run_pre_tool(GuardContext.from_hook_data({
    "tool_name": "Bash",
    "tool_input": {"command": "rm -rf /"},
}))
print(result)  # → {"permissionDecision": "deny", ...}
```

**Install tiers** — pick what you need:

| Tier    | Command                       | What you get                              |
| ------- | ----------------------------- | ----------------------------------------- |
| Default | `pip install cc-cortex`       | Full power: guards + LLM judges + FieldRead |
| Lite    | `pip install cc-cortex[lite]` | Zero deps, guards only                    |
| RAG     | `pip install cc-cortex[rag]`  | + chromadb + sentence-transformers         |
| All     | `pip install cc-cortex[all]`  | Everything                                |

---

## Guard Pipeline — "ESLint for AI Behavior"

If you know ESLint, you already know CC Cortex guards.

| ESLint | CC Cortex Guard |
| ------ | --------------- |
| **Rule** (`no-unused-vars`) | **Guard** (`destruction_guard`) |
| **Ruleset** (`.eslintrc`) | **Pipeline** (44 guards, auto-sorted) |
| **Severity** (error/warn/off) | **Category** (SECURITY/QUALITY/COGNITIVE) |
| **Fix** (`--fix`) | **Step-Back** (self-correction prompt) |
| **Plugin** (`eslint-plugin-react`) | **Custom Guard** (subclass `BaseGuard`) |
| **Path scope** (`overrides[].files`) | **`path_scope`** (glob patterns) |

CC Cortex uses a **unified Guard Pipeline** with 55+ guards across 3 layers:

```text
SECURITY  (7 guards)   →  hard deny, no step-back
QUALITY   (39 guards)  →  hard deny + step-back middleware
COGNITIVE (9 guards)   →  knowledge injection on allow
```

### Writing Custom Guards

```python
from cc_cortex import BaseGuard, GuardCategory, GuardContext, GuardResult
from cc_cortex import create_default_pipeline

class ProdDeployGuard(BaseGuard):
    name = "prod_deploy"
    category = GuardCategory.SECURITY
    # Only active for files in deploy/ (like ESLint overrides)
    path_scope = ["deploy/*", "scripts/deploy*"]

    def check(self, ctx: GuardContext) -> GuardResult | None:
        if ctx.tool_name == "Bash" and "production" in ctx.tool_input.get("command", ""):
            return GuardResult.deny("Production deploy blocked without --confirm")
        return None

pipe = create_default_pipeline()  # 55 built-in guards
pipe.register(ProdDeployGuard())  # + your custom guard

result = pipe.run_pre_tool(GuardContext.from_hook_data({
    "tool_name": "Bash",
    "tool_input": {"command": "deploy --env production"},
}))
# → {"permissionDecision": "deny", "reason": "..."}
```

### 1.4.0 — Input Rewriters (ALLOW / DENY / **REWRITE**)

Until 1.3.0, guards could only let a tool call through or stop it. 1.4.0
adds a third outcome: **REWRITE**. A guard can return
`GuardResult.rewrite(updated_input=...)` and Claude Code will run the
rewritten `tool_input` instead of the original — surfaced through
`hookSpecificOutput.updatedInput`, the official CC channel.

Three rewriters ship by default:

| Rewriter | What it catches | What it rewrites to |
| -------- | --------------- | ------------------- |
| `BashDryRunRewriter` | `rm -rf .`, `rm -fr <glob>` | `echo '[dry-run] would have run: …'` |
| `WriteSecretFileRewriter` | `Write(.env)`, `Write(credentials.json)`, `Write(secrets.yaml)` | `.env.example`, `credentials.example.json`, `secrets.example.yaml` |
| `BashPipeToShellRewriter` | `curl … \| bash`, `wget … \| sh` | `curl -fsSL -o /tmp/cc-cortex-download.sh && echo 'inspect first'` |

Rewrites are *narrow*, *idempotent*, *visible* (every rewrite surfaces
a `↻ <guard>: <reason>` note), and *composable* — a later guard can
still DENY a rewritten call. Write your own by subclassing `BaseGuard`
and returning `GuardResult.rewrite(updated_input=…, reason=…)`. See
`examples/rewrite_guards_example.py` and
`examples/custom_rewrite_guard_example.py` for runnable demos.

### 1.4.0 — LLM-as-Judge via `prompt_hooks`

Claude Code 2026-04 shipped a `type: "prompt"` hook that runs a short
single-turn LLM evaluation inside the CC runtime. `cc_cortex.prompt_hooks`
wraps that feature with three curated judge prompts — you get the value
of an LLM reviewer without CCC itself importing an LLM SDK (core stays
zero-dep).

```python
from cc_cortex import install_prompt_hooks, ALL_JUDGES
from pathlib import Path

# Installs HallucinationJudge + ExcuseScannerJudge + CodeQualityJudge
# into the given settings.json. Idempotent + atomic. Uses Haiku 4.5 by
# default; user-authored hooks in the same file are untouched.
install_prompt_hooks(
    Path.home() / ".claude" / "settings.json",
    judges=ALL_JUDGES,
)
```

| Judge | Event | Matcher | Purpose |
| ----- | ----- | ------- | ------- |
| `HALLUCINATION_JUDGE` | `PostToolUse` | `Write\|Edit` | Flag unsourced factual claims |
| `EXCUSE_SCANNER_JUDGE` | `Stop` | — | Flag hedging language when declaring work done |
| `CODE_QUALITY_JUDGE` | `PostToolUse` | `Write\|Edit` | Flag the four cardinal code sins |

`uninstall_prompt_hooks()` and `list_installed_judges()` round-trip the
install. Full runnable demo in `examples/prompt_hooks_example.py`.

<details>
<summary>Legacy v0.5 API (deprecated, removed in v1.0)</summary>

```python
from cc_cortex import HookResult, Pipeline

pipe = Pipeline()
pipe.add_deny_guard("destruction", evaluate)
result = pipe.run("Bash", {"command": "rm -rf /"})
```

</details>

See [examples/custom_hooks.py](examples/custom_hooks.py) for runnable demos.

---

## Modules

CC Cortex ships ~40 modules organized into 5 layers:

### Safety & Guardrails

| Module | Description |
|--------|-------------|
| `destruction_guard` | Blocks destructive CLI commands with R0-R4 risk classification + auto-backup |
| `secret_scan` | Detects hardcoded API keys, tokens, and passwords |
| `git_safety` | Blocks force-push, reset --hard, and other dangerous git operations |
| `dep_audit` | Detects dependency typosquatting (pip/npm/uv) |
| `exfil_guard` | Prevents sensitive file uploads and data exfiltration |
| `sentinel` | Detects brute-force debugging, analysis paralysis, and edit loops (6 layers) |
| `code_guard` | Ruff / Cargo / Go vet code quality checks with SHA256 caching |
| `linting` | ESLint integration for JS/JSX |
| `typescript` | Automatic `tsc --noEmit` validation with project detection |

### Memory & Learning

| Module | Description |
|--------|-------------|
| `knowledge` | Auto-captures corrections, multi-language patterns, staleness detection |
| `cognitive` | Adaptive learning — session profiles, decision journal, threshold tuning |

### Coordination

| Module | Description |
|--------|-------------|
| `multi_instance` | File-level session locking with zombie detection and conflict resolution |
| `process_guard` | ctypes-based Windows process tree enumeration + orphan cleanup |
| `coordination` | Strategy Pattern base + file locks (extensible) |

### Optimization

| Module | Description |
|--------|-------------|
| `token_monitor` | Token usage tracking with graduated alerts |
| `agent_gate` | Sub-agent spawn control — counting, escalation, and hard cap |
| `window_guard` | IDE focus detection for notification suppression |

### Infrastructure

| Module | Description |
|--------|-------------|
| `hook_api` | Public composition API — `HookResult` + `Pipeline` |
| `core/config` | Central configuration loader (cc_config.json, lazy singleton) |
| `core/atomic` | Atomic JSON read/write with file locking |
| `core/session` | Session ID generation |
| `core/notify` | Cross-platform notifications (Windows Toast / macOS / Linux) |
| `scheduler` | Cross-platform task scheduling (Task Scheduler / launchd / cron) |
| `mcp_server` | MCP Server adapter for Claude Code native integration |
| `warn_router` | Warning message routing and priority classification |
| `feature_config` | Feature toggle with risk metadata and validation |
| `cli` | `cc-cortex init/status/doctor` CLI entry point |

---

## CLI

### `cc-cortex status`

```text
$ cc-cortex status

cc-cortex modules:

  🔒 core                 Token guardian + session notifications   (always on)
  ✅ knowledge            Auto-learning loop + knowledge base      (default)
  ✅ multi_instance       File locking + zombie GC                 (default)
  ✅ sentinel             Anti-brute-force detection               (default)
  ✅ secret_scan          Hardcoded secret detection               (default)
  ✅ git_safety           Dangerous git operation detection         (default)
  ✅ dep_audit            Dependency typosquatting detection        (default)
  ✅ exfil_guard          Data exfiltration prevention             (default)
  ✅ destruction_guard    Risk-based destructive op interception    (default)
  ✅ stop_guard           Premature session stop detection          (default)
  ✅ cognitive            Cross-session learning + decision tracking (default)
```

### `cc-cortex doctor`

```text
$ cc-cortex doctor

🩺 cc-cortex doctor

  ✅ cc_config.json — valid
  ✅ on-session-start.py
  ✅ on-stop.py
  ✅ on-pre-tool.py
  ✅ on-post-tool.py
  ✅ settings.json [SessionStart] → on-session-start.py
  ✅ settings.json [Stop] → on-stop.py
  ✅ settings.json [PreToolUse] → on-pre-tool.py
  ✅ settings.json [PostToolUse] → on-post-tool.py

  All checks passed.
```

---

## Configuration

CC Cortex uses a single `cc_config.json` file:

```jsonc
{
  "hook_mode": "auto",        // auto | off | minimal | balanced | full
  "hook_overrides": {},        // per-module overrides
  "thresholds": {
    "max_handoff_lines": 80,
    "token_warn_at": 60000,
    "sentinel_loop_count": 3
  }
}
```

See [examples/cc_config_example.jsonc](examples/cc_config_example.jsonc) for a fully annotated configuration.

---

## Enterprise Governance

CC Cortex provides built-in alignment with enterprise AI governance standards:

| Standard | Alignment | CC Cortex Feature |
|----------|-----------|-------------------|
| **NIST AI RMF** (Govern/Map/Measure/Manage) | Measure + Manage | Guard audit logs (JSONL) + gate deny enforcement |
| **NIST AI Agent Standards** (2026) | Auth + Privilege Control | Identity guard + agent gate + confidence gate |
| **ISO/IEC 42001** | AI Management System | Feature config + delivery gate + structured handoffs |
| **EU AI Act** | Human Oversight + Audit Trail | Destruction guard confirm flow + immutable audit log |

**Audit trail**: Every guard deny is logged to `~/.claude/destruction_audit.log` (JSONL, immutable append-only).

**Delivery gate**: `delivery.py` enforces binary pass/fail exit criteria, mechanical verification, and three-state reporting (pass/partial/fail with evidence).

---

## Architecture

```text
cc-cortex/
├── src/cc_cortex/
│   ├── __init__.py          # Public API: BaseGuard, GuardPipeline
│   ├── guards/              # Guard Pipeline (base, pipeline, registry)
│   ├── core/                # Atomic I/O, config, session, notify, compact
│   ├── hooks/               # Hook entry points (pre/post tool, session, stop)
│   ├── coordination/        # Strategy Pattern base + file locks
│   ├── skills/              # Skill installer + SKILL.md templates
│   ├── destruction_guard.py # R0-R4 risk classification + auto-backup
│   ├── sentinel.py          # 6-layer behavior detection + prescriptions
│   ├── knowledge.py         # Auto-learning with correction extraction
│   ├── cognitive.py         # Adaptive thresholds + session profiles
│   ├── delivery.py          # Enterprise delivery gate (exit criteria + verify)
│   ├── confidence_gate.py   # Uncertainty detection + irreversible op gate
│   ├── think_inject.py      # Think tool injection for high-risk operations
│   ├── field_read.py        # Selective field extraction (ZIQ breakeven gate)
│   ├── rag.py               # Cognitive RAG (optional: chromadb)
│   └── cli/                 # `cc-cortex` CLI entry point
├── tests/                   # 3430+ tests (pytest)
├── examples/                # Runnable examples
└── docs/                    # Documentation
```

---

## Zero Dependency Philosophy

The core package (`pip install cc-cortex[lite]`) has **zero external
dependencies** — stdlib only. The default install adds optional LLM and
FieldRead support; `[rag]` adds chromadb; `[all]` includes everything.

Why zero-dep core?

- **Claude Code hooks run on every tool call.** Dependencies mean startup latency.
- **Reproducibility.** No version conflicts, no supply chain risk.
- **Portability.** Works on any system with Python 3.10+.

---

## Contributing

We welcome contributions! Please read [CONTRIBUTING.md](CONTRIBUTING.md) before submitting a PR.

**Key rules:**

- Zero external dependencies (stdlib only)
- All changes must pass `ruff check` and `pytest`
- Every new module needs tests

---

## License

Apache-2.0 License. See [LICENSE](LICENSE) for details.

---

<p align="center">
  <strong>CC Cortex</strong> — The Cognitive Layer for Claude Code<br>
  <em>Stop re-explaining. Start remembering.</em>
</p>
