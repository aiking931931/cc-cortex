# CC Cortex Launch Posts

## Twitter/X

```
cc-cortex: Production-grade hooks for Claude Code

Run 4 sessions in parallel without conflicts. Auto-learn from corrections. Detect prompt injection (100% rate, 0% FP). Track tokens, enforce handoffs, score session quality.

1090+ tests. Battle-tested on real multi-agent workflows.

pip install cc-cortex

github.com/anthropics-community/cc-cortex
```

---

## Reddit (r/ClaudeAI, r/ChatGPTCoding, r/LocalLLaMA)

**Title:** cc-cortex — I ran 4 Claude Code sessions in parallel and built hooks to stop them from destroying each other

**Body:**

After months of running multiple Claude Code sessions on the same codebase, I kept hitting the same problems: sessions overwriting each other's files, repeating the same mistakes across conversations, and context windows filling up with no warning.

So I built **cc-cortex** — a Python hook system that turns Claude Code into a production-grade multi-agent dev environment.

**What it does (24 modules, all optional):**

- **Multi-instance coordination** — file-level tracking so 2-4 sessions don't collide
- **Auto-learning loop** — scans transcripts for corrections, promotes patterns to a persistent knowledge base (3+ occurrences = permanent rule)
- **Prompt injection detection** — 14-module scanner, 100% detection on 20 attack vectors, 0% false positives (vs ~92%/~8% for alternatives)
- **SafeExec** — AST-parsed command safety classifier (allow/deny/warn before any shell command runs)
- **GitSafety** — blocks force-push on main, hard reset with uncommitted changes, branch -D on unmerged
- **Sentinel** — detects brute-force retries, analysis paralysis, scope creep in real-time
- **Token guardian** — 4-tier early warning (60K/100K/140K/180K)
- **Structured handoffs** — 3-zone layout with auto-GC under 80 lines
- **Session quality scoring** — grades every session on completion/accuracy/focus/efficiency
- ...and 15 more (dep audit, cost tracking, smart model routing, work analytics, etc.)

**Numbers:**

| Benchmark | Result |
|---|---|
| tsc cache hit | 1.72ms |
| Lock throughput | 1190 ops/s |
| Injection detection | 100% |
| Module import | 0.005s |
| Memory overhead | 0.1MB |
| Tests | 1090 passed |

**Quick start:**

```bash
pip install cc-cortex
cc-cortex init
```

It copies hooks to `~/.claude/hooks/`, updates your Claude Code settings, and asks which modules to enable. Zero dependencies beyond Python 3.10+.

Built for Claude Code but the hook architecture is framework-agnostic. PRs welcome.

GitHub: github.com/anthropics-community/cc-cortex

---

## Hacker News

**Title:** Show HN: cc-cortex – Production hooks for Claude Code (multi-instance, auto-learning, security)

**Body:**

cc-cortex is a Python hook system for Claude Code that solves the problems you hit when running multiple AI coding sessions on the same codebase.

Core problems it addresses:

1. **File conflicts** — two sessions edit the same file silently. cc-cortex tracks file ownership per session and blocks concurrent writes.

2. **No memory** — each session starts from scratch. cc-cortex scans transcripts for corrections, extracts "wrong → right" patterns, and promotes them to a persistent knowledge base after 3+ occurrences.

3. **Security blind spots** — cc-cortex includes a 14-module prompt injection scanner (100% detection, 0% FP on our test suite), command safety classification via AST parsing, and dependency typosquatting detection.

4. **Context window death** — 4-tier token warning system + structured handoff files with auto-GC.

The hook architecture follows Claude Code's native event model (on-session-start, on-pre-tool, on-post-tool, on-stop). Each module is independent — enable what you need.

1090 tests including red-team scenarios, stress tests (100-thread lock contention), fuzz testing, and system failure simulations.

    pip install cc-cortex
    cc-cortex init

Python 3.10+, zero external dependencies. Apache 2.0.

https://github.com/anthropics-community/cc-cortex
