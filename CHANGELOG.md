# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.17.2] - 2026-04-14

### Added

- **`StateStore.prune_all_stale(namespaces=None, *, ttl_seconds=...)`** —
  one-call sweep across every namespace under `base_dir`. When
  `namespaces` is omitted, immediate subdirectories are auto-discovered,
  so a new module that picks a fresh namespace is swept automatically
  with no library change here. Intended for SessionStart hooks: a
  single call clears stale per-session state across the whole cache.
  Returns a `{namespace: deleted_count}` report.
- **`tests/test_state_store_prune.py`** — 6 new tests covering
  `prune_stale` (previously untested) and `prune_all_stale`
  (auto-discover, explicit list, missing base_dir, non-json skip).
- **`tests/test_git_assist.py`** — 10 new tests covering `_is_secret`
  and the previously-uncovered `auto_commit` flow (batch staging,
  secret unstage, timeout floor, failure paths, all-secret
  short-circuit).

### Fixed

- **`git_assist.auto_commit` no longer hangs on large working trees.**
  The previous implementation looped `git add -- <file>` per file,
  paying the full git startup + index lock cost on every iteration.
  On a working tree with ~957 changed files this turned a one-second
  operation into a ~10-minute hang that left `.git/index.lock` behind
  and blocked subsequent stop hooks. Replaced with a single
  `git add -A` shot followed by a single `git reset HEAD --` for
  any `_is_secret`-flagged files, matching the L0 rule "git add -A,
  never per-file." Also bumps the per-call timeout floor to 60s
  because Windows `add -A` on a large tree can cross the previous
  15s ceiling even though the operation itself is batch-fast.

### Internal notes

- This closes the SessionStart prune gap identified in the v5.5
  Phase 1 roadmap. Per-session JSON state files under
  `.cc_cortex_cache/<namespace>/` were accumulating because each guard
  module knew how to write its own state but no entry point swept the
  whole cache. The library now owns the sweep so consumer hooks stay
  thin (one import + one call).
- Already-tracked secret-like files (e.g.
  `_AI_BRAIN/00_System/keys/*credentials*.json`) are out of scope
  for the new `auto_commit` defensive unstage — those need a manual
  `git rm --cached`. The defensive layer only protects against newly
  staged secrets going forward.
- `_is_secret` known limitation: substring matching flags legitimate
  source files like `test_secret_scan.py` or `secretScanner.ts`.
  Backlog: tighten to basename + word-boundary matching.

## [1.17.1] - 2026-04-14

Patch release: two latent `_find_best_handoff` bugs fixed plus 74-test
coverage backfill on `CbuaPipelineGuard` and `auto_checkpoint`.

### Fixed

- **`_find_best_handoff` archive filter** now uses path-component
  matching instead of a naive `"_archive" in root` substring check.
  Previously any directory whose name contained the substring
  (e.g. `test_skips_archive_dir0` from pytest tmp_path, or
  `project_archived_v1`) was wrongly excluded. New `_is_archive_path`
  helper splits the relative path on `os.sep` and only treats
  components that **end with** `_archive` or are exactly `archive` as
  archive markers.
- **`_find_best_handoff` project-tag scoring** rewritten. The previous
  `os.path.dirname(root) in mf` check compared the *parent* of the
  handoff directory against the modified file path (always the
  handoff root, never inside it) so prefix scoring was always 0 in
  practice and projects could not be routed. The new logic uses
  `os.path.basename(root)` (the project tag, e.g. `cc-cortex`) and
  matches it against the modified file's `os.sep`-split components
  to avoid single-letter substring false positives.

### Added

- **`tests/test_cbua_pipeline_guard.py`** — 52 tests covering
  CbuaPipelineGuard plumbing, edit/read/agent counters, B1/C1/U1/
  WIREDO marker detection, behavioural silent-ack, polling streak
  suppression, A4 ask-violation early exit, WIREDO one-shot delivery
  trigger, `_is_delivery_command` segment-split semantics, and
  cross-session isolation.
- **`tests/test_auto_checkpoint.py`** — 30 tests covering trigger
  conditions (yellow zone OR ≥5 files), session idempotence, block
  content (token_k, next_step, file summary truncation), the new
  `_is_archive_path` helper, and project-tag routing.

### Tests

- 82 new tests. Full regression: **4428 passed / 3 xfailed**.

## [1.17.0] - 2026-04-14

ZIQ cascade pipeline orchestrator — composes IterativeRetriever
(L3→L2→L1 cascade) with ZIQRetrieval (FTRL source-weight rerank) into
a single entry point. Pure orchestrator layer; no changes to existing
retrieval or rerank internals. 26 new tests, 4301 total passing.

### Added

- **`cc_cortex.agent.retrieve_pipeline`** — new module exposing
  `ZIQCascadePipeline` orchestrator and `CascadePipelineResult`
  dataclass. Composes `IterativeRetriever` (L3→L2→L1 cascade) with
  `ZIQRetrieval` (FTRL source-weight rerank). Runs the cascade and
  only feeds L1 `raw_hits` through `ZIQRetrieval.rerank`; L3/L2
  cache-only results pass through unreranked because FTRL source-type
  classification depends on file path structure that in-memory pool
  sections do not cleanly expose. 6 new tests.
- **`cc_cortex.agent` facade re-exports** — `IterativeRetriever`,
  `CascadeConfig`, `CascadeStats`, `RetrievalResult`, `L1Retriever`,
  `EvolutionScheduler`, `CascadePipelineResult`, `ZIQCascadePipeline`.

### Internal notes

- Design decision: `ZIQRetrieval` (665 lines, FTRL rerank layer,
  single responsibility) is left untouched. The pipeline module is a
  pure orchestrator; no behavioral change to the rerank layer.
- `iterative_retrieve` subagent output from 1.16.x verified clean
  this release (20 tests) and wired through the agent facade.

### Tests

- 26 new tests (6 pipeline + 20 iterative cascade).
- Full regression: **4301 passed / 3 xfailed**.

## [1.16.0] - 2026-04-13

