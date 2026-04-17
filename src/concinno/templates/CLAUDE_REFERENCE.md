# CC Cortex — The Cognitive Layer for Claude Code

> This reference was installed by `concinno init`. Customize freely.
> It does NOT replace your existing CLAUDE.md — merge what you need.

## Quick Start

```bash
concinno init          # Install hooks + config
concinno doctor        # Verify installation
concinno status        # Show guard pipeline status
concinno-skills               # Install cognitive skills
concinno-rag build            # Build semantic memory index
concinno-scheduler --install self-reflection  # Set up auto-reflection
```

## Guard Pipeline

CC Cortex runs a 3-layer guard pipeline on every tool call:

1. **SECURITY** — Secret scan, exfiltration prevention, identity verification
2. **QUALITY** — Token limits, read-before-edit, structural checks, butterfly effect
3. **COGNITIVE** — Knowledge injection, cognitive anchors, WIREDO checklist

Guards return `allow` (with optional context injection) or `deny` (with reason).
SECURITY failures block hard. QUALITY/COGNITIVE failures are graceful.

## Available Commands

| Command | Description |
|---------|-------------|
| `concinno init` | Install hooks and create config |
| `concinno status` | Show all guards and their states |
| `concinno doctor` | Diagnose installation issues |
| `concinno enable <module>` | Enable a guard module |
| `concinno disable <module>` | Disable a guard module |
| `concinno-guard` | Process guard (zombie cleanup) |
| `concinno-scheduler <task>` | Run a scheduled task |
| `concinno-skills` | Install cognitive skill templates |
| `concinno-rag build` | Build/rebuild RAG index |
| `concinno-rag search <query>` | Semantic search over indexed knowledge |

## Cognitive Skills

Skills are markdown templates that provide thinking frameworks:

- `/three_layer` — L1 root cause → L2 sweet spot → L3 strategy
- `/first_principles` — Strip to fundamentals, rebuild
- `/debug_loop` — Observe → hypothesize → test → narrow
- `/prompt_select` — Choose thinking strategy (CoT/ToT/Step-Back)
- `/decision_journal` — Record and track decisions
- `/pdca` — Plan → Do → Check → Adjust cycle
- `/judgment` — Meta-cognition + uncertainty + causality
- `/awareness` — Attention defense + degradation recovery
- `/learning_loop` — Correction → distill → verify → automate

Run `concinno-skills` to install these as Claude Code skills.

## RAG (Cognitive Memory)

Semantic memory across sessions — corrections, rules, handoffs indexed locally.

```bash
concinno-rag build                    # Index all knowledge files
concinno-rag search "error pattern"   # Semantic search
concinno-rag update path/to/file.md   # Incremental update
concinno-rag stale --days 90          # Find unused knowledge
concinno-rag prune --days 90 --execute # Clean up stale entries
```

Requires: `pip install concinno[rag]` (adds sentence-transformers + chromadb)

## Configuration

Config file: `.concinno_cache/cc_config.json`

```json
{
  "features": {
    "token_gate": { "mode": "step_back_first", "agent_threshold": 140000 },
    "insight_engine": { "enabled": true },
    "pipeline_mode": { "mode": "dynamic" }
  }
}
```

Use `concinno enable/disable <module>` or edit config directly.

## Scheduling

Cross-platform automated tasks (Windows Task Scheduler / macOS launchd / Linux cron):

```bash
concinno-scheduler --install self-reflection --interval 15   # Every 15 hours
concinno-scheduler --install scavenger --interval 68          # Every 68 hours
concinno-scheduler --uninstall self-reflection                # Remove
```

Tasks run via `claude -p` (non-interactive, free with Max/Team subscription).

## Hooks

CC Cortex hooks are thin wrappers that call the guard pipeline:

| Hook | Event | Purpose |
|------|-------|---------|
| `on_pre_tool.py` | PreToolUse | Guard pipeline (deny/allow) |
| `on_post_tool.py` | PostToolUse | Token monitor, streak UX, learning |
| `on_prompt_submit.py` | UserPromptSubmit | Clarity gate, insight engine, RAG |
| `on_stop.py` | Stop | Session summary, handoff reminder |

## Internationalization

Default language: English. Set `CC_UX_LANG=zh_TW` for Traditional Chinese.

Built-in locales: en, zh_TW, ja, ko, es.
Add custom locales via the `/locale` skill.
