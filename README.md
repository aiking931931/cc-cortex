# Concinno

> *Previously known as CC Cortex (CCC)*

**A hook-based governance toolkit compatible with Anthropic's Claude Code CLI**

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-red.svg)](LICENSE)
[![Commercial License Available](https://img.shields.io/badge/Commercial_License-Available-green.svg)](COMMERCIAL_LICENSE.md)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![PyPI](https://img.shields.io/pypi/v/concinno.svg)](https://pypi.org/project/concinno/)
[![Tests](https://github.com/aiking931931/concinno/actions/workflows/ci.yml/badge.svg)](https://github.com/aiking931931/concinno/actions/workflows/ci.yml)

**Disclosure**: see [DISCLOSURE.md](DISCLOSURE.md) for the 2.21–2.23
GAIA artifact cleanup notice.

> **concinno** is a modular hook toolkit that plugs into
> [Anthropic's Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code).
> It adds opt-in dev-time scaffolding — guardrails against destructive
> commands, session memory, multi-instance coordination, and structured
> handoffs — through a drop-in Python package.
>
> Concinno is a complementary add-on, **not** a replacement for Claude Code
> or Anthropic's managed safety features. It interoperates with the
> official hook contract documented at
> <https://docs.anthropic.com/en/docs/claude-code/hooks>.

---

## Why "Concinno"?

**Concinno** (Latin: "I regulate, I moderate") is the governance layer for Claude Code. Previously known as CC Cortex, **Concinno** transforms raw Claude Code sessions into a coherent, self-improving cognitive system:

- **Guardrails** that prevent destructive actions (like the prefrontal cortex inhibiting impulsive behavior)
- **Memory** that persists across sessions (like hippocampus-cortex memory consolidation)
- **Coordination** across multiple instances (like the corpus callosum connecting brain hemispheres)
- **Self-improvement** through reflection and learning (like neuroplasticity reshaping neural pathways)

---

## The Six Pain Points

| # | Pain Point | Without Concinno | With Concinno |
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
pip install concinno    # zero-dep core, or: pip install concinno[all]
concinno init           # auto-detects workspace, installs hooks
```

**What just happened?** Concinno registered 4 hooks into your Claude Code
`settings.json` — every tool call now passes through 55+ guards before
execution. Try it:

```python
from concinno import create_default_pipeline, GuardContext

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
| Default | `pip install concinno`       | Full power: guards + LLM judges + FieldRead |
| Lite    | `pip install concinno[lite]` | Zero deps, guards only                    |
| RAG     | `pip install concinno[rag]`  | + chromadb + sentence-transformers         |
| All     | `pip install concinno[all]`  | Everything                                |

---

## Guard Pipeline — "ESLint for AI Behavior"

If you know ESLint, you already know Concinno guards.

| ESLint | Concinno Guard |
| ------ | --------------- |
| **Rule** (`no-unused-vars`) | **Guard** (`destruction_guard`) |
| **Ruleset** (`.eslintrc`) | **Pipeline** (44 guards, auto-sorted) |
| **Severity** (error/warn/off) | **Category** (SECURITY/QUALITY/COGNITIVE) |
| **Fix** (`--fix`) | **Step-Back** (self-correction prompt) |
| **Plugin** (`eslint-plugin-react`) | **Custom Guard** (subclass `BaseGuard`) |
| **Path scope** (`overrides[].files`) | **`path_scope`** (glob patterns) |

Concinno uses a **unified Guard Pipeline** with 55+ guards across 3 layers:

```text
SECURITY  (7 guards)   →  hard deny, no step-back
QUALITY   (39 guards)  →  hard deny + step-back middleware
COGNITIVE (9 guards)   →  knowledge injection on allow
```

### Writing Custom Guards

```python
from concinno import BaseGuard, GuardCategory, GuardContext, GuardResult
from concinno import create_default_pipeline

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
| `BashPipeToShellRewriter` | `curl … \| bash`, `wget … \| sh` | `curl -fsSL -o /tmp/concinno-download.sh && echo 'inspect first'` |

Rewrites are *narrow*, *idempotent*, *visible* (every rewrite surfaces
a `↻ <guard>: <reason>` note), and *composable* — a later guard can
still DENY a rewritten call. Write your own by subclassing `BaseGuard`
and returning `GuardResult.rewrite(updated_input=…, reason=…)`. See
`examples/rewrite_guards_example.py` and
`examples/custom_rewrite_guard_example.py` for runnable demos.

### 1.4.0 — LLM-as-Judge via `prompt_hooks`

Claude Code 2026-04 shipped a `type: "prompt"` hook that runs a short
single-turn LLM evaluation inside the CC runtime. `concinno.prompt_hooks`
wraps that feature with three curated judge prompts — you get the value
of an LLM reviewer without CCC itself importing an LLM SDK (core stays
zero-dep).

```python
from concinno import install_prompt_hooks, ALL_JUDGES
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
from concinno import HookResult, Pipeline

pipe = Pipeline()
pipe.add_deny_guard("destruction", evaluate)
result = pipe.run("Bash", {"command": "rm -rf /"})
```

</details>

See [examples/custom_hooks.py](examples/custom_hooks.py) for runnable demos.

---

## Modules

Concinno ships ~40 modules organized into 5 layers:

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
| `cli` | `concinno init/status/doctor` CLI entry point |

---



## Feature Switches

<!-- BEGIN: feature-index -->
<!-- Auto-generated from FEATURE_META. Run `concinno features sync-readme` to refresh. -->

| Feature | Category | Effect scope | ZIQ-tunable | Description |
|---------|----------|--------------|-------------|-------------|
| `intent_anchor` | behavioral | immediate |  | CBUA Stage 0 / B4 anchoring: re-inject the user's original intent every N write-tools to prevent scope drift, redteam tangents, and direction-loss in long sessions. |
| `insight_engine` | cognitive | immediate |  | Proactive knowledge injection when user prompt matches blind-spot rules |
| `binary_extractor` | context | immediate |  | Inline-extract xlsx/csv/tsv attachments into the prompt (bypasses weak-model tool-use discipline) |
| `cognitive_anchor` | context | session_restart | ✓ | Inject solid-state language red-team prompts before high-risk operations (architecture edits, large deletions, new modules, deploys) |
| `gaia_music_image_upscale` | context | immediate |  | Pre-inference 4× LANCZOS upscale gate for music notation image questions. Small (<800px) staff-notation images are upscaled before being fed to the local vision encoder, which underperforms on sub-pixel notehead detail at native resolution. Pure preprocess — no prompt injection. Renamed from legacy `bassclef_wordreverse` (2026-04-26) to remove task-specific naming; back-compat alias preserved one minor version. |
| `gaia_music_procedure_anchor` | context | immediate |  | L1 domain-typed anchor: inject music-notation procedure (clef line/space mnemonics + common time-units) for any question referencing musical staff / clef / noteheads. Generic textbook knowledge, no GAIA answer paths. |
| `gaia_polygon_area_procedure_anchor` | context | immediate |  | L1 domain-typed anchor: inject orthogonal-polygon area procedure (label-vs-decoration / boundary walk / closure check / decompose / sum / sanity check) for area-of-polygon questions. Generic geometry, no GAIA answer paths. |
| `gaia_polygon_image_upscale` | context | immediate |  | Pre-inference 4× LANCZOS upscale gate for orthogonal-polygon image questions. Small (<800px) polygon-with-labels images are upscaled before being fed to the local vision encoder, which loses small numeric labels at native resolution. Pure preprocess — no prompt injection. Renamed from legacy `polygon_counting_hint` (2026-04-26) to remove task-specific naming; back-compat alias preserved one minor version. |
| `gaia_tool_router` | context | immediate |  | Route GAIA questions by the Annotator-Metadata Tools field (ground-truth tool list) instead of self-regex heuristic |
| `gaia_web_only_procedure_anchor` | context | immediate |  | L1 domain-typed anchor: inject web-research procedure (call web_search / multi-hop strategy / Wayback fallback) for questions with no attachment + temporal/named-entity cues. Generic research strategy, no GAIA answer paths. |
| `gemma4_vision` | context | immediate |  | Enable Gemma 4 native vision handler (Gemma4VisionChatHandler) in place of Qwen2.5-VL fallback |
| `image_upscale_4x` | context | immediate |  | Auto 4× LANCZOS upscale for small (<800 px) images before vision inference — music notation / compact tables benefit |
| `language_enforce` | context | session_restart |  | Inject language enforcement on every tool call — forces thinking + responses in configured language |
| `ocr_fallback` | context | immediate |  | Route text-heavy images through OCR + text-LLM reasoning (charts / headstones / documents) before vision |
| `pipeline_mode` | context | immediate |  | Toggle between Dynamic (Guard Pipeline + learning loop) and Static (pure prompt pipeline, no guards) mode. Dynamic is a strict superset of Static |
| `session_switches` | context | session_restart |  | SessionStart summary of non-default switches — ensures the agent reads user opt-outs before primacy-bias kicks in |
| `unified_inprocess` | context | immediate |  | Use a single in-process Llama instance for both text and vision (KV cache shared, no HTTP :9000 hop) |
| `plugins_enabled` | core | immediate |  | Master switch for entry-points plugin discovery (concinno.features + concinno.skills). Off = ignore all installed concinno-skills-* packages. Off via env CONCINNO_PLUGINS_ENABLED=0 or allowlist restrictions via CONCINNO_PLUGINS_ALLOWLIST=pkg-a,pkg-b. Same trust model as pytest/flask/mkdocs plugins -- pip install is the trust boundary. |
| `agent_cap` | hard_gate | immediate | ✓ | Block execution-type Agent spawn after N times per session. Research agents (Explore/Plan/read-only) are uncapped. |
| `bash_background_gate` | hard_gate | immediate |  | Block long-running Bash commands without run_in_background |
| `boundary_guard` | hard_gate | immediate |  | Hook/library boundary violation detection (PreToolUse DENY) |
| `butterfly_guard` | hard_gate | immediate | ✓ | Butterfly Effect: discover issue → must fix before continuing. Tracks issues in a session-scoped ledger, denies non-fix operations |
| `clarity_gate` | hard_gate | immediate | ✓ | Block ambiguous prompts combined with irreversible operations |
| `consecutive_fail_gate` | hard_gate | immediate | ✓ | Block after N consecutive tool failures (stuck detection) |
| `delivery_gate` | hard_gate | immediate | ✓ | Enterprise delivery verification — block submission of unverified work |
| `handoff_required_guard` | hard_gate | immediate |  | Block session stop when work was done but no handoff file updated |
| `hijack_gate` | hard_gate | immediate | ✓ | TADS four-level circuit breaker based on hijack_score (L0→L2→L3→L4) |
| `identity_guard` | hard_gate | immediate |  | Block Agent from modifying identity configs (CLAUDE.md, .claude/rules/, settings.json, hook configs) |
| `prompt_guard` | hard_gate | session_restart | ✓ | Clarity gate + multi-question detection for UserPromptSubmit |
| `proposal_guard` | hard_gate | immediate |  | Block new proposals in planning files without side-effect analysis |
| `publish_scan` | hard_gate | immediate |  | Pre-publish artifact scan for secrets, keys, and personal paths |
| `python_c_gate` | hard_gate | immediate |  | Block complex python -c one-liners (>5 lines) |
| `read_first_gate` | hard_gate | immediate |  | Block Edit/Write on existing files not yet Read this session |
| `sentinel_gate` | hard_gate | immediate | ✓ | Block repeated Edit on same file N+ times (with lint exception) |
| `token_gate` | hard_gate | immediate | ✓ | Block Agent spawn when context tokens exceed threshold |
| `ui_verify` | hard_gate | immediate |  | Lock after deploy with UI changes until screenshot verification |
| `whitepaper_guard` | hard_gate | immediate |  | Block whitepaper IP keywords from leaking to external paths |
| `code_guard` | hard_quality | immediate |  | Python(ruff) / Rust(cargo) / Go(vet) static analysis |
| `design_theory` | hard_quality | immediate | ✓ | Enforce design principles: Vertical Slice traceability on planning files, Deep Module ratio check on code files |
| `handoff_format` | hard_quality | immediate |  | Validate handoff file structure on write |
| `linting` | hard_quality | immediate |  | ESLint for JavaScript files |
| `structural_guard` | hard_quality | immediate | ✓ | Structural analysis (func length / nesting / TODO debt / file size) |
| `typescript` | hard_quality | immediate |  | tsc --noEmit type checking with SHA256 cache |
| `memory_relief` | optional_optimization | immediate |  | Windows-only RAM cleanup with before/after stats and per-process trim list. SAFE tier needs no admin; STANDBY/AGGRESSIVE/DESTRUCTIVE require elevated token. Auto-fires as wave 4 of process_guard chain when wave 3 leaves RAM above threshold. |
| `configure_permissions` | utility | immediate |  | One-shot allowlist bootstrap — add ~100 safe Bash patterns (pytest/ruff/git/pip) to ~/.claude/settings.json so the agent stops being prompted for routine ops |
| `deny_marker` | ux | session_restart |  | Red ANSI counter on every deny (✖ 阻擋 #N) |
| `session_summary` | ux | session_restart |  | Visual session end summary (token/streak/files box) |
| `streak_ux` | ux | session_restart |  | Clean edit streak celebrations (🔥x5, ✅ fixed, etc.) |
| `token_display` | ux | session_restart |  | Append real token usage to CRITICAL/MILESTONE UX messages |

<!-- END: feature-index -->
## CLI

### `concinno status`

```text
$ concinno status

concinno modules:

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

### `concinno doctor`

```text
$ concinno doctor

🩺 concinno doctor

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

Concinno uses a single `cc_config.json` file:

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

### Upgrade Safety (2.16.0+)

**Guarantee**: `pip install --upgrade concinno` never resets user-set values
in `~/.concinno/*.json` or `~/.claude/*.json`. Your opt-outs survive.

How it works:

- User-tunable values live in `~/.concinno/<feature>.json` — the package
  install process touches `site-packages/` only, never your home directory.
- When a new Concinno version ships with different defaults for an existing
  key (e.g. `release_auth.disabled=False` → user still sees their set
  `True`), `concinno.config_preservation.preserve_user_values` merges the
  two dicts with **user scalar always wins**. New keys from the upgrade
  are added; existing keys keep their user values.
- `safe_write_config` is atomic (temp-file + `os.replace`) with rotating
  backups (`.bak.1` / `.bak.2` / `.bak.3`).
- Corrupted JSON files are **never** silently overwritten — the package
  emits a stderr warning and falls back to in-memory defaults, leaving
  the user's file untouched.

Regression test: `tests/test_config_survives_upgrade.py` locks this
invariant with 25 pytest cases — CI fails if a future PR breaks the
guarantee.

---

## Observability & Audit Logs

Concinno is an **observability / monitoring layer** for Claude Code tool
calls. It emits structured evidence that the *deployer* can map to whatever
governance framework their organisation follows. Concinno itself is not
audited, certified, or endorsed by any standards body, and makes no claim
to confer compliance on downstream systems.

- **Audit trail**: every guard deny is appended to
  `~/.claude/destruction_audit.log` (JSONL, append-only).
- **Delivery gate**: `delivery.py` enforces binary pass/fail exit
  criteria with mechanical verification and three-state reporting.
- **Deployer responsibility**: if your use-case falls under NIST AI RMF,
  ISO/IEC 42001, or EU AI Act Annex III, the deployer is solely
  responsible for mapping Concinno's logs onto those frameworks. See
  [docs/ai_act_compliance.md](docs/ai_act_compliance.md) for the
  disclaimer Concinno itself operates under.

---

## Architecture

```text
concinno/
├── src/concinno/
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
│   └── cli/                 # `concinno` CLI entry point
├── tests/                   # 3430+ tests (pytest)
├── examples/                # Runnable examples
└── docs/                    # Documentation
```

---

## Zero Dependency Philosophy

The core package (`pip install concinno[lite]`) has **zero external
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

## Positioning

**What Concinno is**

- Dev-time scaffolding for the Claude Code CLI.
- An individual-developer / small-team tool for guardrails, session
  memory, multi-instance coordination, and structured handoffs.
- A zero-API-cost add-on (it runs inside the user's Claude Code
  subscription; no additional tokens are billed by Concinno itself).
- Opinionated primitives you can override — every guard is subclassable,
  every feature can be disabled through `cc_config.json`.

**What Concinno is NOT**

- A cloud SaaS governance platform (that is the territory of
  managed agent offerings such as Anthropic's managed agents and
  NeMo Guardrails).
- A safety circumvention tool. Concinno guards are observability
  and dev-time guardrails; they do not bypass Anthropic's own safety
  systems or the Claude Code CLI's built-in policies.
- A certified compliance product. Alignment claims map onto the
  deployer, not onto Concinno (see *Observability & Audit Logs* above).
- An "AI system" within the meaning of EU AI Act Art 3(1) — it ships
  no model weights and makes no autonomous decisions. See
  [docs/ai_act_compliance.md](docs/ai_act_compliance.md).

---

## Security

Security-sensitive questions, including historical disclosure of
secrets in prior releases, are documented in
[SECURITY.md](SECURITY.md). PyPI uploads for Concinno use Trusted
Publishers with WebAuthn 2FA; no long-lived API tokens are stored in
CI.

---

## Export Control Notice

This software is subject to the U.S. Export Administration Regulations
(EAR). Users are responsible for compliance. Concinno is not for use
by entities on the U.S. Specially Designated Nationals (SDN) list or
in embargoed jurisdictions (Cuba, Iran, North Korea, Syria, or the
Crimea region of Ukraine).

---

## License & Trademarks

**Concinno is dual-licensed** under:

1. **GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later)** —
   the default. Use, modify, and redistribute freely, including over a
   network, **provided** you comply with §13 (network-use disclosure of
   complete corresponding source). See [LICENSE](LICENSE).
2. **Commercial License** — for organizations that cannot or will not
   comply with AGPL §13 (most commercial SaaS deployments) or whose
   internal policy bans AGPL-licensed dependencies. See
   [COMMERCIAL_LICENSE.md](COMMERCIAL_LICENSE.md) for tiers, pricing,
   and contact.

**Trademarks.** "Concinno", "Sancio", "Cerno", "ZIQ", "FieldRead",
"PSYCHE", and "TACB" are trademarks of Chen Syuan Wang (王晨宣 /
AI King). The AGPL grant does not include trademark rights —
forks must rename before public distribution under a different mark.

**Sole copyright.** All Concinno copyright is held by Chen Syuan
Wang as sole author. For acquisition or strategic partnership
discussions: <me@ai-king.dev>.

Claude Code is a trademark of Anthropic PBC. Concinno is an
independent open-source project and is **not affiliated with,
endorsed by, or sponsored by Anthropic PBC**. References to Claude
Code are made solely for interoperability and identification
purposes.

---

<p align="center">
  <strong>Concinno</strong> — hook-based governance for Claude Code<br>
  <em>Observable, local, opinionated.</em>
</p>