ZIQ v7 three-layer cognitive sharing architecture + 9-layer security
stack (surpassing Hermes Agent's 7 layers) + competition-mode advisory
routing. 171 new tests, 4256 total passing.

### Added — ZIQ v7 cognitive sharing (L2 + L3)

- **`cache/cognitive_pool.py`** — L3 cross-session/cross-agent shared
  markdown pool with stable 8-hex section hashes for prefix-safe
  Anthropic cache edits. Atomic save, stale pruning, TTL per section,
  `PoolFull` eviction, `PoolCorrupt` detection. 604 lines, 23 tests.
- **`cache/l2_distill.py`** — L2 distillation pipeline with A-MEM
  memory evolution. Consumes L1 raw hits via `DistillSink` Protocol,
  detects contradictions with existing L3 sections, rewrites instead
  of blindly appending. `EvolveRecord` audit trail. Keyword-match
  fast-path `retrieve()` bypasses L1 when answer already distilled.
  704 lines, 27 tests.
- **`cache/microcompact.py` section-edit extension** — `SectionEdit`
  dataclass + `queue_section_replace` / `queue_section_delete` /
  `flush_sections` / `compact_all` helper. Additive: existing 25
  `delete_tool_result` tests unchanged. +234 lines, 17 new tests.

### Added — 9-layer security (layers 7-9, surpassing Hermes)

- **`security/ssrf_guard.py`** (Layer 7) — fail-closed SSRF validator
  blocking RFC1918, loopback, link-local, carrier-grade NAT, cloud
  metadata (AWS/Azure/GCP exact hosts + IPs), with DNS resolution
  guard and redirect-chain re-validation. 542 lines, 40 tests.
- **`security/llm_judge_guard.py`** (Layer 8, beyond Hermes) — LLM-as-
  judge prompt-injection detector via `InjectionJudge` Protocol.
  5-type taxonomy (direct_override / indirect_injection /
  social_engineering / encoded_payload / context_manipulation).
  OrderedDict LRU cache, fail-open when no judge configured.
  233 lines, 20 tests.
- **`security/policy_gate.py`** (Layer 9, beyond Hermes) — policy-as-
  code engine with `ThreatCategory` enum (OWASP LLM01-10 + 3 NIST),
  5 matcher types (ToolName/ContentPattern/Metadata/Composite/
  Callable), `OWASP_LLM_BASELINE` built-in ruleset (11 rules
  covering all 10 OWASP threats), fail-closed evaluation, `from_dict`
  factory for YAML/JSON loading, coverage reporting. 390 lines,
  25 tests.

### Added — competition mode advisory routing (1.15.1 fold-in)

- **`guards/base.py` `GuardResult.advisory`** flag — True for
  cognitive/UX guards (CBUA/WIREDO/Read:Edit/ThinkInject), False for
  safety guards (Destruction/BashValidator/PermissionFSM/Secrets).
- **`guards/pipeline.py`** routes advisory results to audit log instead
  of LLM context when `profile == "competition"`, eliminating the
  "LLM self-talk" failure mode where the model wastes tokens
  responding to hook nags.
- **`/mode` Skill** — unified `daily`/`competition`/`handoff` profile
  switcher.

### Changed

- `security/__init__.py` now describes a "9-layer defense-in-depth
  stack" and re-exports 60+ public symbols across 5 modules.
- `cache/__init__.py` re-exports cognitive_pool, l2_distill,
  microcompact section-edit additions.

## [1.15.0] - 2026-04-13

CC parity sweep: ports Claude Code's prompt cache, fork-subagent context,
parallel dispatch, permission FSM, and bash validator pipeline into cc-cortex
so downstream projects (aegis, persona-api) inherit the three core CC pillars
— tiered caching, security FSM, parallel subagents — that CCC previously
lacked. 282 new tests, 4085 total passing.

### Added — Pillar 1: tiered caching (previously FAR BEHIND)

- **`cache/microcompact.py`** — Anthropic prompt cache editing API wrapper
  that deletes tool results without invalidating the cached prefix.
  Main-thread guard, time/token dual triggers, circuit-breaker-backed
  `CacheEditSink` Protocol, `StateStore`-persisted. Single biggest cost
  reduction for long sessions (CC ports this as the secret-weapon module).
  527 lines, 25 tests.
- **`cache/cache_break_detector.py`** — systemHash / toolsHash /
  perToolHashes / betas / effort / strategy fingerprint diff with
  per-reason counters. Pinpoints *which* field caused a cache miss instead
  of just logging "cache missed". 350 lines, 23 tests.
- **`cache/autocompact.py`** — last-resort summarization with
  `AUTOCOMPACT_BUFFER_TOKENS=13_000` reserve, circuit breaker at
  `MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES=3`, recursion guards for
  `session_memory` / `compact` / `ctx_agent` sources (prevents deadlock),
  `CONTEXT_COLLAPSE` mutual exclusion. Per-model budget lookup
  (`claude-opus-4-6` / `claude-sonnet-4-6` / `claude-haiku-4-5`).
  587 lines, 27 tests.
- **`cache/session_memory.py`** — forked subagent distills recent tool
  calls into a cap-bounded markdown file (200 lines / 25k bytes, memdir
  convention). Ring buffer of recent events + `DistillSink` Protocol for
  the LLM pass + two-stage `truncate_content` enforcing both caps.
  613 lines, 28 tests.
- **`cache/memdir.py`** — append-only dated markdown log with automatic
  rollover when today's file hits 200 lines / 25k bytes. Keyword `find_relevant`
  search over a bounded age window, `read_day` / `read_window` retrieval,
  `MemoryEntry.parse_md_line` round-trip. Default root respects
  `CC_CORTEX_MEMDIR` env var. 642 lines, 32 tests.

### Added — Pillar 2: parallel subagents (previously BEHIND)

- **`agent/fork_context.py`** — `CacheSafeParams` frozen dataclass holding
  the byte-identical subset a fork must inherit to hit the parent's
  Anthropic prompt cache prefix. `FileStateCache[T].clone()` isolates the
  parent's file-read dedup cache so fork writes don't pollute parent (and
  vice versa). `SubagentContext.clone_for_child` + `ForkDepthExceeded`
  guard. 340 lines, 22 tests.
- **`agent/parallel_dispatch.py`** — Four-path spawn router (teammate /
  fork / regular / background) with invariant enforcement:
  `TeammateCannotSpawnTeammate`, `TeammateCannotSpawnBackground`,
  `ForkInForkRejected`, `ParallelLimitExceeded`. Exports
  `COORDINATOR_PROMPT_SNIPPET` — the training prompt that teaches models
  to emit parallel `tool_use` blocks in a single assistant message
  (the key insight behind CC's parallelism). Pure policy, no subprocess
  spawning. 622 lines, 26 tests.

### Added — Pillar 3: security FSM (previously BEHIND)

- **`security/permission_mode.py`** — Five-mode FSM
  (`default` / `plan` / `accept_edits` / `bypass_permissions` / `auto`)
  with session-scoped mode latch, audit trail, and a declarative rule
  grammar (`Tool` / `Tool:glob` / `*:glob`) matched via `fnmatch`.
  Scoped rules outrank null-scope; `override=True` allow beats deny;
  `dontAsk_to_deny` materializes user "don't ask again" into permanent
  deny rules. `PermissionHook` Protocol for caller-supplied prompts.
  656 lines, 26 tests.
- **`security/bash_validators.py`** — 24-validator pipeline ported from
  `tools/BashTool/bashSecurity.ts`. Each validator closes a specific
  bypass class: `validate_empty`, `validate_length`,
  `validate_incomplete_commands`, `validate_safe_command_substitution`,
  `validate_git_commit`, `validate_jq_command`,
  `validate_shell_metacharacters`, `validate_dangerous_variables`,
  `validate_dangerous_patterns`, `validate_redirections`,
  `validate_newlines`, `validate_carriage_return`,
  `validate_ifs_injection`, `validate_proc_environ_access`,
  `validate_malformed_token_injection`, `validate_obfuscated_flags`,
  `validate_backslash_escaped_whitespace`,
  `validate_backslash_escaped_operators`, `validate_brace_expansion`,
  `validate_unicode_whitespace`, `validate_mid_word_hash`,
  `validate_comment_quote_desync`, `validate_quoted_newline`,
  `validate_zsh_dangerous_commands`. `BashValidator` orchestrator splits
  compound commands (`a && b ; c`) and strips safe wrappers
  (`timeout 5 env FOO=bar ...`) to fixed point before per-subcommand
  checks. stdlib-only port — whitelist-of-shapes not blacklist-of-tokens,
  permissive on uncertainty. 1816 lines, 75 tests.

### Added — top-level re-exports

- `cc_cortex.cache` subpackage re-exports all 10 P0/P1/P2 classes +
  constants + sinks.
- `cc_cortex.agent` subpackage extends with fork_context + parallel_dispatch
  symbols + `COORDINATOR_PROMPT_SNIPPET` constant.
- `cc_cortex.security` subpackage with permission_mode + bash_validators
  re-exports (≈44 public symbols).

### Added — WIREDO three-tier loader (from earlier 1.14.0 tail)

- **`wiredo_loader.py`** + **`wiredo_change_type.py`** + 22
  `templates/wiredo/*.md` files split the prior 2750-token static
  WIREDO prompt into a tier-routed loader. Default complicated tier
  builds ~1830 tokens (33% saving). ZIQ α_t routing: simple → core only
  (500t), complicated → +routing +dims (1000t), complex → +recipe
  (1200t), chaotic → all recipes (2500t with budget shrink backstop).
  78 tests.

### Changed

- `prompt_hooks.WIREDO_JUDGE.prompt_body` now builds lazily via
  `_default_wiredo_body()` calling `build_wiredo_prompt(alpha_t=0.40)`.
  Falls back to the prior static string if templates are missing.

### Internal notes

- Three pillars are now at parity with CC source leaked 2026-04; prior
  audit classified all three as BEHIND / FAR BEHIND. Strangers who
  `pip install cc-cortex` now get the same fork-cache / bash-validator /
  permission-mode machinery CC uses internally, with the additional
  ZIQ/CBUA guards CCC already had.
- The `dual_mind` proposal from the earlier red/blue team review
  remains demolished; 1.15.0's fork/cache/validator primitives are the
  right replacement.

## [1.14.0] - 2026-04-13

Multi-tier LLM gateway + few-shot retrieve + multi-step tool loop + 17 skill templates.

### Added

- **`escalation` module** (`LLMEscalator`, `escalate`) — auto-fallback chain
  `gemma → haiku → sonnet → opus`. Per-tier circuit breaker persisted via
  `StateStore`, single retry on transient errors, lazy `httpx`/`anthropic`
  imports so `import cc_cortex.escalation` stays cheap. Claude tiers silently
  skipped when `ANTHROPIC_API_KEY` unset. 28 tests.
- **`fewshot` module** (`FewshotBank`, `FewshotCase`) — generic solved-case
  store with Jaccard similarity retrieve. Pure stdlib, zero deps. Extracted
  from prior cybergym-specific helper into a library primitive. Pluggable
  tokenizer for CJK; backward-compat with legacy `task_id` → `id` synthesis.
  22 tests.
- **`tool_executor` module** (`ToolExecutor`, `Tool`, `ExecutionState`) —
  goal-driven multi-step tool loop. State machine `observe → think → act →
  observe`, resumable via `StateStore`, circuit breaker per tool, unparseable
  LLM reply recovery with hint injection. Lazy import of escalator so the
  module loads without Anthropic deps. Module docstring explicitly labels
  this as dispatch plumbing, not a reasoning guarantee. 28 tests.
- **17 skill templates** under `templates/skills/`: 12 personas
  (`persona-bizops-analyst`, `persona-deep-researcher`,
  `persona-finance-analyst`, `persona-game-strategist`,
  `persona-multi-agent-judge`, `persona-openenv-explorer`,
  `persona-perfectionist-creator`, `persona-safety-researcher`,
  `persona-security-researcher`, `persona-software-engineer`,
  `persona-tool-precise-agent`, `persona-web-navigator`) + 5 output formats
  (`output-code-block`, `output-free-strict`, `output-numeric`,
  `output-structured-json`, `output-tool-call`). Each ships with a
  canonical `SKILL.md` containing frontmatter (`name/description/triggers/
  category/source`) + verbatim prose body. Packaged via the existing
  `templates/**/*.md` glob — no force-include needed.
- **Top-level re-exports**: `LLMEscalator`, `EscalationResult`,
  `EscalationExhausted`, `FewshotBank`, `FewshotCase`, `ToolExecutor`,
  `Tool`, `ExecutionState`, `ToolStep`, and helper callables.

### Changed

- **`httpx>=0.27`** promoted to core `[project.dependencies]`. Previously
  brought in transitively by `anthropic`; now explicit because
  `escalation._call_gemma` uses `httpx.Client` directly. CCC's "zero-dep
  core" invariant was already broken by `anthropic`/`openai` in base, so
  this is documentation, not regression.

### Note on dual-mind red team

The `dual_mind` orchestrator originally drafted for 1.14.0 was demolished
by the structured red/blue team review (three FATAL findings: Goodhart
on the merge metric, output-post-processing wearing a cognitive primitive
mask, refactor cut at the wrong joint). The replacement shape —
`escalation` + `fewshot` + `tool_executor` + skill templates as the
composable primitives — survived review intact and is what shipped.

## [1.13.0] - 2026-04-13

CBUA pipeline hardening + cleanup module + red/blue team architecture reform.

### Added

- **`cleanup` module** — workspace hygiene utilities: `detect_dead_handoffs()`,
  `squash_auto_commits()`, `git_gc()`, `rotate_log_files()`, `cleanup_stale_files()`.
  22 tests. Called by `/tidy` Skill and scheduled cleanup hooks.
- **`CbuaPipelineGuard`** — hardened CBUA B1/C1/U1/A4/A5 enforcement via PostToolUse
  state tracking. StateStore-persisted across subprocess invocations. Replaces
  text-only SOP with behavioral signal detection.
- **CBUA forced classification** in `on_prompt_submit` — injects C0 result + B1
  format requirement + A5 red team reminder before Claude acts.
- **`feature_config.PROFILES`** — 4 preset profiles (minimal/standard/paranoid/competition)
  with `apply_profile()` API.
- **`handoff_engine.auto_checkpoint()`** — auto-write checkpoint to handoff files
  when token usage or file count exceeds thresholds.

### Changed

- **反熵優先** (entropy-first) — unified terminology across 11 files. L0 Iron Law #5
  renamed from 先清再寫. `git_assist._inline_squash_if_needed()` squashes old
  auto-commits inline. `BackupManager.create()` prunes before creating (not after).
- **`C0Result.from_dict`** — now restores `redteam_required` and `a2a_suggested` fields
  (was silently dropping them → red team gate never triggered).
- **`cognitive/router.py`** — removed `交接` and `deploy` from `_SIMPLE_PATTERNS`
  (these are Complicated+ tasks, not Simple).
- **Red/blue team SOP** — reformed to 1 Opus architect (red) + 1 Opus architect (blue),
  same identity opposing anchors. Self-red-team deprecated (45/100 vs Opus 88-92/100).

### Fixed

- `CbuaPipelineGuard.post_check` renamed to `on_post_tool` (guard was completely inert).
- Guard state now persisted to disk via JSON (was resetting every subprocess invocation).
- `_get_scannable_text` uses `tool_result` not `tool_output` (field name mismatch).

## [1.12.1] - 2026-04-12

Handoff claim verification guard — blocks stop when assistant claims handoff written but git shows no change.

### Added

- **`handoff_claim_guard` module** — detects "claimed handoff but didn't write it" pattern.
  Scans last assistant message for claim keywords (zh/en), verifies git diff for actual
  handoff file changes. Circuit breaker: max 1 block per session. 27 tests.
- **`on_stop` pipeline** wired `handoff_claim` module with `HANDOFF_CLAIM_BLOCK:` prefix.

## [1.12.0] - 2026-04-12

A2A cross-stage communication layer hardened into CBUA pipeline.

### Added

- **A2A display codes** in `cbua_ux.py`: A2A.Query/RedTeam/Delegate/Broadcast (i18n)
- **`C0Result.a2a_suggested`** field — auto-detects when task benefits from
  multi-agent collaboration (keywords: delegate/parallel/red-team/deploy/跨專案)
- **Patent PAT-003** updated with A2A as hardened code evidence

## [1.11.0] - 2026-04-12

Convention Engine + CBUA numbering migration + C0Router redteam field.

### Added

- **`convention_engine` module** — naming/placement/template/reuse enforcement.
  Industry-aligned defaults, fully overridable via `conventions.json`. 23 tests.
- **`C0Result.redteam_required`** field — auto-triggers red team for Complex+
  tasks matching patent/architecture/irreversible patterns.

### Changed

- **CBUA numbering migration** — C0-C5/A0-A5 → C0-C3(Cognize)/B0-B5(Budget)/
  U0-U3(Unify)/A0-A5(Act). 22+ files, 62+ replacements across rules, source,
  KB, memory. Golden tests updated. Zero functional regression.

## [1.10.0] - 2026-04-12

CBUA UX: standardized display codes for all pipeline stages and thinking tools.

### Added

- **`cbua_ux` module** — 55+ display codes covering C(Cognize)/B(Budget)/U(Unify)/A(Act)
  stages, thinking tools (三層思考/CoT/反轉/蘇格拉底), WIREDO verification, blue/red team,
  knowledge depth tiers, convention SOP, and six laws. i18n-aware (en/zh-TW).
- **Helper functions**: `cbua_format()`, `cbua_wiredo()`, `cbua_three_layer()`,
  `cbua_redteam_verdict()`, `cbua_clean_write()` — all guards/hooks MUST use these.
- **36 tests** covering enum completeness, i18n labels, format output, and helpers.

## [1.9.0] - 2026-04-12

FieldRead: selective field extraction for token-efficient handoff/memory injection.

### Added

- **`field_read` module** — parses structured markdown (handoffs, memory) and
  extracts only task-relevant sections within a token budget. Fills the
  previously-empty `DynamicSlots.handoff_summary` slot in `PromptEngine`.
- **ZIQ breakeven gate** — compression only activates when source content
  exceeds 2500 tokens (empirically validated Pareto threshold). Below that,
  content passes through uncompressed to avoid net-negative quality loss.
- **50 tests** covering section parsing, scoring, keyword extraction, memory
  matching, handoff discovery, ZIQ gate, and orchestrator integration.

### Changed

- **`PromptEngine.assemble()`** now populates `handoff_summary` via
  `build_field_context()` when workspace and task_prompt are provided.

## [1.8.1] - 2026-04-12

4 LLM semantic guards: PIIGuard + JailbreakGuard + DataLeakGuard + ToxicityGuard.

## [1.8.0] - 2026-04-12

Cognitive inject wired to C0Router — dynamic depth by task complexity.

### Changed

- **`cognitive_inject` three-layer depth now driven by C0Router** —
  parent session no longer always injects "full" (~320t). Simple tasks
  get "minimal" (~50t), Complicated get "standard" (~120t), Complex+
  get "full". Saves 50-70% inject tokens on simple tasks. Subagent
  routing unchanged (identity-driven).

## [1.7.1] - 2026-04-12

Install tiers: default full-power, opt-in lite.

### Changed

- **Default install now includes LLM deps** — `pip install cc-cortex`
  gives you regex guards + LLM semantic guards out of the box.
  No more guessing which extra to pick.
- **Install tiers**:
  - `pip install cc-cortex` — full power (LLM included, ~5MB extra)
  - `pip install cc-cortex[all]` — everything (LLM + RAG)
  - `pip install cc-cortex[rag]` — adds RAG retrieval (~2GB)
  - `pip install cc-cortex[llm]` — LLM only (same as default now)
  - `pip install cc-cortex[lite]` — zero-dep, regex only
- Existing users can upgrade tiers seamlessly:
  `pip install --upgrade cc-cortex[all]` adds missing deps without
  reinstalling.

## [1.7.0] - 2026-04-12

Break the zero-dep ceiling: LLM-backed semantic guards.

### Added

- **`cc-cortex[llm]` optional dependency** — `pip install cc-cortex[llm]`
  adds `anthropic` + `openai` SDK. Core remains zero-dep.
- **`LLMGuard` abstract base class** — subclass it, set `judge_prompt`,
  get LLM-level semantic judgment with fail-open fallback. If no LLM
  SDK installed, returns None (ALLOW) so regex guards still protect.
- **`SemanticInjectionGuard`** — first concrete LLM guard. Detects
  prompt injection via semantic analysis: encoded attacks, multi-
  language injection, indirect injection in data, social engineering,
  role-play injection. Goes beyond what regex can catch.
- Supports Anthropic (Haiku default) and OpenAI (gpt-4o-mini fallback).
  Model configurable via `CC_CORTEX_LLM_MODEL` env var.

### Notes

- `pip install cc-cortex` still zero-dep (regex-only, 0ms).
- `pip install cc-cortex[llm]` adds semantic depth (~500ms per check).
- Two layers work together: regex catches obvious attacks instantly,
  LLM catches sophisticated attacks that regex misses.
- 12 new tests (all mocked, no real LLM calls in CI).
- Tests: 3368 → 3380 (+12).

## [1.6.0] - 2026-04-12

Ship-ready release: PyPI metadata overhaul + A2A wiring + Aegis
integration + evolution plan.

### Added

- **A2A agent wired to CCC pipeline** — `a2a/agent.py` L1 layer now
  uses `create_default_pipeline().run_pre_tool()` instead of external
  `agent_shield`. L4 uses `run_post_tool()`. Agent Card updated to
  "CC-Cortex Guard Agent" v1.6.0. Smoke-tested: `rm -rf /` →
  destruction_guard → BLOCKED via L1_ccc_pipeline.
- **Aegis integration** — `persona-api/guards.py` `classify_safety()`
  now tries CCC pipeline first (55+ guards), falls back to local 9
  regex if cc-cortex not installed. Zero breaking change to Aegis API.
- **PyPI metadata overhaul** — keywords expanded to 14 terms
  (ai-safety / guardrails / llm-guard / llm-security /
  prompt-injection / agent-governance / agent-safety / a2a).
  Classifiers: +Security +AI. Description rewritten for PyPI search.

### Changed

- Description: "The Cognitive Layer for Claude Code" → "AI behavior
  governance for Claude Code — 55+ guards, input rewriting,
  LLM-as-Judge, A2A safety agent, zero-dep core"

### Notes

- Includes all 1.5.1 content (ThreatPatternsGuard 6 Aegis regex +
  c0_router context_tokens signal).
- Tests: 3368. ruff clean. Version SSOT aligned.

## [1.5.1] - 2026-04-12

Aegis threat pattern port + c0_router context-token signal.

### Added

- **`ThreatPatternsGuard`** — 6 regex threat categories ported from
  Aegis `persona-api/guards.py`: harmful_content, social_engineering,
  persona_hijack, role_switch_attack, no_restrictions_mode,
  context_reset_attack. Registered in QUALITY layer (step-back enabled
  for legitimate security research). 18 tests.
- **`C0Router.classify(context_tokens=)`** — new optional signal
  ported from Aegis ZIQ PTME "memory" dimension. Context >100k tokens
  escalates to at least complicated; >250k to complex. Drives RAG
  depth and subagent dispatch strategy.

### Notes

- Tests: 3350 → 3368 (+18 threat_patterns). ruff clean.
- Version SSOT aligned.

## [1.5.0] - 2026-04-12

Focus of this release: **CBUA Law #3 hardening** — a second verification
mode on `premise_gate` that blocks writes when the assistant references a
platform limitation (CC hook / L1-L8 / `updatedInput` / `type:"prompt"`)
without having WebFetched current official docs first. Hardens the
"ceiling misalignment" anti-pattern that got CCC 1.3.0 to KILL H1 based
on CC limitations that had already been removed several versions earlier.

Same release ships a `paths` glob frontmatter upgrade on four
file-bound skill templates so Claude Code auto-surfaces them when the
user edits matching files.

### Added

- **`premise_gate` Mode 2 — ceiling verification**. New regex family
  `_CEILING_WORDS` detects references to:
  - `L1`-`L8` limitation ids
  - `updatedInput` / `hookSpecificOutput` / `SubagentStart payload`
  - `type: "prompt"` hook
  - `hook/subagent/skill can't / cannot / doesn't support`
  - Chinese: `hook 無法 / 不支援 / 沒辦法 / 只能`, `CC 不支援 / 無法`,
    `平台限制 / CC 限制 / api 限制`
  Once detected, `PremiseGate.check()` denies write tools until the
  assistant WebFetches an official CC docs host (`code.claude.com`,
  `docs.claude.com`, `docs.anthropic.com`,
  `github.com/anthropics/claude-code`) — same dual-phase state machine
  as the existing external-constraint mode, so the two coexist without
  stepping on each other. 13 new tests in `tests/test_premise_gate.py`
  cover both phases, bypass logic, simple-task skip, and coexistence
  with Mode 1.
- **Skill `paths` glob frontmatter** on four file-bound skill
  templates, leveraging Claude Code's native auto-surface on matching
  files:
  - `debug_loop` → `**/*test*.py`, `**/*spec*.ts`, `**/*_test.go`,
    `**/tests/**`, `**/__tests__/**`, `**/conftest.py`
  - `decision_journal` → `**/ADR-*.md`, `**/adr/**/*.md`,
    `**/RFC-*.md`, `**/rfcs/**/*.md`, `**/DECISION-*.md`,
    `**/docs/adr/**`
  - `handoff` → `**/交接*.md`, `**/handoff*.md`, `**/HANDOFF*.md`,
    `**/_handoffs/**`, `**/06_Handoffs/**`
  - `learning_loop` → `**/corrections-queue.jsonl`,
    `**/corrections.jsonl`, `**/MEMORY.md`, `**/feedback_*.md`,
    `**/learnings/**`
  Each template also gains an `allowed-tools` list so the skill can
  load without per-use permission prompts.

### Rationale

`feedback_ceiling_misalignment.md` now documents three concrete
self-demos of this anti-pattern in a single week:

1. CCC 1.3.0 L3/L5 误判 — `updatedInput` + `type:"prompt"` were CC
   features, not limitations. The red team caught it; 1.4.0 C1/C6
   closed it.
2. 1.5.0 SOP A design self-demo — initial design assumed
   `on-session-start.py` could not inject version-check reminders and
   proposed a MEMORY.md cache hack. Red team pointed out CCC is its
   own library and can add a `prompt_submit` regex injector directly.
3. 1.5.0 V2 sentinel self-demo — almost wrote a brand-new
   `consecutive_failure_guard` before discovering that
   `sentinel.ConsecutiveFailGuard` already handles exactly this case
   (max_fails=3 + sig-based counting + i18n "三敗鐵律" prescription).

The recurrence made it clear that a regex-based pre-flight check at the
CBUA A0 layer was the right fix — hence `premise_gate` Mode 2.

### Notes

- No runtime dependencies added. Mode 2 uses stdlib only.
- 3350 tests passed / 3 xfailed (was 3337 in 1.4.0 → +13 ceiling tests).
- Version SSOT aligned across `pyproject.toml`, `__init__.py`,
  `CHANGELOG.md`.

## [1.4.0] - 2026-04-12

Focus of this release: **input rewriting** (C1 `PreToolUseRewrite`
layer) and **LLM-as-Judge reopen** (C6 `prompt_hooks` module). Closes
two of the six red-team ceiling gaps identified in 1.3.0: guards can
now rewrite `tool_input` in place, and CCC ships curated judge prompts
for Claude Code's `type: "prompt"` hook runtime.

### Added

- **`GuardAction.REWRITE` + `GuardResult.rewrite(updated_input, …)`**
  — third outcome state beyond ALLOW/DENY for PreToolUse only. Guards
  that return REWRITE replace `ctx.tool_input` in place; remaining
  guards see the rewritten version and can still DENY it. Pipeline
  emits `hookSpecificOutput.updatedInput` + an `additionalContext`
  note so the user sees what changed. Rewrites are required to be
  *narrow*, *idempotent*, and *visible*.
- **Three shipped rewriters** (`cc_cortex.guards.rewrite_guards`):
  - `BashDryRunRewriter` — `rm -rf .` / `rm -fr <glob>` → `echo
    '[dry-run] would have run: …'`, preserving the original form
    as a shell comment.
  - `WriteSecretFileRewriter` — `Write(.env | credentials.json |
    secrets.yaml | …)` → `.env.example` / `credentials.example.json`
    / `secrets.example.yaml`. Preserves flavor suffixes
    (`.env.prod` → `.env.example.prod`). Edit is intentionally
    left alone (rotation, not materialisation).
  - `BashPipeToShellRewriter` — `curl … | bash` / `wget … | sh`
    rewritten to `curl -fsSL URL -o /tmp/cc-cortex-download.sh &&
    echo 'inspect before running'`.

  Registered early in the QUALITY layer so downstream guards
  (SecretScan, ExfilGuard, …) see the rewritten input.
- **`cc_cortex.prompt_hooks`** — LLM-as-Judge reopen (1.3.0 H1). CCC
  ships three curated judge prompts as module constants plus a
  settings.json installer that writes `type: "prompt"` hook config
  for Claude Code's built-in prompt-hook runtime. CCC itself never
  calls an LLM (core stays zero-dep + L3 hook-side LLM ban). Judges:
  - `HALLUCINATION_JUDGE` — PostToolUse on Write|Edit, flags
    unsourced factual claims.
  - `EXCUSE_SCANNER_JUDGE` — Stop event, flags hedging language
    when declaring work done.
  - `CODE_QUALITY_JUDGE` — PostToolUse on Write|Edit, flags the
    four cardinal sins (dead code, swallowed errors, over-
    engineering, backdoor defaults).
  Installer API: `install_prompt_hooks(settings_path, *, judges=…,
  dry_run=…)`, `uninstall_prompt_hooks(…)`, `list_installed_judges(…)`.
  All functions are idempotent, atomic, marker-tagged so user-written
  hooks in the same settings.json are never touched. Default model
  is `claude-haiku-4-5-20251001` (overridable per judge).
- **`cc_cortex` top-level exports** — `BashDryRunRewriter`,
  `BashPipeToShellRewriter`, `WriteSecretFileRewriter`, `PromptJudge`,
  `HALLUCINATION_JUDGE`, `EXCUSE_SCANNER_JUDGE`, `CODE_QUALITY_JUDGE`,
  `ALL_JUDGES`, `build_hook_config`, `install_prompt_hooks`,
  `uninstall_prompt_hooks`, `list_installed_judges`. Strangers can
  now reach the 1.4.0 surface with a single `import cc_cortex`.

### Changed

- **`GuardResult.to_hook_dict()`** — gained a REWRITE branch that
  emits `hookSpecificOutput.hookEventName="PreToolUse"` +
  `permissionDecision="allow"` + `updatedInput` so the CC runtime
  re-dispatches with the replacement input. ALLOW/DENY shapes are
  unchanged (regression-tested).
- **`GuardPipeline.run_pre_tool()`** — gained rewrite chain
  handling. Each REWRITE replaces `ctx.tool_input` via
  `dataclasses.replace`, records a `↻ <guard-name>: <reason>` note,
  and continues iteration so later guards see the rewritten version.
  A downstream DENY still wins over an upstream REWRITE.

### Notes

- No runtime dependencies added. `prompt_hooks` uses stdlib only
  (`json`, `pathlib`, `tempfile`).
- BoundaryGuard audit: no personal paths introduced.
- Test count: 3268 → 3337 (+30 `test_rewrite_guards.py` +39
  `test_prompt_hooks.py`, net +69).

## [1.3.0] - 2026-04-11

Focus of this release: **patch-loop burst tracking** (A2c) + a round of
red-team hardening + release-engineering gaps. Two experimental features
(P1 Prompt Compression, P2 Persona Router) shipped in the initial merge
`a63c4018` were later removed after red-team review — see "Removed"
below for why. Never published to PyPI in the intermediate state.

### Added

- **ErrorRecovery burst tracking** — `record_burst(operation, category)`
  with a configurable 10-minute consecutive window, backed by
  StateStore's `tool_failure_burst` namespace (read-modify-write
  locking, project-flat bucket so patch loops span Claude Code session
  restarts). Companion methods: `burst_status(operation, category, now=)`,
  `clear_burst(operation=None)`, and the classmethod
  `classify_burst(consecutive, total)` returning
  `"escalate"`/`"prescribe"`/`"normal"` (escalate priority when
  `consecutive >= 2`). The `+1` consecutive fix-up now lives inside the
  primitive so callers cannot be off by one.
- **SSOT version guard** — `tests/test_version_sync.py` parses the
  `[project].version` field of `pyproject.toml` and the first
  `## [X.Y.Z]` header of `CHANGELOG.md` and asserts they match
  `cc_cortex.__version__`. Drift now fails the suite.

### Changed

- **`on_post_tool_failure` hook** — 259 → ~180 lines. The C1 patch-loop
  detector's mechanism (time-windowed consecutive counting + history
  persistence) now delegates to `ErrorRecovery.record_burst`; the
  hook keeps presentation (six-category classifier, prescription
  strings, Chinese C1 escalation message, stdout JSON shape,
  `_is_user_denial` skip). State moved from the project-scoped JSONL
  `tool_failures.jsonl` to StateStore's project-flat
  `tool_failure_burst` namespace. Keeping the bucket flat (not
  session-scoped) means patch loops that span `claude` session
  restarts still trigger — matches pre-1.3.0 JSONL semantics.
- **`_is_user_denial` no longer swallows system failures** (red team
  #1-F1). The original substring match on `"denied"`/`"cancelled"`/
  `"interrupted"` ate `"permission denied"`, `"connection denied"`,
  `"timed out / cancelled"`, `"EINTR interrupted"` — exactly the
  failure modes patch-loop tracking must count. New rule: skip only
  on unambiguous user markers (`"user rejected"`, `"denied by user"`,
  `"cancelled by user"`, …); anything with `permission` / `access` /
  `timeout` / `connection` / `network` / `socket` / `errno` / `eintr`
  / `refused` / `no such` is counted as a real failure. Golden
  tests (`test_g1b_system_failures_are_counted`) pin the new behavior.
- **Hook `main()` is best-effort** (red team #1-H2). Burst tracking
  crashes, lock contention, or corrupt state files no longer propagate
  to Claude Code — `_dispatch` lives inside an outer `try/except
  Exception: return`.
- **`StaticCache.load()` stopped hard-coding author paths** (red team
  #3 坑 1). The initial merge read
  `projects/cc-cortex/src/cc_cortex/cognitive_anchor.py` and
  `.claude/rules/00-L0.md` — both absolute to the author's workspace.
  New load order: `CC_CORTEX_IDENTITY_PATH` env var →
  `<workspace>/.cc_cortex/identity.md` → (empty); iron laws come from
  `CC_CORTEX_L0_RULES_PATH` → `<workspace>/.cc_cortex/l0.md` →
  `<workspace>/CLAUDE.md` → (empty). CCC hard rule #1 (no personal
  paths in source) upheld. Dead helper `_extract_anchor_identity`
  removed.
- **`cc_cortex.__version__` aligned with `pyproject.toml`** — was
  stuck at `"1.1.0"` before this release; a stranger calling
  `cc_cortex.__version__` at runtime got a value that matched no
  released version.

### Fixed

- **`record_burst` / `burst_status` tolerate corrupt state files** (red
  team #1-H1). A malformed event with a tz-naive or unparseable `ts`
  no longer raises `TypeError` from `datetime` comparison — unparseable
  rows are skipped, naive datetimes are coerced to UTC.
- **`burst_status()` accepts injectable `now`** (red team #1-M4). CI
  on slow runners could race the 10-minute window boundary in tests
  seeded relative to `datetime.now()`. The writer already took `now=`;
  the reader now does too.
- **`StateStore.read_modify_write` creates the namespace directory**
  — pre-existing latent bug where the `O_EXCL` lock sentinel would
  fail to land in a missing parent directory on the first RMW of a
  namespace. Added `os.makedirs(..., exist_ok=True)` before the lock
  attempt. Surfaced the moment any consumer used RMW on a fresh
  cache dir.
- **Wheel/sdist artifact symmetry** (red team #1-F3). The wheel
  `force-include` was missing
  `src/cc_cortex/_cognitive/__init__.py`; sdist already had it via
  the sdist force-include fix. `pip install cc-cortex` and
  `pip install --no-binary=:all: cc-cortex` now produce identical
  package trees.

### Removed

- **P1 Prompt Compression** — `PromptEngine.compressed_context` slot,
  `PromptEngine.compress_text()`, `PromptEngine.set_compression_backend()`,
  the `large_context` / `compression_query` kwargs on `assemble()`,
  the `_compress_text()` helper, and the `compressed_context` priority
  entry in `DynamicSlots.render()`. Red-team review found zero
  non-test consumers of the slot and confirmed that the CC hook system
  does not consume `PromptEngine.assemble()`'s return value — the
  only hook-side injection point is `additionalContext` as an
  append-only plain string. Compression belonged to a different
  architectural layer. Removing it kills a dead API before it hit
  PyPI and can be redesigned properly in a future release if a real
  hook-side consumer emerges.
- **P2 Persona Router** — `cc_cortex.persona_router` module
  (`PersonaRouter`, `infer_mode`, `MODES`, `DEFAULT_MODE`,
  `RouteExplanation`), `cognitive_inject.build_persona_directive`,
  `_PERSONA_DIRECTIVES`, the `persona_directive` dynamic slot, and
  the `persona_mode` / `session_goal` kwargs on `PromptEngine.assemble()`.
  Red-team review: zero non-test consumers; inferred mode had no
  path back to the CC-side `/mode` skill (which is UI-driven);
  keyword-based routing on free-text goals was too brittle to be the
  sole signal. Belongs in the CC-side consumer, not in the CCC library.
- **Plan item H1 LLM-as-Judge** — killed in the initial merge because
  hook-internal LLM calls violate CCC rule #4 and hit L1/L3 double
  hard limits. **Red team #2 found that Claude Code 2026-04 officially
  supports `type: "prompt"` hooks with an internal Haiku judge.** The
  KILL premise is obsolete and H1 should be **reopened** in a future
  release via the official prompt-hook path — see handoff `1.4.0 方向`.
- **Plan item C1 Demo GIF** — killed. Not a requirement for current
  phase.

### Internal / Tests

- `tests/test_on_post_tool_failure_golden.py` — 28 golden cases
  pinning observable hook behavior: window boundaries, reset
  semantics on category/tool mismatch, escalation priority over
  prescription, confidence side-effect, ImportError graceful degrade,
  200-entry history cap, system-failure counting (G1b), hook JSON
  output shape, and a byte-exact snapshot of the Chinese C1
  escalation message.
- `tests/test_error_recovery.py` — +18 burst tests covering first
  record, consecutive same pair, reset on op/category mismatch,
  inside/outside the 10-min window, configurable window, history cap,
  injectable `now`, `classify_burst` four-quadrants (escalate/
  prescribe/normal/escalate-priority), `clear_burst` (all +
  operation-scoped), session isolation, `burst_status` non-mutation,
  sequential RMW safety.
- Tests: 3214 → 3268 for this release (A2c + hardening + SSOT; the
  intermediate merge briefly peaked at 3320 when P1/P2 tests were
  still in the tree). 0 regressions vs 1.2.0.
- `python -m build` + `twine check dist/*` → PASSED on both wheel
  and sdist. Wheel verified to contain `cc_cortex/_cognitive/__init__.py`
  plus all 9 md files and `__version__ == "1.3.0"`.

## [1.2.0] - 2026-04-10

### Added

- **CBUA v2**: Six laws (+ Honesty Law #6), C4 intent anchoring + hallucination detection, C5 admit-not-knowing, A0 premise verification, eleven native capabilities
- **PremiseGate**: Block execution when external constraints (competition/spec) detected but source material unread. Co-occurrence keyword pattern to reduce false positives
- **HallucinationGuard**: Detect unsourced claims (percentages, citations, URLs, dates) in written content. No blanket exemption — checks every Write
- **IntentAnchorGuard**: Periodic re-injection of user's original intent from `user_prompt` namespace. Prevents red-team drift and scope creep
- **SedimentationGate**: Block session stop when corrections exist but not sedimented to feedback/KB. Session-scoped cutoff + 100-char minimum content
- **VerifyBeforeWriteGuard**: Scan Write/Edit for unverified imports, API endpoints, version pins. Track Read/Grep as verification evidence
- **InitialIntentProbe**: Probe user's root purpose for Complicated+ tasks. Anti-RLHF people-pleasing reminder for Complex+ tasks
- **TaskOrchestrator**: Session-level task decomposition, subtask tracking, milestone reporting
- **CostTracker**: Per-session token/cost tracking with budget ceiling and 80% alert
- **ProgressReporter**: Formatted milestone reports integrating orchestrator + cost data
- **ErrorRecovery**: Four-level recovery strategy (retry → degrade → escalate → pause) with per-operation failure tracking
- **RAG multi-namespace**: 5 predefined namespaces (knowledge/memory/cognition/skills/context) with `create_namespace_index()` factory
- **BM25 hybrid search**: `hybrid_search()` with RRF fusion. Graceful fallback when bm25s not installed
- **Reranker interface**: `reranked_search(reranker=obj)` for pluggable reranker models
- **ZIQ query router**: `route_query(query, confidence)` routes to 1-5 namespaces based on α_t
- **FTRL L1 injection**: `on_prompt_submit` step 4 — top-3 FTRL-weighted learnings injected as session reminders
- **FTRL weight function**: `ftrl_weight(count, last_seen)` with exponential decay (λ=0.1, ~7-day half-life). Future timestamp protection
- **HonestyGate overconfidence**: Detect "definitely/guarantee/一定/保證" in Complex+ tasks, warn to quantify uncertainty
- **UIVerifyGuard auto-trigger**: Inject screenshot verification hint after ≥3 UI file edits (even without deploy)
- **kb_rag KB Skill**: Five-namespace architecture docs, SOP, design decisions
- **MCP screenshot tool**: WIREDO visual verification (Playwright + windows-mcp fallback)
- **MCP progress tool**: TaskOrchestrator + CostTracker integrated report
- **MCP cost tool**: Token cost breakdown with budget ceiling and alerts
- **Token checkpoint system**: 4-zone (GREEN/YELLOW/ORANGE/RED) with C1-C5 thresholds
- **Checkpoint trigger**: `checkpoint`/`檢查點` writes handoff snapshot without stopping work
- **Compliance mapping**: NIST AI RMF + ISO 42001 full guard mapping (docs/compliance-mapping.md)

### Changed

- Guard pipeline: 44 → 55 guards (Security 7 / Quality 39 / Cognitive 9)
- Tests: 2884 → 3138
- MCP tools: 12 → 15 (+ screenshot, progress, cost)
- Token zones: 3-zone (GREEN/YELLOW/RED) → 4-zone (+ ORANGE for subagent mode)
- HonestyGate: Fixed cross-session state pollution (hardcoded "state" → ctx.session_id)
- UIVerifyGuard: Merged double StateStore write into single atomic write
- knowledge.py `get_pending_promotions`: Added `use_ftrl` parameter (default False for backward compat)
- pyproject.toml: Added `bm25s>=0.2` to rag optional deps

### Fixed

- PremiseGate: Removed `len > 200` bypass (any Read would unlock). Now requires content matching constraint keywords
- HallucinationGuard: Removed `evidence_count >= 3` blanket exemption
- IntentAnchorGuard: Fixed intent capture from tool_result (architectural error) → now reads from user_prompt StateStore namespace
- SedimentationGate: Fixed 30-minute global cutoff → session-scoped cutoff. Added 100-char minimum to prevent empty-file bypass
- FTRL weight: Future timestamps now return 0.0 instead of maximum weight

## [1.0.0] - 2026-04-08

### Added

- PromptEngine: dynamic prompt assembly with anti-drift re-injection
- ThinkingDepthGuard: Read:Edit ratio degradation detection (#42796)
- AgentSupervisor: contract-based subagent verification
- ZIQRetrieval: EMA adaptive RAG source weights
- MemoryPalace: spatial structured memory (MemPalace-inspired)
- C0Router: CBUA complexity classifier
- Facade subsystem packages: inject/, token/, agent/, memory/, prompt/, handoff/

### Changed

- tct/ renamed to ziq_control/ (TCN→ZIQ brand unification)
- StateStore: added read_modify_write() atomic operation with file lock
- on_post_tool.py: integrated ThinkingDepthGuard + Anti-Drift + ZIQ feedback
- on_subagent_stop.py: integrated AgentSupervisor verification

### Fixed

- ReDoS protection in AgentSupervisor regex matching
- Path traversal prevention in file existence checks
- EMA weight saturation with periodic decay
- MemoryPalace user memories protected from eviction
- verify_wiring whitelist to prevent full workspace scan

## [0.5.0] - 2026-03-16

### Added

- **Hard Deny System**: 9 hard-deny guards (destruction, token gate, agent cap, read-first, bash/python, sentinel, file tracker, window guard) with fail-open architecture
- **Feature Config**: `feature_config.py` with risk metadata — granular enable/disable per guard
- **Token Gate**: Block Agent spawn at 140K+ context tokens with real transcript-based usage
- **Handoff Engine**: `check_token_gate()` + `check_handoff_reminder()` + session summary generation
- **Handoff Reminder**: PreToolUse additionalContext warn when >80K tokens + 3+ files modified without handoff update
- **Streak UX**: COMBO counter with milestone celebrations (🔥x5, x10...) + error fix tracking
- **Stderr Dual-Channel**: CRITICAL/MILESTONE messages emit to both stderr (user) and additionalContext (AI) — `[SHOW USER VERBATIM]` protocol
- **Session ID Unification**: Consistent `session_id` propagation across all hooks and guards

### Changed

- **406 tests** total, all passing, ruff clean
- **Soft→Hard deny migration**: All guards upgraded from additionalContext warnings to `permissionDecision: deny`
- **on_pre_tool pipeline**: 11-step pipeline (8 deny + 3 warn) with short-circuit on first deny
- **on_post_tool throttle**: CRITICAL (always show) / MILESTONE (interval-based) / normal (AI only) classification

### Fixed

- **Destruction Guard R4 bypass**: Fixed regex pattern that allowed `#DESTROY_CONFIRMED` in wrong position
- **README inconsistencies**: 4 doc/code mismatches corrected
- **Streak UX trigger**: Fixed threshold from `>=` to `% interval` for consistent milestone firing

## [0.4.2] - 2026-03-15

### Added

- **CC BashGuard**: Long-running command detection (server/watch/while/tail -f) warns if `run_in_background` not set
- **CC PythonGuard**: `python -c` multiline (>5 lines) warns to use script file instead
- **CC WhitepaperGuard**: Core IP keyword detection on external paths — deny write
- **CC HandoffGuard**: Session >20min without handoff update → stderr reminder (once per session)

### Changed

- **on-pre-tool.py**: CC-specific guards chained after CCC pipeline (fail-open, <5ms marginal cost)
- **on-stop.py**: Added `_check_handoff_reminder()` with per-session dedup via cache file
- **Rule 29 (destruction-guard)**: Simplified from 44→6 lines (fully hooked, ~200 tokens returned to attention budget)

## [0.4.1] - 2026-03-12

### Added

- **Warn Generators**: `warn_generators.py` module with named warning producers (awareness/scavenger/char_limit)
- **warn_router integration**: PostToolUse warnings now route through mode/cooldown/budget filtering
- **Scavenger generator**: Stale session marker detection with `/tidy` prompt
- **Char limit generator**: Oversized content warnings (>500 lines / >50K chars)

### Changed

- **on_post_tool.py**: Refactored to two-tier pipeline (always-on + routed warnings)
- **CC on-post-tool.py**: Simplified to pure relay (v6.0), CCC handles all throttling
- **266 tests** total, all passing, ruff clean

## [0.4.0] - 2026-03-12

### Added

- **Prompt optimization**: All 27 modules with refined descriptions and detection patterns
- **Security hardening**: 14→20 injection patterns, Unicode normalization, nested encoding detection
- **Sentinel v2**: 4→6 layers (+ token budget burn-rate, oscillation detection)
- **Knowledge v2**: Multi-language corrections, staleness detection, conflict resolution
- **Agent Gate**: Misuse detection for unnecessary agent spawning
- **MCP Server**: JSON-RPC stdio transport for Claude Code native integration
- **Three-layer coordination**: Strategy Pattern extraction for cognitive layer
- **Skills system**: `cc-cortex skills install/list/create` + SKILL.md templates
- **Process Guard**: ctypes-based Windows process tree enumeration
- **Three Strike v2**: Auto-escalation with kb→rules→hook pipeline
- **Status dashboard**: `/status` skill with terminal box-drawing UI
- **Window Guard**: IDE focus detection for notification suppression

### Changed

- **228 tests** total, all passing, ruff clean
- **CC/CCC boundary**: Thin wrapper architecture — CC hooks call CCC APIs
- **Strategy Pattern**: Coordination logic extracted from monolithic handlers
- **Brand**: "The Cognitive Layer for Claude Code"

### Fixed

- **Session cleanup**: stdin JSON session_id replaces non-existent env var
- **Process guard**: ctypes CreateToolhelp32Snapshot replaces unreliable psutil

## [0.3.0] - 2026-03-09

### Added

- **Autopilot module**: Autonomous task execution with MODULES registry + CLI 8 subcommands (`cc-cortex autopilot status/start/stop/config/logs/history/retry/reset`)
- **Destruction Guard module**: R0-R4 risk classification for tool calls, backup engine, audit logging, integrated into `on_pre_tool.py`
- **Backup CLI**: `cc-cortex backup list/cleanup/restore/pin/unpin` — 5 subcommands for managing safety backups
- **Compact enhancement**: Transcript user-command extraction + full task reading + dual-format output (JSON+TXT)

### Changed

- **1196 tests** total (up from 502), all passing
- **Dead code cleanup**: Removed 4 unused modules, `test_guard` refactored to CLI-only
- **Sentinel**: Fixed lint-fix false positive detection
- **Module registry**: MODULES dict now includes autopilot, destruction_guard, backup_cleanup

### Fixed

- **Sentinel lint-fix**: False positive on legitimate lint-fix operations no longer triggers brute-force detection

## [0.2.0] - 2026-03-04

### Added

- **Security module**: 14-pattern prompt injection scanner with 100% detection / 0% FP rate
- **Webhook module**: HTTP POST session events to Slack/dashboards
- **TypeScript SHA256 caching**: cache-hit latency <2ms (vs 15s full recheck)
- **Benchmark suite**: 7 benchmarks with competitor comparison (`cc-cortex benchmark`)
- **CLI `doctor` command**: health check for hooks, settings, config
- **CLI `uninstall` command**: clean removal of all hooks
- **CLI `benchmark` command**: run and display performance benchmarks
- **Hook templates**: 5 installable hooks (session-start, pre-tool, post-tool, stop, extract-learnings)
- **Red-team test suite**: 12 attack scenarios (lock racing, injection, path traversal, Unicode bypass)
- **Stress tests**: 100-thread lock contention, 4-session concurrent writes
- **Fuzz tests**: hypothesis-based random input for correction detection and security scanning
- **System failure tests**: corrupt JSON, 0-byte files, stale locks, disk simulation

### Changed

- **502 tests** total (up from 0), coverage >80%
- **Atomic I/O**: TOCTOU fix — PID-verified locks replace brute-force deletion
- **Multi-instance**: recursive glob support for conflict detection
- **Config**: singleton reset support, parameter comparison on reload
- **Sentinel**: namespaced state files (no cross-session overwrites)
- **Scavenger**: unified private handoff detection shared with handoff module
- **Token warnings**: 4-tier thresholds configured out of the box
- **Version**: 0.1.0 (Alpha) → 0.2.0 (Beta)

### Fixed

- **C1**: CLI enable/disable now actually reads/writes `cc_config.json` modules
- **C2**: `cc-cortex init` copies real hook templates (not missing source files)
- **C3**: Full test suite added (was zero tests)
- **C4**: `scavenger.py` — `cfg.raw("scavenger", {})` replaces broken `.get()` call
- **C5**: Config singleton respects new `hooks_dir` parameter
- **C6**: `knowledge.py` — `UnboundLocalError` on `corrections[-1]` fixed

## [0.1.0] - 2026-03-04

### Added

- **Core layer**: Atomic I/O with file locking, session ID generation, state compaction, cross-platform notifications, configuration loader
- **Sentinel**: 4-layer brute-force detection (tool repeat, edit stagnation, analysis paralysis, scope creep)
- **Knowledge**: Auto-learning loop with correction extraction and pattern promotion
- **Multi-instance**: File-level session locking with zombie detection and conflict resolution
- **Handoff**: Structured handoff file management with 3-zone layout and auto-GC
- **Quality**: C4 quality scoring (completeness, correctness, focus, efficiency)
- **Evolution**: Three daily reflections + entropy reduction automation
- **TypeScript**: Automatic `tsc --noEmit` validation within 15s
- **CLI**: `cc-cortex init`, `cc-cortex enable/disable`, `cc-cortex status`
