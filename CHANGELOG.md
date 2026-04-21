# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2.13.0] - 2026-04-21

Minor with **one documented breaking change** (`gaia_meta_router.select_arm`
now returns `tuple[Arm, int]`). Lands the three delayed extensions from
cc_b2c962dc red-blue CBUA 3-Opus verdict (see
`_AI_BRAIN/05_Planning/gaia-meta-router-n-aware-2026-04-21.md` + MEMORY #88
+ `~/.claude/skills/kb_cognitive_layer_boundaries/mas_14_defects_crosswalk.md`):

1. **E extension**: N-aware `select_arm` — breadth-aware routing
2. **E extension 2**: `fidelity_delta` module — subagent fork in/out
   information-loss measurement (Cemri #8 DPI cover, zero breaking)
3. **D/C extension**: MAS 14 crosswalk + routing-primary rule propagated
   into kb skill + Perpetuo ↔ Concinno A2A bridge spec appendix

### ⚠ BREAKING — `gaia_meta_router.select_arm` now returns `tuple[Arm, int]`

Callers of `concinno.select_arm` (or `from concinno.gaia_meta_router
import select_arm`) must unpack the new tuple:

```python
# Before (2.12.x)
arm = select_arm(task)

# After (2.13.0)
arm, n = select_arm(task)          # n = parallel subagent count
# or use the diagnostic variant:
decision = select_arm_with_reason(task)
arm, n, reason = decision.arm, decision.n, decision.reason
```

`n` is the **parallel subagent count** (breadth / fan-out width): SAS=1,
MAS=2-3, hybrid=3-4. Addresses Cemri et al. (2025) MAS failure mode #14
N-aware depth budget by making breadth atomic with arm selection, so
callers don't guess a fan-out after the fact (previously `gaia_agent.py`
hard-coded guesses that either over-fanned Level-1 tasks or under-fanned
Level-3 synthesis). Budget pressure applies two floors:

- `remaining/budget < 0.20` → force `("SAS", 1)` (hard budget floor)
- `0.20 ≤ remaining/budget < 0.40` → keep chosen arm, clamp `n` to the
  arm's lower bound (shrink fan-out under pressure)

Migration impact: repo grep confirms **zero external production
callers** at 2.12.2 (only `__init__.py` re-export + `test_gaia_meta_router`
tests import this symbol). Downstream projects that relied on a bare
`Arm` return must update to tuple unpacking.

### Added — `gaia_meta_router` N-aware companions

- **`ArmDecision`** dataclass (`arm`, `n`, `reason`) — frozen, hashable,
  stable across the 2.13.x series. `reason` is one of `"budget-floor"` /
  `"posterior"` / `"hysteresis"`.
- **`select_arm_with_reason(task, ...)`** — same router logic as
  `select_arm` but returns `ArmDecision` for callers that want the
  breadcrumb for logging / diagnostics without parsing tuples.
- **`subagent_count(arm, task, *, budget, remaining_budget)`** — pure
  helper exposing the N selection logic so callers can recompute `n` for
  a given arm without going through the FTRL router again (useful when
  the caller already has a pinned arm from elsewhere).

All three exported from the top-level `concinno` namespace.

### Added — `fidelity_delta` module (Cemri #8 DPI cover)

New module `concinno.fidelity_delta` quantifies structured-field
information loss between a subagent's input prompt and its returned
`final_text`. Reuses `concinno.field_read`'s section / bullet / keyword
parsing so "lost fields" surface as `ElidedSection` records with gist +
confidence, not an opaque scalar.

**Design rationale** (Tran & Kiela 2025 DPI attack / GAIA Sancio plan):
subagent fork collapses a multi-round child loop into a single
`final_text` string. That collapse is **the one fundamental MAS failure
mode with no architectural cover** — no routing trick or N-aware fan-out
recovers information the child chose not to say. `fidelity_delta`
quantifies the loss per spawn so operators spot high-delta forks, the
meta-router can penalize arms / task shapes that systematically lose
information, and ZIQ can learn "when not to fork at all" via outcome
feedback.

**Public API**:

```python
from concinno import FidelityDeltaRecord, compute_fidelity_delta

record = compute_fidelity_delta(in_message, out_message)
# record.delta ∈ [0, 1]       — 1 - preserved / total fields
# record.recall              — preserved / total
# record.fields_in           — scorable field count
# record.fields_preserved    — count with recall ≥ threshold (0.5)
# record.lost_fields         — list[ElidedSection] per dropped field
# record.confidence          — meta-confidence in delta itself
# record.summary             — "3/7 fields preserved (Δ=0.57)"
```

Threshold is configurable via `preservation_threshold` kwarg. Empty
input / empty output return `delta=0` with `confidence=0` — no evidence
of loss is not evidence of loss (caller decides whether empty input is
itself a failure signal). Zero new dependencies; stdlib + existing
`field_read` only.

### Added — MAS 14 defects crosswalk + routing-primary rule (D/C extensions)

Following cc_b2c962dc red-blue CBUA 3-Opus verdict, two documentation
deliverables shipped alongside the code changes (not packaged —
referenced via `_AI_BRAIN/05_Planning/` and `~/.claude/skills/`):

- **MAS 14 × Sancio 1-layer crosswalk**
  (`~/.claude/skills/kb_cognitive_layer_boundaries/mas_14_defects_crosswalk.md`):
  3-axis verdict matrix (academic / cover-verification / red-team attack)
  showing 4 FIX_ARCH full cover + 7 partial + 3 NEEDS_ROUTING fundamental
  (#5 Information Withholding / #8 DPI / #14 N-aware depth) + 1 Perpetuo
  boundary (#13 inter-agent supervision).
- **Routing-primary rule** (`feedback_mas_fundamental_limits_routing_primary.md`):
  routing is upstream primary dispatch, not downstream fallback. 3
  fundamental routing claims remain **untested** until pilot
  (N≥20 task pair per class + paired McNemar p<0.05 + Δ≥+3pp + token
  efficiency not regressed); until pilot passes they do not enter
  `concinno.routing` or any Sancio main API.

### Notes

- `DEPTH_TIER_MAP` values originally proposed in the N-aware plan doc
  were **not shipped** — the landed implementation uses breadth-based
  `_N_BOUNDS` + signal-driven adjustment (`subagent_count()`) instead of
  a fixed tier × arm table. Depth-budget (`max_iter`) routing is a
  2.14.0+ candidate that would extend `ArmDecision` without breaking
  the tuple contract from `select_arm`.
- Per MEMORY #57 paper-kill guard, all three fundamental routing claims
  from MAS 14 crosswalk remain **proposal-tier**. Do not wire to any
  production code path until pilot passes.

## [2.12.2] - 2026-04-21

Minor: reconciles 2.12.1 (PyPI orphan — parallel session built from
dirty working tree, uploaded without git commit/tag) with Session E
cognitive-layer additions. 2.12.1 source files (12 total: cli/
convention_cmd, convention_presets/, 8 new guards, handoff_writeback,
release_authorization) merged into git tree; Session E ZIQ-autotune /
GAIA-meta-router / sweep_guard overlaid on top. All additions ship
**opt-in** — zero behavior change for callers that do not explicitly
enable them, matching CLAUDE.md "small surface, deep behavior"
philosophy.

### Reconciled — 12 files from 2.12.1 (no longer orphan on PyPI)

- `concinno.cli.convention_cmd` wired into `cli/main.py` sub-parser
  (alongside existing `config_cmd`). Workspace convention CLI available
  as `concinno convention ...`.
- `concinno.convention_presets` submodule with `aiking.json` /
  `minimal.json` preset profiles.
- 8 new opt-in guards under `concinno.guards.*`:
  ``benchmark_setup_guard`` / ``deterministic_repro_guard`` /
  ``function_length_guard`` / ``import_cycle_guard`` /
  ``magic_number_guard`` / ``result_file_guard`` /
  ``seed_propagation_guard`` / ``token_efficient_guard``.
  Importable directly; **not** registered in `create_default_pipeline`
  (matches 2.12.1's intentional opt-in design — callers wire via
  `pipeline.register(XGuard())` per project need).
- `concinno.handoff_writeback` — scheduled-task report → handoff TODO
  writeback (fail-open, stdlib-only). Surfaces `_format_todo_entry`,
  `_resolve_handoff_file`, `writeback_scheduled_report`.
- `concinno.release_authorization` — authorization gate separate from
  `destruction_guard`. Two modes: `STRING_MATCH` (chat token `go
  publish <package> <version>`) and `ASKUSER_ANSWER` (via
  `AskUserQuestion`). Disable toggle preserves destruction protection
  while relaxing publish friction. Top-level exports:
  ``AuthorizationConfig`` / ``AuthorizationMode`` / ``PUBLISH_PATTERNS``
  / ``check_authorization`` / ``describe_current_config`` /
  ``detect_publish_operation`` / ``format_required_string`` /
  ``load_config``.

### Added — ZIQ auto-tune gradient + GAIA SAS/MAS/hybrid meta-router + sweep_guard (Session E)

All three modules ship **opt-in** — zero behavior change for callers that
do not explicitly enable them, matching CLAUDE.md "small surface, deep
behavior" philosophy.

### Added — ZIQ auto-tune (generic 3-regime hyperparameter gradient)

- ``concinno.ZIQAutoTuner`` — routes tunable hyperparameters through a
  cold-to-warm gradient driven by ``(value, outcome)`` history. Three
  regimes: ``n < 300`` preset (hardcoded best) / ``300 ≤ n < 500``
  conservative (small LR, large prior) / ``n ≥ 500`` full FTRL-Proximal.
  Matches AI King 2026-04-21 directive "所有能調動的參數能選擇的，只要
  數量大於 300 或 500 都能 ZIQ 自己 CBUA 最佳解".
- Supports three value kinds: ``continuous`` / ``discrete`` /
  ``boolean``. Each target persists append-only JSONL at
  ``$HOME/.concinno/ziq_tuners/<target>.jsonl``; partial-write
  corruption is skipped on load.
- Opt-in via ``CONCINNO_ZIQ_AUTOTUNE=1`` env var. Default off
  preserves backward compatibility — callers that never set the env
  keep receiving the preset even after accumulated observations.
- Also exports ``AutoTuneObservation``, ``AutoTuneRegime``,
  ``is_autotune_enabled`` for introspection.

### Added — ZIQ auto-tune registry (14 declared tunable targets)

- ``concinno.TUNABLE_REGISTRY`` — 14 declared tunable hyperparameters
  spanning the cognitive layer (escalation retries, spawn depth cap,
  consecutive-fail threshold, wiredo timeout, etc). Each target
  declares ``kind`` / ``vmin`` / ``vmax`` / ``preset`` / ``choices``
  schema contract.
- ``get_tuner(target_id)`` factory returns a per-target
  ``ZIQAutoTuner`` matching the registered schema. Target IDs use
  dotted paths mirroring the source module path so ``grep`` traces
  from a target id to its baseline call site.
- Also exports ``TunableSpec``, ``list_targets`` for discovery.

### Added — GAIA SAS/MAS/hybrid meta-router

- ``concinno.select_arm`` — per-task SAS (single-agent ReAct) vs MAS
  (1-layer parallel subagent) vs hybrid (MAS + external judge)
  selector. Breaks the Tran & Kiela 2026 "SAS ≥ MAS at token-matched
  budget" attack by picking the right arm per task instead of
  committing to one family across the whole run.
- Routing signal = SPS (structural prior from question features:
  level / tools / file / long-horizon) × FTRL (outcome online-learning
  per arm). Token-budget floor 20% — force SAS when MAS spawn overhead
  would starve the remaining budget.
- ``record_arm_outcome(arm, outcome)`` updates per-arm FTRL weights;
  state persists to ``$HOME/.concinno/gaia_arm_ftrl.json``
  (override with ``$CONCINNO_GAIA_ARM_STORE`` for tests).
- Also exports ``ArmFTRL``, ``ARMS``, ``Arm``, ``sps_arm_scores``.
- Module lives at package root (not under
  ``skills/public/agent/gaia_ziq.py``) because arms are mutually
  exclusive while buff-stack FTRL is additive — separate state file
  keeps them decoupled. Generic meta-router consumable from any
  agent loop (Sancio's ``fork_context`` driver included).

### Added — sweep_guard (.git residual state detector)

- New ``concinno.sweep_guard`` module detects interrupted git
  operations (rebase / merge / cherry-pick / revert / bisect) left
  unfinished when a session ends. Wired into ``concinno.hooks.on_stop``
  pipeline alongside ``handoff_required`` / ``sedimentation_gate``
  / ``excuse_scanner`` — completes the "任務結束順手修" ironclad
  coverage: dialog (excuse + sedimentation) + handoff file
  (handoff_required) + git filesystem state (sweep_guard).
- WARN mode by default (``stderr`` one-line actionable recovery hint
  per residual). Upgradable to BLOCK via
  ``feature_config.sweep_guard.block = true`` for users who want hard
  coupling.
- Per-session circuit breaker: once warned for a residual, suppress
  re-warning for 5 min (avoids spam when the session intentionally
  pauses mid-rebase).
- Escape valves: ``CONCINNO_FORCE_STOP=1`` (dispatch-level bypass) /
  ``stop_hook_active=true`` (CC retry signal) / ``CONCINNO_SWEEP_SKIP=1``
  (single-guard skip) / ``feature_config sweep_guard.enabled=false``
  (permanent).
- Handles plain checkouts AND linked worktrees (``.git`` file with
  ``gitdir:`` pointer).

## [2.11.0] - 2026-04-21

Minor: PromptJudge decision schema extension — new ``route`` third
enum ("block" | "allow" | "route") lets judges emit an
information-preserving advisory instead of forcing a binary block /
allow choice. Scope deliberately minimal, ratified by an S5
red-blue CBUA with two Opus subagents + WebFetch of
`code.claude.com/docs/en/hooks`. Active cross-process dispatch is
deliberately deferred — `ship route-as-schema` now, `wire up
real handlers` later (2.12.0+ / Sancio L3).

### Added — VALID_DECISIONS contract constant

- ``concinno.prompt_hooks.VALID_DECISIONS`` frozenset exports the
  open enum ``{"block", "allow", "route"}``. Module docstring
  declares the contract: consumers MUST treat unknown ``decision``
  values as ``allow`` (fail-open), never assert a closed set.
- Added to ``__all__`` so ``from concinno.prompt_hooks import
  VALID_DECISIONS`` works without reaching into internals.

### Added — concinno.prompt_hooks_routes submodule

- New module `concinno.prompt_hooks_routes` shipping
  ``RouteContext``, ``RouteResult``, ``BUILTIN_ROUTES``,
  ``echo_advisory``, ``validate_route_payload``, ``dispatch``.
  stdlib-only (``re``, ``sys``, ``dataclasses``, ``typing``);
  zero new runtime dependencies.
- ``echo_advisory(ctx)`` is the only shipped handler in 2.11.0 —
  pure function, writes a terse ``[concinno:route] ...`` line to
  stderr, returns ``RouteResult``. Handles closed-stderr via
  ``RouteResult(handled=True, action="noop", ...)`` fallback.
- ``BUILTIN_ROUTES`` maps five names (``echo_advisory``,
  ``citation``, ``opus_reviewer``, ``expert_review``,
  ``deploy_recipe``) to ``echo_advisory``. Replacing any with
  exec-capable handlers requires a capability manifest design
  deferred to 2.12.0+.
- ``validate_route_payload()`` rejects: non-``route`` decision,
  missing / non-ASCII / non-identifier / unknown ``route_to``,
  non-mapping / depth > 4 / unsafe-str (shell meta / path
  traversal / control chars / length > 2 KiB) ``route_context``,
  non-string / unsafe ``reason``. Stdlib-only; second line of
  defense after JSON parsing.
- ``dispatch(decision)`` is the **manual-call** entry point —
  **not invoked automatically by any Concinno hook** (CC hook
  protocol has no output-chaining channel between phases, per
  WebFetch-verified docs 2026-04; red team FATAL-1). User code
  that wants to act on a ``route`` decision imports and calls
  ``dispatch()`` itself. On validation failure or crashing
  handler, returns ``RouteResult(handled=False, action="reject",
  message=...)`` — callers treat unhandled as equivalent to
  ``allow``.
- No ``register_route`` API. Red team FATAL-2 flagged arbitrary-
  exec surface; commander verdict defers user-registered handlers
  to 2.12.0+ with capability manifest.

### Changed — four judge prompt bodies

- ``HALLUCINATION_JUDGE`` body: adds ``{"decision": "route",
  "route_to": "citation", "route_context": {"claim", "suggested_source"}}``
  option when a claim is plausibly legitimate research but lacks
  explicit source — information-preserving alternative to block.
- ``EXCUSE_SCANNER_JUDGE`` body: adds ``route_to: "opus_reviewer"``
  when hedging is present but session intent (spike / POC /
  production) is ambiguous — Haiku cannot judge intent reliably,
  routes up instead of false-positive blocking.
- ``CODE_QUALITY_JUDGE`` body: adds ``route_to: "expert_review"``
  when a pattern resembles a cardinal sin but context requires
  deeper reading (intentional defensive fallback, imminent-reuse
  abstraction, etc).
- ``WIREDO_JUDGE`` (`templates/wiredo/core.md`): adds
  ``route_to: "deploy_recipe"`` + ``route_context`` with
  ``dimension_in_doubt`` + ``recipe_hint`` fields when a
  dimension status is uncertain (e.g. "is this smoke test
  sufficient?"). D-dimension ``block`` rule preserved —
  delivery verification remains non-negotiable, route is for
  genuinely uncertain cases only.
- Each body now explicitly prefers ``route`` over ``block`` when
  the call is ambiguous: ``block`` destroys information,
  ``route`` preserves it.

### Red-blue CBUA S5 verdict (planning reference)

- See `_AI_BRAIN/05_Planning/promptjudge-route-schema-design-2026-04-21.md`
  (section 9) for the full attack / defense / commander verdict.
- Red team findings accepted but downgraded: FATAL-1 (dispatcher
  no receive path) → schema-only ship, no auto-dispatcher; FATAL-2
  (register_route arbitrary exec) → not shipped in 2.11.0.
- Red team findings rejected: HIGH-2 YAGNI (blue team's three
  concrete use cases stand — HALLUCINATION→citation /
  EXCUSE→opus_reviewer / WIREDO→deploy_recipe preserve
  information that binary block/allow destroys); MEDIUM-1 I11
  ordering (route = judge-logical layer vs I11 = adapter-dispatch
  layer, decoupled and can ship independently).
- Red team findings accepted as-is: FATAL-2 register_route
  dropped; HIGH-1 contract hardened via ``VALID_DECISIONS`` +
  docstring; HIGH-3 hook protocol verified via WebFetch; MEDIUM-2
  env gate granularity deferred with register_route; LOW-1
  ``route_context`` schema is ``dict[str, Any]`` with stdlib
  validator.

### Tests

- 84 new tests in `tests/test_prompt_hooks_routes.py`:
  `TestValidDecisions` (3) `TestJudgeBodiesMentionRoute` (9)
  `TestDataclassShapes` (4) `TestEchoAdvisory` (5) `TestValidator`
  (18) `TestDispatch` (4) `TestScope2_11_0` (4). Adversarial
  coverage: shell meta / path traversal / control chars / unicode
  homoglyph / depth limit / ASCII-identifier enforcement /
  crashing-handler fail-open.
- Existing `tests/test_prompt_hooks.py` and `tests/test_wiredo_loader.py`
  updated to reflect new prompt body sizes (core grew from ~841t
  → ~1053t after route schema addition; budget test bumped
  2000→2200 to preserve "dims kept, recipes dropped" semantic).
- Full regression: 5550 passed / 1 skipped / 3 xfailed / 0 failed.

### Added — 10 SkillsMP-ready skill wrappers (F2 from交接)

10 new SKILL.md docs under `src/concinno/skills/public/`, each wrapping
one high-value guard for SkillsMP marketplace
(<https://skillsmp.com>) GitHub-scraped discovery. Picks per
`docs/skillsmp_submission_plan.md`:

- `destruction-guard` — R0-R4 risk gating
- `secret-scan` — basename + word-boundary regex secret detector
- `butterfly` — pre-existing bug detection + Stop block
- `consecutive-fail` — three-strikes RAG / hard-stop
- `hallucination` — unsourced URL/version/API claim warn
- `premise-gate` — Mode 1 spec read / Mode 2 platform ceiling verify
- `verify-before-write` — block Write to unread modules
- `wiredo` — 6-dim delivery checklist injection
- `bash-dry-run` — input-rewriter for dangerous patterns (ALLOW-only)
- `handoff-required` — session hygiene Stop gate

Each SKILL.md: frontmatter (name / description / triggers /
user-invocable / license / upstream) + body (what it does + install
+ See also link). `user-invocable: false` because guards are
hook-based auto-run, not user-triggered. Source code stays in
`src/concinno/guards/` and `git_assist.py`; SKILL.md is the
marketplace-discoverable wrapper. No new pyproject `force-include`
needed — `packages = ["src/concinno"]` auto-bundles.

### Fixed

- Nothing — this is a purely additive minor.

## [2.10.5] - 2026-04-21

Patch: red-team Opus review of 2.10.2 + 2.10.3 found 1 FATAL + 3 HIGH —
all addressed in this release. Stacks on top of 2.10.4's
`AgentDispatchGuard` work; no overlap.

### Added — `_status_records_z` + `_parse_status_z` (FATAL F1 fix)

`auto_commit` now parses `git status -z` (NUL-separated, unquoted)
instead of `--short` (quote-wrapped CJK / spaced paths). Pre-fix: a
CJK-named 50 MiB LoRA at `模型/big.safetensors` would slip past
`_is_large_unignored` and `_is_secret` because both received the literal
`"模型/..."` (with quotes) as `path`, `os.stat()` raised
`FileNotFoundError`, the filter silently passed it through. Real
ai-king CJK handoff paths reproduced this on 2.10.3.

- `_git_raw()`: variant of `_git()` that does NOT `.strip()` stdout.
  Required because `-z` output's leading byte may be a meaningful space
  and `.strip()` ate it.
- `_status_records_z(cwd, timeout)`: returns NUL-separated record list,
  filtering empty.
- `_parse_status_z(records)`: parses XY + path with stable column-3
  offset (no leading-space ambiguity), handles rename-record-pair
  (`R  new\0old`) by skipping the old half.
- `auto_commit` falls back to legacy `--short` parsing only if
  `_status_records_z` returns None (sandbox / mocked-subprocess test
  env). Real git always succeeds via the `-z` path.

### Fixed — `_snapshot_inner_repo` worktree detection (HIGH F2)

Worktree's `.git` is a FILE (`gitdir: <abs path>`) pointing at the
shared `.git/worktrees/<name>/` admin dir. Pre-fix `os.path.isdir()`
returned False, function silently returned None, outer squash refused
with no diagnostic. New behavior: explicit stderr breadcrumb + bail.
`reset --hard` inside a worktree would mutate sibling worktrees'
shared HEAD/refs — much worse than refusing.

### Fixed — `squash_auto_commits` finally guards outer mid-rebase (HIGH F3)

If outer aborted mid-rebase, the `finally` block previously called
`_restore_inner_repo` (`reset --hard <inner_HEAD>`) — safe for inner
ITSELF but corrupts outer's view because outer's index is still stuck
in rebase-stale state. Result: outer status shows phantom diffs, next
session triggers the GitLens Interactive Rebase popup (MEMORY #67
redux). Fix: detect outer's `.git/rebase-merge` / `rebase-apply`
sentinel before restore; if present, skip restore and surface stderr
breadcrumb. Inner HEAD + stash remain in inner `refs/stash` for manual
recovery.

### Fixed — `_detect_embedded_nested_repos` walk pruning (HIGH H1)

Pre-fix used `Path.rglob('.git')` which on CPython <3.12 walks the
ENTIRE tree and only filters by `max_depth` after the fact — every Stop
event stat'd `.venv/Lib/site-packages/...`, `node_modules/...`,
`__pycache__/...`, adding 5–30 s on a typical ai-king tree. Switched to
`os.walk()` with explicit depth cap + in-place `dirnames[:] = ...`
pruning of `{.git, .venv, venv, env, node_modules, __pycache__,
.mypy_cache, .pytest_cache, .ruff_cache, .hypothesis, .tox, dist,
build}`. Also prunes the discovered nested repo's own subtree.

### Tests (167 passed, +9 new)

- 6 new `TestParseStatusZ` (CJK / space / rename / untracked / modified /
  short-record).
- `test_snapshot_inner_repo_refuses_worktree` (F2).
- `test_squash_skips_restore_when_outer_in_rebase` (F3 — pre-plant
  rebase-merge sentinel, verify inner HEAD intact + breadcrumb).
- `test_detect_embedded_skips_pruned_subtrees` (H1 — `.venv/.git`,
  `node_modules/.git`, `__pycache__/.git`, `.pytest_cache/.git`
  ignored; real `projects/real_inner/.git` detected).

### Deferred to backlog (red-team MEDIUM)

- H2 rename `_is_large_unignored` → `_is_large_regular_file`. Cosmetic.
- H3 NTFS sparse / compressed `st_size` — `os.stat` reports logical
  which matches what git serializes; current behavior correct, may
  overflag legitimate compressed workfiles. Documented limitation.
- H4 `_large_file_threshold()` read twice — tiny race, harmless.
- H5 (driven back) — outer `auto_commit`'s `git add -A` capturing
  inner snapshot is the user-intended carve-out (MEMORY #67); not a
  defect. Driven back per commander framing-check #1.
- M1-M5 / G1-G3 — backlog (ergonomic / Goodhart-resistance / docs).

## [2.10.4] - 2026-04-21

Patch: `AgentDispatchGuard` now scans subagent `prompt` strings for
unbounded poll loops (`until grep`, `until [` keyword-only, `while !
grep`) that have no timeout guard (`date +%s` elapsed cap, `timeout`
wrapper, `$SECONDS`, `[ -f ... ]` file-exist exit, `kill -0` PID check).
When detected, the guard appends a warning to the existing token-zone
strategy context pointing the caller at the three safe-poll patterns
from `feedback_subagent_poll_marker_fragile.md`. Driven by a live
incident — 2026-04-21 subagent F burned $1.01 stuck in `until grep
-q DONE log; do sleep 15; done` when the background smoke test
crashed silently and never wrote the marker.

### Added

- `_has_unbounded_poll()` / `_extract_prompt()` helpers in
  `agent_dispatch_guard.py`. Regex-based detection of three poll
  signatures (`until\s+grep`, `until\s+\[`, `while\s+!\s*grep`)
  combined with negation against five safety-guard signatures
  (`date +%s`, `timeout=`, `$SECONDS`, `[ -f|d|e|s` file-exist tests,
  `kill -0` liveness).
- `CONCINNO_ALLOW_UNBOUNDED_POLL=1` environment escape for callers
  who intentionally want an unbounded poll (rare — the recommended
  alternative is to add a `date +%s` timeout ceiling).
- 18 unit tests in `tests/test_agent_dispatch_guard.py` covering
  each poll pattern, each safety guard, the env escape, and three
  check-level integration scenarios (clean prompt, poll prompt,
  poll+RED zone stacking, PostToolUse ignore, non-Agent tool noop).

### Fixed

- The warning never fires on PostToolUse or on non-Agent tools — it
  only engages the PreToolUse path on `tool_name == "Agent"`, so it
  cannot pollute Bash/Edit contexts where `until grep` is legitimate
  (e.g. a long-running tail wait inside the main agent's own shell).

### Not Ready

- Full suite has one pre-existing flaky test
  (`test_git_assist.py::TestAutoCommit::test_batch_stage_then_commit`)
  that fails in full-suite order but passes isolated + fails to
  reproduce in a direct Python script. Root cause is test-order
  pollution in the existing suite, not related to this 2.10.4 guard
  change (whose scope is `guards/agent_dispatch_guard.py` only, with
  zero edge into `git_assist.py`). Flagged for a future patch.

## [2.10.3] - 2026-04-21

Patch: `auto_commit` now unstages large unignored blobs (≥10 MiB by
default) before committing. MEMORY #77's 7.6 GB bloat was caused by
LoRA / safetensors / BEIR corpus files that the `.gitignore` did not
cover — the 2.10.2 squash fix reclaims historical bulk, but preventing
the stage in the first place is cheaper. This is the belt to
`.gitignore`'s suspenders.

### Added

- `_large_file_threshold()` reads `CONCINNO_LARGE_FILE_THRESHOLD`
  (bytes, default 10 MiB / 10 485 760). Invalid / non-positive values
  fall back to the default.
- `_is_large_unignored(path, cwd, threshold=None)` — size-based filter
  paired with the existing `_is_secret`. `follow_symlinks=False` so
  symlinks themselves are never flagged (the target may live outside
  the repo). Non-regular files and missing paths return `False` — this
  is a hygiene signal, not a security gate.
- 9 new tests under `TestLargeFileThreshold` + `TestIsLargeUnignored`
  in `test_git_assist.py` (unit-level, no subprocess / real repo).

### Changed

- `auto_commit` step order: after `git add -A` and the secret-file
  defensive unstage, scan `safe_files` for large unignored blobs and
  unstage them via `git reset HEAD -- <paths>`. stderr emits the first
  5 unstaged paths + threshold + escape-hatch hint
  (`CONCINNO_LARGE_FILE_THRESHOLD=<bytes>`). If every safe file was
  large, return `None` (nothing to commit) instead of an empty commit.

### Fixed

- Prevents the MEMORY #77 bulk pattern (tracked LoRA adapter /
  safetensors / multi-MB dataset snapshots) from entering outer .git
  history. Combined with the 2.10.2 squash fix, outer repos that
  embed inner repos (e.g. ai-king / projects/concinno) now have
  both historical (squash) and prospective (stage filter) defenses.

## [2.10.2] - 2026-04-20

Patch: `squash_auto_commits` now protects embedded inner repos instead of
refusing outright when one is detected. The 2.9.0 `_detect_embedded_nested_repos`
guard was strictly correct (outer rebase could overwrite inner working tree
with stale snapshots) but caused unbounded outer `.git` bloat — inner-tracking
outer repos (e.g. `ai-king` with `!projects/concinno/` carve-out) could never
squash and grew 7.6 GB before this fix landed (MEMORY #77). The new default
snapshots inner HEAD + stashes any inner WIP, lets outer squash proceed, then
restores inner via `reset --hard HEAD` + `stash pop` in a `finally` block so
inner is protected even on rebase failure. Set
`CONCINNO_PROTECT_NESTED_REPOS=0` to restore the 2.9.0 refuse behavior.

### Added

- `_snapshot_inner_repo()` / `_restore_inner_repo()` helpers in
  `cleanup.py`. Snapshot records inner HEAD + creates a marked stash
  (`concinno-outer-squash-protect`) when inner has uncommitted state.
  Refuses when inner is mid-rebase / mid-merge (rebase-merge, rebase-apply,
  MERGE_HEAD, CHERRY_PICK_HEAD sentinels present).
- `CONCINNO_PROTECT_NESTED_REPOS` environment variable (default on).
  Legacy opt-out to the 2.9.0 refuse-outright behavior.
- 4 new integration tests in `test_cleanup.py` using real `git` repo
  fixtures: `test_squash_protects_inner_when_outer_embeds`,
  `test_squash_protects_inner_with_dirty_wip`,
  `test_squash_legacy_refuse_mode`, `test_squash_refuses_when_inner_in_rebase`.
  Shared `_build_outer_with_inner` helper extracted for reuse.

### Changed

- `squash_auto_commits()` no longer early-returns with
  `"nested repo(s) ... refusing squash"` when embedded inners are
  detected. New flow: snapshot inner → pre-rebase `checkout HEAD -- <inner>`
  in outer (so rebase's cleanliness check passes) → run rebase →
  `finally`-block restore inner to its snapshotted state.
- Outer dirty-tree check uses `git status -z` (NUL-separated, unambiguous)
  instead of `--short` (leading space of " M path" was eaten by
  `_git()`'s `strip()`, causing `ln[3:]` to slice the wrong column).
- `_detect_embedded_nested_repos()` docstring rewrite: previously said the
  embedded configuration is "never safe to squash"; that was true under
  the 2.9.0 refuse strategy and no longer holds after direction-D fix.
  The function itself is unchanged — still returns the list of embedded
  relative paths, still gated by `CONCINNO_SKIP_NESTED_REPOS`.

### Fixed

- Outer `.git` unbounded growth when the outer repo tracks paths inside
  an inner repo's working tree. Observed: `ai-king/.git` was 7.6 GB
  (85% reducible via `git-filter-repo --strip-blobs-bigger-than 10M`)
  because the `CONCINNO_KEEP_COMMITS=3` auto-squash never ran on outer.
  With 2.10.2 the squash runs; see `feedback_git_bloat_root_cause_fix.md`
  for the full diagnosis.

## [2.10.1] - 2026-04-20

Patch: `windows-full` extras bundle now explicitly lists
`concinno[control-anything]` alongside `concinno[all]` and `concinno[windows]`,
and the comment above the bundle was stale (still referred to `api-anything`).
Functionally a no-op — `concinno[windows-full]` already resolved
`control-anything` transitively through `concinno[all]` in 2.10.0 — but the
explicit listing makes the dependency visible in `pyproject.toml` without
having to trace through the `[all]` bundle.

### Changed

- `[project.optional-dependencies] windows-full` adds explicit
  `"concinno[control-anything]"` entry (redundant with `concinno[all]` but
  documents intent).
- Comment on `windows-full` updated: `api-anything` → `control-anything`
  (stale text missed during the 2.10.0 rename).

## [2.10.0] - 2026-04-20

Combined release covering two parallel axes:

### Changed — PyPI maintainer account migration

Re-publish under new PyPI maintainer account after old account deletion. The
original `AI_King` account is being deleted; all four AI-King-owned projects
(`invoco`, `concinno`, `api-anything`, `cc-cortex`) are being removed and
re-registered under a new account. PyPI disallows filename reuse even for
deleted projects, so the `concinno-2.9.0.tar.gz` / `concinno-2.9.1.tar.gz`
filenames cannot be re-uploaded — `2.10.0` is the first release under the new
account. (The `2.9.1` draft prepared earlier in the same session was never
published; it is superseded by `2.10.0`.)

### Changed — `api-anything` extras renamed to `control-anything`

The sibling automation library has been renamed upstream:
`api-anything 0.1.0-0.3.2` → `invoco 0.1.0-0.3.1` → `control-anything 0.1.0+`.
Concinno's optional extras key is updated accordingly.

- **Breaking (extras only)**: `pip install 'concinno[api-anything]'` no longer
  resolves. Use `pip install 'concinno[control-anything]'` instead.
- The `all` bundle now references `concinno[control-anything]` (previously
  `concinno[api-anything]`).
- No change to Concinno's core API, guard pipeline, hooks, or any non-extras
  surface. Users who never depended on the `api-anything` extras are
  unaffected.

### Why minor bump (2.9 → 2.10) not major

Extras key rename is a surface change only for consumers who pinned
`concinno[api-anything]` explicitly. Core imports, guard APIs, hook contracts,
and LLM-as-Judge surfaces are unchanged. Per semver guidance for
"backward-incompatible but narrow-scope" extras changes, a minor bump with a
clearly documented migration path is acceptable.

### Migration

```bash
# Old
pip install 'concinno[api-anything]'
# or transitively via: pip install 'concinno[all]' (where all included api-anything)

# New
pip install 'concinno[control-anything]'
# or transitively via: pip install 'concinno[all]' (where all now includes control-anything)
```

## [2.9.0] - 2026-04-19

Minor release. Two parallel axes:

1. **Root-cause fix** for a destructive interaction between Concinno's
   ``_inline_squash_if_needed`` and outer repositories that intentionally
   track files inside another repo's working tree (e.g. an umbrella
   workspace with a ``.gitignore`` carve-out for ``projects/concinno/``).
   Without this fix, the outer repo's squash rebase replayed stale
   snapshots of inner source files and silently overwrote the inner
   working tree, blowing away sub-agent work-in-progress.
2. **Positioning and compliance text** clean-up driven by a five-Opus
   red/blue CBUA review (session 648cae48). Narrative, keyword, and
   citation framing are pulled back from claims Concinno cannot
   independently substantiate; an explicit observability-not-safety-
   circumvention disclaimer is added.

### Fixed

- **Outer-repo squash no longer overwrites embedded inner repos.**
  ``concinno.cleanup.squash_auto_commits`` now detects when the caller's
  repository tracks paths that lie beneath another repository's
  ``.git`` directory, and refuses to squash with a clear error message.
  A new helper ``_detect_embedded_nested_repos`` performs the detection
  (depth-limited ``rglob``, gitlinks treated as safe, bypass via
  ``CONCINNO_SKIP_NESTED_REPOS=0`` for operators who know their
  configuration is safe). Corresponding four-test suite added to
  ``tests/test_cleanup.py`` covering the positive trip, the bypass
  environment variable, clean-tree no-op, and submodule-gitlink safety.

### Changed

- **README tagline** pulled back from "The Cognitive Layer for Claude
  Code" to "A hook-based governance toolkit compatible with Anthropic's
  Claude Code CLI", with an explicit disclaimer that Concinno is not
  affiliated with or endorsed by Anthropic.
- **Self-assigned certification badges removed** (NIST AI RMF and
  hard-coded counts such as "Tests: 3430" / "Guards: 55+" / "Skills:
  66"). These were self-declared, not independently attested, and the
  shields implied a certification that does not exist.
- **PyPI keywords** narrowed to generic terms (``hooks``,
  ``guardrails``, ``python``, ``governance``, ``agent``,
  ``developer-tools``). Removed ``claude-code``, ``anthropic``,
  ``ai-safety``, ``llm-guard``, ``llm-security``, ``prompt-injection``,
  ``agent-governance``, ``agent-safety``, ``a2a``, ``multi-instance``,
  ``ai-assistant`` to avoid brand-adjacency confusion on PyPI search.
- **``concinno.prompt_hooks`` module docstring** now cites the public
  hooks documentation (``https://docs.anthropic.com/en/docs/claude-code/hooks``)
  for the prompt-type hook runtime, and clarifies Concinno does not
  call any LLM directly.
- **Enterprise Governance section in README** reframed as
  "Observability & Audit Logs" with explicit language that Concinno is
  not audited, certified, or endorsed by any standards body and makes
  no claim to confer compliance on downstream systems. A new
  Positioning section now spells out what Concinno is and is not
  (dev-time scaffolding for the CLI; not a cloud SaaS governance
  platform; not a safety circumvention tool).

### Added

- ``docs/trademark_clearance_2026-04.md`` records a preliminary
  knock-out search for five Latin-style names (Concinno, Sancio,
  Cerno, Redigo, Perpetuo) across US (USPTO), EU (EUIPO), and WIPO
  public sources, with explicit caveats about paid-search coverage.
  This document is not legal advice.

### Tests

- ``tests/test_cleanup.py`` +4 tests for the new embedded-nested-repo
  guard. Total ``test_cleanup.py`` goes 24 → 28.

### Deferred to 2.10.0

The following items from the session 648cae48 shipping roadmap were
intentionally kept out of 2.9.0 so the root-cause fix and the
disclaimer text land cleanly without an invasive cross-file rename
hiding the diff:

- Rename pass (Cerno → Iudico, Redigo → Compono) based on the
  trademark clearance findings.
- ``SECURITY.md`` + ``detect-secrets`` / ``gitleaks`` pre-commit (I6–I8).
- ``docs/ai_act_compliance.md`` full text + ``LICENSE`` AI Ethics
  Notice + Export Control Notice (I19–I20).
- ``pip-licenses`` snapshot to ``docs/licenses.md`` (I21).

## [2.8.1] - 2026-04-19

Patch release. Two items: (1) root-cause fix for the ``.git/index.lock``
race that caused sub-agents in 2.8.0 to reach for ``--no-verify``, and
(2) cleanup of 44 accumulated ``ruff`` findings across ``gaia_ziq.py``,
``test_rag.py``, ``test_llm_guard.py``, and ``test_windows_live.py``.

### Fixed

- **Stale ``.git/index.lock`` orphan recovery** in
  ``concinno.git_assist`` — a killed ``git commit`` (session crash,
  Ctrl-C during startup, sub-agent timeout, Windows reboot) leaves
  behind a zero-byte lock file that blocks every subsequent commit
  with ``fatal: Unable to create '.git/index.lock': File exists``.
  Sub-agents misread this as "pre-commit hook recreating the lock"
  and reached for ``--no-verify``, which bypasses nothing because
  no hook is involved — the root cause is the orphan itself.
  ``auto_commit()`` now calls ``_clear_stale_index_lock()`` before
  any write op: locks older than 60s (tunable via
  ``CONCINNO_LOCK_STALE_SEC``) are removed with an stderr breadcrumb;
  fresh locks mean a sibling is mid-commit, so the caller bails
  rather than racing. Worktree / submodule layouts (``.git`` as a
  ``gitdir:`` pointer file) are resolved correctly via the new
  ``_resolve_index_lock_path()`` helper. 13 regression tests cover
  the happy path, fresh-lock bail, env override, gitdir resolution,
  and the end-to-end ``auto_commit`` integration.
- **ruff zero-tolerance restored** — 44 accumulated findings cleaned
  to 0. Breakdown: ``gaia_ziq.py`` (39: E701/E702/E501/I001/E722),
  ``test_rag.py`` (3: E501 monkeypatch long lines), ``test_llm_guard.py``
  (1: E501 mock JSON payload), ``test_windows_live.py`` (1: E501
  assertion message). Behavioural changes are zero — only formatting
  / line-wrap / explicit exception types. ``except:`` bare handlers
  upgraded to ``except Exception:`` in ``gaia_ziq.py``.

### Tests

- **5457 passed, 1 skipped, 3 xfailed** (5447 → 5457, +10 net from the
  new lock-recovery suite in ``tests/test_git_assist.py``).

## [2.8.0] - 2026-04-18

Six P0 items from the 2026-04-18 night red-blue CBUA session
(session 648cae48, 4 Opus agents in parallel + user second-pass
correction). The commander's judgement reframed red B's
API-cost attack as framing error (CLI lives inside CC
subscription; Haiku high-frequency binding is a feature, not
a cost leak) and redirected the hardening to runaway-spawn
prevention plus Goodhart self-downgrade blocking. Architectural
dual-axis synthesis ships alongside — the former "5-layer"
framing v2 is reclassified as a single **enforcement axis**
(3 tiers) orthogonal to a new **timescale governance axis**
(L3 / L4 live there, not as enforcement layers). Full record
at ``_AI_BRAIN/05_Planning/cbua-v3-three-layer-final.md``
(quoted in this release's commit message).

### Added

- **``concinno.redteam_spawn_guard``** — event-scoped spawn count
  cap (default 5, env override
  ``CONCINNO_REDTEAM_MAX_SPAWNS_PER_EVENT``) plus a JSONL ledger
  at ``{cache_dir | _AI_BRAIN/00_System | ~/.cache/concinno}/
  redteam_ledger.jsonl`` for audit. Public helper
  ``before_spawn_redteam(event_id, estimated_spawns)`` raises
  ``SpawnLimitExceeded`` atomically when a batch would exceed
  the cap (no partial writes). ``REDTEAM_GRANDCHILD_DIRECTIVE``
  constant holds the system-prompt fragment forbidding red-team
  subagents from spawning further subagents (docs-level
  enforcement while CC L1 keeps subagent spawn unmonitored;
  Sancio runtime is the breakout path). 12 tests cover cap
  boundary, batch atomicity, multi-event isolation, env
  override (inc. junk / zero / non-positive fallback), single-
  event reset, full ledger reset, malformed JSON tolerance,
  ``cap`` kwarg trumping env, and record shape.
- **``C0Router.classify_with_hysteresis``** — ratchet against
  self-downgrade Goodhart (red team C's FATAL-2). Once a
  session is classified above Simple, later prompts that look
  Simple cannot drop the session back down; only upward
  escalation crosses the lock, and a new ``upgrade_once`` kwarg
  explicitly refuses to act as a downgrade backdoor. New
  ``C0Result.hysteresis_locked`` field records the ratchet
  state and is persisted through ``StateStore``. 6 tests cover
  fresh-session lock, Simple-prompt refusal, upgrade-to-chaotic
  acceptance, upgrade_once misuse rejection, same-rank refresh,
  and redteam_required carry-forward. The original
  ``classify()`` stays pure for callers that need the unlocked
  reading (ablation, retrospectives, first-prompt of a new
  session).

### Changed

- **``cbua_pipeline_guard`` — B1/C1/U1 text regex deleted.**
  ``_B1_MARKERS`` / ``_C1_MARKERS`` / ``_U1_MARKERS`` /
  ``_WIREDO_TABLE`` are removed. MEMORY #27 "術語堆疊" proved
  they were gameable theater (models stuff
  root/sweet/strategy / 我知道-我不知道-我假設 / 反例 into tool
  args without actually thinking). B1 now has exactly one
  signal source — the behavioral silent ack (reads≥3 OR
  bash≥8 with edits≥3). C1 and U1 reminders retired because
  they had no behavioral counterpart and would have fired
  permanently once the text regex was gone. A4 / A5 patterns
  remain — they are Agent-tool-scoped (``ctx.tool_name ==
  "Agent"``), a specific behavioral surface rather than a
  general content scan. The dichotomy hook and delivery-verb
  Bash scanner also stay (they were never part of the
  B1/C1/U1 scope). Tests in ``test_cbua_pipeline_guard.py`` now
  prove the **absence** of text-regex detection: stuffing
  markers no longer flips state flags, and severity stacking
  uses the surviving signals (B1 + dichotomy + A5 + WIREDO).
- **``.claude/hooks/schedule_config.json`` and
  ``scheduled_launcher.ps1`` (public copy) documentation
  rebranded.** The scheduled Sonnet self-reflection
  ($1.50/day, already live) is now explicitly labelled as the
  per-day node on the CBUA timescale governance axis (軸 B),
  not a fifth enforcement layer. Adds ``_architecture`` note
  to ``schedule_config.json`` and a block comment to
  ``scheduled_launcher.ps1``. No behavioural change — this is
  a label/doc correction so the per-day, per-week, and
  per-fine-tune-cycle nodes stop getting mistaken for
  enforcement-pyramid layers.
- **``.claude/rules/L1/cbua.md``** — appended a "雙軸架構
  (v3.1 synthesis)" section describing the enforcement-cost
  ladder (L1 hook / L2 Opus red-blue / L3 Sancio runtime)
  alongside the timescale axis (per-tool-call / per-turn-stop
  / per-event-decision / per-day L3 / per-week L4 / per-fine-
  tune-cycle). Also syncs ``.claude/rules/public/L1/cbua.md``
  (operator-scope copy; the existing governance-doc drift
  between root and public/ is out-of-scope for this release).
  ``redteam.md`` public/root sync brings the 「有一說一」 2026-
  04-18 hardening block into both copies.

### Hardened

- Spawn-cap ledger JSONL schema v1 is documented inline at
  ``SpawnRecord.to_jsonl()`` — additive fields are append-only
  compatible, field removal is breaking. ``RedteamSpawnLedger``
  tolerates malformed lines so a corrupted ledger cannot take
  down the guard.

### Tests

- **5433 → 5447 passing** (+12 new redteam_spawn_guard + 6
  new C0 hysteresis - 4 cbua regex-detection tests rewritten
  in place). Full regression ``python -m pytest -q`` green in
  173s on Python 3.11.9 / win32.

### Known carryover (not 2.8.0 scope)

- Pre-existing 44 manual-fix ruff issues (E501 / E701 / E702
  / I001 / E722) stay as-is. 15 auto-fixable issues were
  tidied en passant while running ``ruff check --fix``. These
  issues predate this release and live in ``gaia_ziq.py`` /
  ``weekly_evolve.py`` / ``hidden_worker.py`` / legacy test
  files. Cleanup queued for 2.8.1 or later when there is a
  dedicated lint pass.
- CC platform ceilings that block further tightening stay
  logged: L1 (Agent spawn unmonitored → red team can only be
  detected, not enforced), L4 (hook cannot read conversation
  history → content scan limited to ``tool_input`` /
  ``tool_result``), L6 (PostToolUse cannot DENY → the guard
  stays advisory). The Sancio runtime is the agreed breakout
  path; Concinno 2.8.0 uses every hook the current CC platform
  exposes.

## [2.7.2] - 2026-04-18

Gap-cleanup follow-up to 2.7.1. A post-ship audit (five Opus agents)
found nine remaining gaps; one was already covered by 2.7.1 F8
(``ux_injection`` D6) and three are blocked on wiring decisions that
need product input. The five actionable gaps all land here as a single
release so the 2.6-series "claimed-but-not-shipped" items stop
accumulating.

### Fixed

- **Gap 1 — ``general-mode`` Skill now ships inside the wheel.**
  CHANGELOG 2.6.0 claimed ``competition-mode → general-mode`` had
  been renamed, but the new Skill only existed in AI King's
  user-global ``~/.claude/skills/`` directory — the PyPI wheel
  bundled only ``agent`` / ``browser`` / ``windows`` under
  ``src/concinno/skills/public/``. Anonymous downloaders ran
  ``/general-mode`` and got "unknown skill". The SKILL.md is now
  present at ``src/concinno/skills/public/general-mode/SKILL.md``,
  picked up automatically by ``[tool.hatch.build.targets.wheel]
  packages = ["src/concinno"]`` (no force-include — force-include
  would trigger PyPI's duplicate-filename warning). Verified via
  ``python -m zipfile -l dist/concinno-2.7.2-py3-none-any.whl``.

- **Gap 2 — ``competition-mode`` deprecation redirect now ships
  inside the wheel.** Same root cause as Gap 1 — the SKILL.md
  existed only in AI King's user-global layout. The redirect is now
  at ``src/concinno/skills/public/competition-mode/SKILL.md`` and
  points every invocation at ``/general-mode`` + ``/agent``. The
  redirect itself is scheduled for removal on **2026-07-18**
  (three months from 2026-04-18 per MEMORY #45-era naming SOP).

- **Gap 4 — ``auto_compact`` and ``memory_file_enabled`` now have
  real consumers.** Both keys shipped in ``_DEFAULT_CONFIG`` since
  2.6.0 but had no runtime consumer — grep across ``src/concinno/``
  only found them in ``config.py`` and CLI help. Users flipping
  either key saw the JSON update and no behaviour change. 2.7.2
  wires both:

  * ``auto_compact`` is now read in
    :meth:`AutoCompactor.should_trigger`
    (``concinno.cache.autocompact``). When False, ``should_trigger``
    returns ``"noop"`` before the token-threshold check, which
    cascades into :meth:`AutoCompactor.run` returning ``None``
    without touching the sink. Fail-soft: a ``concinno.config.get``
    crash falls back to the previous "enabled" behaviour so a
    broken config cannot silently break compaction.
  * ``memory_file_enabled`` is now read in
    :meth:`SessionMemory.should_update`
    (``concinno.cache.session_memory``). When False,
    ``should_update`` returns ``False`` unconditionally and
    :meth:`SessionMemory.update` short-circuits before the distill
    sink is invoked. Same fail-soft contract.

  New regression tests in
  ``tests/test_feature_enabled_wiring_part2.py`` pin both the
  disabled-kills-behaviour path and the fail-soft fallback path.

- **Gap 5 — test suite isolated from AI King's user config.**
  Three pre-existing tests had been failing on AI King's machine
  because ``~/.concinno/config.json`` (``locale: zh-TW`` +
  ``mode: handoff``) was leaking into the pytest run through
  :func:`concinno.config.load`: ``test_file_tracker::
  TestSessionFormat::test_invalid_session_id`` (i18n string came
  back in Traditional Chinese), ``test_git_assist::TestGt::
  test_english_default`` (same), and ``test_handoff_engine::
  TestHandoffMode::test_get_mode_default`` (got ``save-token``
  instead of ``phase``). ``tests/conftest.py`` gains an
  autouse fixture ``_pin_ship_default_config`` that sets
  ``CONCINNO_LOCALE=en`` + ``CONCINNO_MODE=general`` (the env
  layer wins over every JSON layer) and resets ``concinno.i18n``'s
  module-level caches so previous tests' locale cannot bleed in.
  ``test_config.py`` / ``test_config_loader.py`` own the config
  default invariants and manage these vars themselves, so the
  fixture skips them to avoid fixture-ordering fights.

### Meta

- **2.6.0 skill rename claim was ship-incomplete.** The original
  CHANGELOG entry claimed ``competition-mode → general-mode`` was
  live, but the rename only landed in AI King's user-global layout
  — the wheel never carried either Skill. 2.7.2 Gap 1 + Gap 2 fix
  the shipped artifact, and this meta entry documents the lag
  transparently so downstream users can see exactly which release
  they need.

- **Post-audit gap list**: nine gaps total from the five-agent
  audit. One was already covered by 2.7.1 F8 (``ux_injection``).
  Five are addressed in this release (Gaps 1, 2, 4, 5, and the
  Gap 3 "hook-level direct call" verification below). The other
  three need product decisions and stay on the backlog with
  explicit notes in the handoff.

- **Gap 3 was already wired.** The audit flagged ``streak_ux`` /
  ``session_summary`` / ``delivery_gate`` as missing their
  ``cfg.feature(..., "enabled")`` gate at the hook entry point,
  but a fresh grep confirmed all three land in 2.7.0's codebase
  (``on_post_tool.py:719``, ``on_stop.py:584``,
  ``on_stop.py:192``). The audit was working off a stale snapshot.
  The existing ``tests/test_feature_enabled_wiring.py::
  test_streak_ux_respects_feature_flag`` + ``_session_summary_*``
  + ``_auto_delivery_*`` tests continue to pin the invariant; no
  new code was needed. This entry records the verification so the
  audit's flagged item is closed explicitly.

## [2.7.1] - 2026-04-18

Hotfix release immediately following 2.7.0. Five Opus audit agents +
the user's own spec review caught seven real bugs in the 2.7.0 island-
closing work plus one un-landed spec item (``ux_injection`` config key
from MEMORY #61 D6). All eight land together with pinning regression
tests so the classes of bug never reappear.

### Added

- **F8 — ``ux_injection`` config key (MEMORY #61 D6).** New
  ``ux_injection`` entry in ``concinno.config._DEFAULT_CONFIG``,
  locked to ``False`` at ship time. When disabled (ship default)
  every LLM-facing UX inject site — CBUA pipeline markers (B1/C1/
  U1/A4/A5), WIREDO checklists, Read:Edit warnings, token-zone
  hints, three-layer thinking injects, cognitive anchors, intent
  anchors, cross-session pool context, and the ``cognitive_inject``
  thinking-directives router — silently returns the empty string /
  ``None`` and never reaches the subagent. Safety layer (destruction,
  butterfly, boundary, exfil, secret-scan, ``WiredoEnforcementGuard``
  deny, ``handoff_engine`` token gate Agent deny) is **not** gated
  and continues to fire. AI King's own machine sets the flag via
  ``concinno config set ux_injection true`` in the user layer; the
  ``CONCINNO_UX_INJECTION=1`` env var is a one-shot operator override
  that wins over every layer. New ``concinno.cache.ux_gate`` helper
  module exposes ``is_ux_enabled`` / ``reset_cache`` / ``CONCINNO_UX_ENV``
  as the single blessed gating call site; inject modules guard with a
  three-line ``try / if not is_ux_enabled() / except Exception`` so a
  broken config degrades to "no UX" rather than poisoning the pipeline.
  New regression tests in ``tests/test_ux_gate.py``.

### Fixed

- **F1 — ``installer.py`` rmtree could follow symlinks into user
  repos.** ``shutil.rmtree(dest_dir)`` on Linux/macOS follows
  symlinks, and on Windows silently follows NTFS directory junctions
  (``os.path.islink`` returns False for junctions per bpo-37834).
  A user who had symlinked ``~/.claude/skills/public/<skill>`` to
  their personal Skill development workspace would see
  ``concinno-install-skills`` wipe the source tree. The fix splits
  the destination probe into ``islink → os.unlink`` (link target
  untouched) and ``isdir → rmtree`` (real directory still replaced),
  plus a new ``_is_windows_junction`` helper that reads the
  reparse-point attribute off ``os.lstat`` on win32. New regression
  tests in ``tests/test_installer_symlink_safety.py``.
- **F2 — ``CognitivePool.save`` silently last-write-wins under
  concurrent writers.** The 2.7.0 save path was a simple
  ``tmp → replace`` sequence with no external locking. Two Claude
  Code sessions (or a session + a subagent) upserting different
  titles could both load an empty pool, both save their single
  section, and the later replace would drop the earlier writer's
  memory. ``upsert_section`` / ``remove_section`` / ``clear`` /
  ``prune_stale`` now wrap the read-modify-write cycle in a
  cross-platform advisory lock (``msvcrt.locking`` on win32,
  ``fcntl.flock`` on POSIX) held against a sibling ``.lock`` file
  so readers never contend, and reload the on-disk state inside
  the lock before persisting. New regression tests in
  ``tests/test_cognitive_pool_concurrent.py``.
- **F3 — ``cognitive_pool_inject`` violated Self-RAG with zero-score
  fallback.** When the task prompt tokenised into words that matched
  no pool section title or body, the module fell through to
  ``ranked = [s for _, s in scored]`` — recency-sorted whole pool —
  and injected unrelated cross-session chatter into the subagent's
  primacy slot. Self-RAG says: gate the retrieval when you have no
  signal. The zero-positive-score branch now returns ``""`` outright.
  Explicit empty queries (``task_prompt=""``) still fall back to
  recency so callers that deliberately ask for "whatever's fresh"
  keep working. New regression tests in
  ``tests/test_cognitive_pool_inject_gating.py``.
- **F4 — ``insert_cache_breakpoints`` prioritised system over tools.**
  2.7.0 used ``[system_idx, tools_idx, history_idx, fallback_idx]``.
  Anthropic's prompt caching best practices recommend caching the
  most stable prefix first so a changing system prompt still hits
  the tools cache. The order is now ``[tools_idx, system_idx,
  history_idx, fallback_idx]``. With ``max_breakpoints=2`` markers
  land on tools + system (previously system + tools); with cap 3+
  the history marker position is unchanged. New regression tests
  in ``tests/test_fork_context_cache_order.py``.
- **F5 — ``installer.py`` swallowed junction-creation failures
  silently.** The ``try/except (OSError, CalledProcessError): pass``
  around ``_ensure_junction`` hid failures behind "skill installed
  but not discoverable" mysteries. The except block now writes a
  formatted warning line to ``sys.stderr`` so operators see the
  failure immediately.
- **F6 — ``handoff_engine`` silently dropped legacy mode values.**
  Pre-2.5 ``cc_config.json`` files carrying ``handoff_mode:
  autonomous`` or ``handoff_mode: save_token`` (underscore) were
  not in ``HANDOFF_MODES`` and fell through to the phase ship
  default, silently changing behaviour for long-standing installs.
  A new ``_LEGACY_MODE_ALIASES`` map + ``_normalize_legacy_mode``
  helper normalises ``autonomous → full`` and ``save_token →
  save-token`` before validation; canonical values pass through
  unchanged and unknown values still fall to the default. New
  regression tests in ``tests/test_handoff_engine_legacy_alias.py``.
- **F7 — ``ask_user_toast`` hook could exceed its 3 s timeout on
  cold WinRT init.** The synchronous ``show_toast`` call chain
  blocks 2-5 seconds on the first toast in a fresh session while
  Windows initialises COM/WinRT. The hook's settings.json timeout
  is 3 seconds and would fire mid-toast, killing the subprocess
  and the AskUserQuestion would silently never notify the operator.
  The toast emission is now dispatched onto a daemon
  ``threading.Thread`` so the hook's main body returns its ALLOW
  decision in milliseconds while the WinRT pipeline finishes in
  the background. The hook's 3 s timeout is no longer load-bearing
  for toast delivery; existing ``test_ask_user_toast.py`` coverage
  continues to pass without modification.

## [2.7.0] - 2026-04-18

Island-closing release. Agent Y's health audit caught three large
modules that had been written but never wired to a consumer — the
kind of silent debt that makes the "tests green" count lie. The user
also filed two complaints in one session about silent AskUserQuestion
dialogs. All four fixes land together with pinning regression tests
so regressions show up loud.

### Added

- **F4 — AskUserQuestion toast notifier.** New
  ``concinno.hooks.ask_user_toast`` module emits a Windows toast the
  moment Claude opens an ``AskUserQuestion`` prompt so the operator
  stops burning 10+ minutes of wall time on silently-waiting dialogs.
  Registered as a dedicated ``PreToolUse`` hook with the
  ``AskUserQuestion`` matcher so it doesn't toast on every tool call;
  body preview is the first 60 chars of the question.
  ``maybe_show_ask_user_toast`` catches every exception and still
  ALLOWs — the hook never blocks a question from reaching the user.
  13 new regression tests in ``tests/test_ask_user_toast.py``
  covering matcher gating, preview truncation, fail-open on notify
  crash / import failure, and the hook-protocol contract. Entry
  point registered as ``concinno-ask-user-toast`` in
  ``pyproject.toml`` and wired into ``settings.json`` via the CLI
  installer's 3-tuple ``(filename, event, matcher)`` schema.
- **Reusable Anthropic cache helper module
  (``concinno.cache.anthropic_helpers``).** Exports
  ``with_cache_control``, ``system_with_cache``, and
  ``cache_breakpoint`` — a single blessed code path for applying
  Anthropic prompt-cache breakpoints. Supports all five modes the
  Sancio provider implements (``legacy`` / ``disabled`` / ``explicit``
  / ``multiturn`` / ``length-guard``) with full index translation
  (negative-from-end), content-shape handling (string → list-of-blocks
  promotion, list → last-block mutation), and TTL validation
  (``None`` / ``"5m"`` / ``"1h"``). Input messages are never mutated
  so caller retry payloads stay clean. 27 new regression tests in
  ``tests/test_anthropic_cache_helper.py`` including an identity-
  across-calls test that pins cache position stability (the whole
  reason a cache works).

### Fixed

- **F1 island — cognitive pool was written but never read.** ``1.16``
  introduced ``concinno.cache.cognitive_pool`` as a cross-session /
  cross-agent shared markdown store, and ``microcompact`` +
  ``l2_distill`` wrote sections to it from day one. But
  ``cognitive_inject.build_cognitive_context`` imported
  ``concinno.cognitive_pool_inject`` inside a ``try/except Exception``
  — and that module did not exist. The import silently failed on
  every SubagentStart, so every red/blue CBUA run started from zero
  shared cognition. Root cause: writer shipped before reader, import
  guard made the gap invisible. Fix:
  ``concinno.cognitive_pool_inject.build_pool_context`` now ships —
  token-bounded (default 3 sections / ~900 tokens), relevance-ranked
  against the incoming task prompt with a 3×-weighted title overlap
  heuristic, recency-ordered fallback when the prompt is empty,
  per-section body truncation at 1500 chars with a visible
  ``[...truncated]`` marker. Fail-open: a broken pool file returns
  ``""`` instead of breaking subagent spawn. Tests: 3 new integration
  tests in ``tests/test_cognitive_inject.py``
  (``TestCognitivePoolIntegration``) pin happy-path / empty-pool /
  crash-fail-open through the public ``build_cognitive_context``
  entry point, plus 19 focused tests in
  ``tests/test_cognitive_pool_inject.py`` for the adapter itself.
- **F2 island — ``/hook X off`` now actually stops the guard.**
  ``FEATURE_META`` shipped with 33 feature entries but only 5-6
  consulted ``cfg.feature(name, "enabled")`` at runtime. Users who
  ran ``concinno config set boundary_guard enabled false`` saw the
  JSON update and the guard keep running. Root cause: 2.x added
  per-feature ``enabled`` keys to cc_config but only wired them into
  the four flagship guards; the rest of the surface grew under a
  "metadata-only" disclaimer that quietly calcified into a bug. Fix:
  flag routed through two sinks — (a) **centralized pipeline
  dispatch** (``GuardPipeline._feature_enabled`` looks up
  ``guard.feature_name or guard.name`` in ``cc_config.json`` before
  every ``check`` / ``on_post_tool`` / ``on_stop`` call, so every
  ``BaseGuard`` subclass is wired in one commit); (b) **hook-level
  direct calls** (``clarity_gate`` / ``prompt_guard`` /
  ``insight_engine`` / ``streak_ux`` / ``session_summary`` /
  ``delivery_gate`` / ``bash_background_gate`` / ``python_c_gate``
  now read ``cfg.feature(..., "enabled")`` at their hook entry).
  Seven guard classes whose ``name`` diverges from their feature key
  (``ReadFirstGuard`` → ``read_first_gate``, ``LintGuard`` →
  ``linting``, ``HijackGuard`` → ``hijack_gate``,
  ``ConsecutiveFailGuard`` → ``consecutive_fail_gate``,
  ``SentinelGuard`` → ``sentinel_gate``, ``PromptInjectionGuard`` →
  ``prompt_guard``, ``AgentGateGuard`` → ``agent_cap``,
  ``HandoffGuard`` → ``handoff_format``) declare
  ``feature_name = "..."`` on the class. The stale "metadata-only"
  disclaimer at the top of ``feature_config.py`` is replaced with an
  accurate wiring table. Fail-open semantics preserved: a crashing
  ``cfg.feature`` call treats the guard as enabled so a broken
  config never silences a safety guard. Tests: 18 new regression
  tests in ``tests/test_feature_enabled_wiring.py`` covering the
  pipeline dispatch, ``feature_name`` overrides, pre/post/stop
  pass-through, fail-open, and each divergent class pin.
- **F3 island — Concinno-internal Anthropic calls now use prompt
  caching.** Sancio's ``providers/anthropic.py`` has had
  ``_cache_control()`` and full breakpoint logic for months, but
  Concinno's own internal Anthropic callers never plugged in. Root
  cause: logic lived in the consumer (``persona-api``), not in a
  reusable Concinno module, so every other caller copy-pasted
  nothing. Fix: five callsites now go through
  ``concinno.cache.anthropic_helpers`` — ``escalation.py``
  (Gemma→Haiku→Sonnet→Opus chain: legacy cache on first user turn +
  ``system_with_cache``), ``llm_guard.py`` (explicit breakpoint at
  index 0 so repeat safety checks hit warm cache), ``a2a/agent.py``
  (same pattern), ``skills/public/agent/gaia_agent.py``
  (``system_with_cache`` + legacy message cache for multi-step
  agent loops), and ``skills/public/agent/eval_runner.py`` (explicit
  cache on goal-parsing prompt reused across 100+ tasks). Expected
  cache hit rate ≥80% once a chain or loop reaches its second call
  within the 5-minute TTL window. Tests: 27 helper tests above
  exercise every strategy / mode / edge case; the five callsites
  import from a tested core.

### Internal

- ``GuardPipeline._is_disabled`` remains for backward compatibility
  with third-party callers; new code paths use
  ``_is_guard_active`` which combines health state + feature-enabled
  into a single "should-this-guard-run" predicate.
- ``BaseGuard`` gained a ``feature_name: str = ""`` class attribute
  so guards whose ``name`` doesn't match the ``FEATURE_META`` key
  can declare the mapping declaratively.
- ``HOOK_EVENTS`` in ``concinno.cli.main`` switched from 2-tuples to
  3-tuples ``(filename, event, matcher)`` so hooks with tool-specific
  matchers (like ``ask-user-toast.py`` targeting
  ``AskUserQuestion``) can register without hand-editing
  ``settings.json``. Old 2-tuples still parse for any forks that
  imported the list.

## [2.6.1] - 2026-04-18

Hotfix ship — five bugs caught by the 2.6.0 S5 red/blue CBUA review
after the release artifact was already built. Every fix pinned by
new regression tests so we can't silently undo the wiring later.

### Fixed

- **F1 — `concinno config set mode` now actually takes effect.** In
  2.6.0 the `handoff_engine.get_handoff_mode()` resolver only read the
  legacy `cc_config.json::handoff_mode` file and completely ignored
  the new `concinno.config` loader. `concinno config set mode handoff`
  would succeed, write the JSON, and change nothing at runtime. The
  resolver now reads legacy first (for back-compat with existing
  installs) then falls through to `concinno.config.get("mode")`,
  mapping `general` → `phase` and `handoff` → `save-token`. 11 new
  regression tests in `tests/test_handoff_engine_config_wire.py`.
- **F2 — `concinno.config._write_layer` is now atomic.** Previously
  used a bare `open(path, "w")` truncate+write which could corrupt
  the JSON file if two sessions hit `set_user` simultaneously, or if
  the process crashed mid-write. Now serializes to a per-writer
  unique `*.<pid>.<tid>.tmp` sibling and `os.replace`s onto the
  target. On Windows a short retry handles the `PermissionError`
  that transient reader-lock contention can cause; the file on disk
  is never torn. 3 new regression tests (valid-write, crash-mid-write
  preserves original, threaded concurrent writers leave readable JSON).
- **F3 — `_DEFAULT_CONFIG` is now immutable.** Wrapped in
  `types.MappingProxyType` so
  `concinno.config._DEFAULT_CONFIG["mode"] = "handoff"` raises
  `TypeError` instead of silently flipping the ship default for every
  other import in the process. `load()` and `default_config()` still
  return fresh mutable `dict`s (via `dict(_DEFAULT_CONFIG)`) so
  callers that were in the habit of mutating the result keep working.
  4 new regression tests including explicit mutation rejection.
- **F4 — `i18n._BUILTIN_LOCALES` derived from `config._VALID_LOCALES`.**
  Before 2.6.1 the two lists drifted: `_VALID_LOCALES` accepted `fr`
  and `de` but `i18n` only loaded five built-ins, so a user who ran
  `concinno config set locale fr` passed validation then saw English
  forever. The SSoT is now `config._VALID_LOCALES`, converted to
  underscore filename form (`zh-TW` → `zh_TW`) by the derivation
  helper. A declared locale with no translation file on disk falls
  back to English AND emits one stderr warning per process so the
  silent-UX-fail is now a visible-UX-fail. 8 new regression tests.
- **H1 — `field_read.expand()` path-traversal defense.** The hook
  pipeline can hand `expand()` section ids that cross-reference
  arbitrary paths; previously nothing stopped
  `expand("../../../etc/passwd", "x")` from reading up to 50 KB of
  any file the agent process could touch. Added an opt-in
  `workspace_root` kwarg; when set, the resolved source path must
  live under the workspace or `ValueError` is raised. Default stays
  `None` (no check) for back-compat with callers that legitimately
  pass absolute handoff paths outside any particular workspace;
  trusted-input frontends SHOULD pass `workspace_root=Path.cwd()`.
  Symlink escape is caught because both paths are `resolve()`d before
  comparison. 6 new regression tests.

### Internal

- `import copy` removed from `concinno.config` (unused after F3).
- `import threading`, `import time` added to `concinno.config`
  (needed for F2 per-writer tmp names + retry).
- `import sys` added to `concinno.i18n` (needed for F4 warning path).
- `from pathlib import Path` added to `concinno.field_read` (needed
  for H1 `is_relative_to` resolution).
- Total new regression tests: **32** (5299 → 5331 green + 1 skipped +
  3 xfailed, same as 2.6.0 baseline).

## [2.6.0] - 2026-04-18

Three-layer config system, FieldRead metadata-first redesign, and a
handful of plumbing fixes discovered during the 2.5.x security
postmortem. Ship default locked to `mode=general + locale=en` so
anyone `pip install concinno` gets the globally sensible defaults;
AI King's local preferences (zh-TW + handoff mode) now live in
`~/.concinno/config.json` and no longer leak into the source tree.

### Added

- **`concinno.config`** — layered config loader (env > project > user
  > package default). Priorities: `CONCINNO_<KEY>` env var
  > `<cwd>/.concinno/config.json` > `~/.concinno/config.json` > source
  `_DEFAULT_CONFIG = {"mode": "general", "locale": "en",
  "auto_compact": True, "memory_file_enabled": True}`. Validated
  against `_VALID_MODES = {general, handoff}` and `_VALID_LOCALES =
  {en, zh-TW, ja, ko, fr, de, es}`. `load()` never raises — malformed
  user config falls back to defaults with a stderr warning so a
  corrupt JSON file can't brick the library for a downstream agent.
  23 regression tests including two invariant locks on the ship
  defaults.
- **`concinno config` CLI subcommand** — `concinno config` (show
  merged + per-key source), `get <key>`, `set <key> <value>` (writes
  user layer), `set --project <key> <value>`, `unset <key>`, `path`.
  Replaces the ad-hoc "set an env var and hope" workflow.
- **Layer-aware `i18n._resolve_display_locale()`** — now consults
  `concinno.config.get("locale")` before falling back. Legacy
  `CC_UX_LANG` env var still honored as the top-priority override for
  back-compat.
- **`general-mode` Skill** (`.claude/skills/general-mode/`) — the
  PyPI ship default documented as its own Skill. Triggers on
  "general" / "一般" / "normal" / "預設" / "standard".
- **FieldRead v2 (metadata-first)** —
  `concinno.field_read.ElidedSection` + `FieldReadResult` + new
  `read_handoff_fields_v2` / `read_memory_fields_v2` /
  `build_field_context_v2` / `expand(source_path, section_id)`
  functions. Output now includes `sections_kept`, `sections_elided`
  (each with `id`, `heading`, `lines`, `gist`, `confidence`),
  overall `confidence`, and an `expand_hint` string so the LLM can
  see what was trimmed and call `expand()` when user references an
  elided topic. Closes the "cognitive desync" gap documented in
  MEMORY #15 — the reader no longer has to guess whether important
  content was silently dropped. Legacy `read_handoff_fields` /
  `read_memory_fields` / `build_field_context` still return `str`
  (they delegate to the v2 functions and extract `.content`), so
  every existing call site keeps working bytewise-unchanged.
  36 new tests covering the v2 API plus 4 dedicated backward-compat
  tests asserting `legacy_output == v2_result.content`.

### Changed

- **`competition-mode` Skill renamed to `general-mode`** — the old
  name framed Concinno as a benchmark/competition tool, which is
  wrong: the ship default targets the general LLM-usage workflow
  (context runs to the limit, auto-compacts, memory files, new
  conversations). `competition-mode/SKILL.md` is now a thin
  deprecation redirect that will be removed three months from today
  (2026-07-18). Migration: anywhere you see `competition-mode`,
  swap to `general-mode`.
- **Honest `mode` / `locale` semantics** —
  `_DEFAULT_CONFIG["mode"] = "general"` is intentionally pinned at
  the source level. AI King's personal preferences (zh-TW +
  `handoff` mode for the structured-handoff workflow) live in
  `~/.concinno/config.json`, never in package source. The invariant
  is now enforced by
  `tests/test_config_loader.py::TestShipDefaults` so a stray
  `mode=handoff` or `locale=zh-TW` in source fails CI.

### Fixed

- **`tests/test_scheduler.py::test_install_skills`** — pre-existing
  failure that predated this release (the installer now returns a
  directory path for `general-mode` style bundled Skills, and the
  assertion was still checking for `SKILL.md` as the final path
  component). Now asserts `isdir(path)` and `(path / "SKILL.md").exists()`.

### Migration notes

- **For PyPI consumers**: nothing to do. Default behaviour is
  unchanged (English output, general mode). New config module is
  opt-in.
- **For AI King / AI King's friends who already use Concinno**:
  create `~/.concinno/config.json` via
  `concinno config set mode handoff && concinno config set locale zh-TW`
  to preserve your current experience. The package no longer
  auto-detects Chinese or "handoff-style" users.
- **Skill references**: search your transcripts / notes for
  `competition-mode` and replace with `general-mode`. The old
  redirect Skill expires 2026-07-18.

### Notes

- Ship defaults rationale: MEMORY #59 (linked `general` to wide
  audience, `handoff` to AI King's personal structured workflow).
  Any future refactor that changes these defaults must preserve
  the ship-defaults invariants in `test_config_loader.py`.
- No breaking public-API changes. All existing imports and call
  sites continue to work. FieldRead v2 is additive; the v1 names
  are still canonical.

## [2.5.1] - 2026-04-18

### Security

- **Removed hard-coded Hugging Face token fallbacks** in
  `skills/public/agent/gaia_agent.py:43`,
  `skills/public/agent/gaia_runner.py:29`, and
  `skills/public/agent/gaia_ziq.py:133`. All three now read
  `HF_TOKEN` from the environment and raise `RuntimeError` with
  an actionable message when unset, instead of silently using a
  live fallback token. The token was pinned to a personal
  HuggingFace account; shipped wheels 2.5.0 and earlier released
  under the same source tree carry it. This 2.5.1 wheel does not.

## [2.5.0] - 2026-04-18

Four silent-failure bugs in the auto-commit / squash / gc pipeline
caused `.git` to bloat unbounded (observed 5.6 GB / 2443 commits
before today's fix). Red-team Opus audit uncovered a public-RCE
class of gaps in the companion `persona-api` service and a
Plan-style confirmation gap in `destruction_guard`. Everything
below is the integrated fix set.

### Fixed

- **`cleanup.squash_auto_commits`** now runs `git status` with
  `--ignore-submodules=all`. Previously any dirty nested repo
  (e.g. `.claude/skills/last30days`, `benchmarks/E2Rank`) made the
  top-level status permanently non-empty, so the inline squash
  aborted on every auto-commit — silently, because
  `git_assist._inline_squash_if_needed` swallowed the returned
  error. Net effect: the "keep 3 commits" rule was a no-op in
  practice and `.git` grew unbounded (observed 5.6 GB after 2440
  auto-commits).
- **`cleanup.squash_auto_commits`** now calls `git gc --auto`
  after a successful squash. Without it, squashed commits left
  orphan objects/packs that never shrank, so pack bloat
  accumulated even when the commit graph was compressed. `--auto`
  is git-throttled (no-op unless thresholds hit) so it stays safe
  inside an auto-commit hook path. Aggressive repack + explicit
  prune remain in `/tidy git` for user-triggered maintenance.
- **`git_assist._inline_squash_if_needed`** now surfaces
  `CleanupResult.error` and unexpected exceptions to stderr
  instead of silently `pass`-ing. The prior swallow made the
  above squash bug undiagnosable from runtime behaviour alone.
- **`cleanup.detect_large_git_objects`** inner blob-filter loop
  refactored from 6-level nesting to 3 via early-continue —
  no behaviour change, quieter structural lint.
- **`destruction_guard.restore_backup`** refactored from 6-level
  nesting to 3 via an extracted `_restore_single_target` helper
  plus a basename→target lookup dict. Same semantics, lint-clean.
- **`hooks.on_pre_tool`** bare `except Exception: _allow()` now
  emits a yellow warning to stderr before fail-open. Previously
  any guard-pipeline crash was invisible — fail-open hid months
  of regressions from view. Pipeline still fails open (user never
  gets blocked by a crashed gate), but the failure is observable.
- **`core.state_store.write` / `write_flat`** failures escalated
  from `logger.debug` (nobody reads that) to stderr + a
  destruction_guard audit entry. State corruption that used to
  surface only when a downstream consumer crashed now screams
  on the write itself.

### Added

- **`git_size_monitor`** — lightweight stop-hook module that
  warns when ``.git/objects/pack/*.pack`` exceeds a GB threshold
  (default 5 GB, override via `CONCINNO_GIT_SIZE_WARN_GB`).
  Fast path sums pack sizes only; single-digit ms on 10 GB
  repos. Wired into `hooks.on_stop` pipeline + whitelisted for
  stderr emission so the user hears about bloat before push/fetch
  latency screams. 17 tests including worktree `gitdir:` file
  support, multi-pack summation, env override, bad-env fallback.
- **`destruction_guard.confirm_with_options`** — builds an
  AskUserQuestion template (`{question, options, default}`)
  that `evaluate()` attaches to R3/R4 deny decisions under
  `additionalContext.ask_user_question_template`. CC's hook API
  cannot call AskUserQuestion directly (L6 locked) so the LLM
  pastes the template on the next turn. Enforces 2–4 options +
  required `label`/`description` keys.
- **`destruction_guard.suggest_safer_alternative`** — lookup
  table (11 patterns) mapping destructive commands to safer
  alternatives: `rm -f *.lock` → `pathlib.Path.unlink(missing_ok=True)`,
  `rm -rf dir` → soft `mv` to `_trash/`, `git reset --hard` →
  `git stash push -u`, `git push --force` → `--force-with-lease`,
  `git gc --prune=now` → `git gc --auto`, `twine upload` →
  `--skip-existing`, `DROP TABLE` → `RENAME TO _deprecated_YYYYMMDD`,
  `kubectl delete ns` → `get all > backup.yaml` first,
  `docker system prune -a` → without `-a`, `aws s3 rb --force` →
  enable versioning first, `git filter-repo` → mirror clone +
  bfg. Results bundled into the deny payload so the LLM has
  concrete proposals ready.
- **`destruction_guard.destruction_gate` decorator** +
  **`DestructionBlockedError`** — applied to
  `cleanup.squash_auto_commits` (R3),
  `cleanup.git_gc` (R3), `cleanup.cleanup_stale_files` (R2),
  `cleanup.rotate_log_files` (R2), `backup_manager.BackupManager.prune`
  (R2), and `git_assist.rollback` (R3). Direct call paths
  require a `reason=<keyword>` kwarg drawn from
  `VALID_REASON_KEYWORDS` (migrate / decommission / archive /
  redact / retire / ...). In-process hook/orchestrator paths
  raise per-op escape env flags
  (`CONCINNO_INLINE_SQUASH`, `CONCINNO_GIT_GC`,
  `CONCINNO_BACKUP_PRUNE`, `CONCINNO_GIT_ROLLBACK`,
  `CONCINNO_STALE_CLEANUP`, `CONCINNO_LOG_ROTATE`) so trusted
  callers (`run_cleanup`, `_inline_squash_if_needed`,
  `BackupManager.create → .prune`) pass through. Tests exercise
  direct-call block, valid-reason pass, bogus-reason block,
  hook-context pass, unknown-op requires reason, and
  decorator metadata preservation.
- **`destruction_guard.audit` rotation** — 10 MB / 90-day
  threshold (stat-cheap common path); rotated archives gzipped
  to `destruction_audit.log.1.gz` ... `.3.gz`, older archives
  dropped on shift. Best-effort, never raises.
- **`tests/conftest.py`** — autouse fixture raising all six
  destruction_gate escape env flags so the broad test suite
  runs destructive cleanup / backup / rollback paths without
  plastering `reason=` kwargs through every test. Gate-specific
  tests in `TestDestructionGate` pop the flags via
  `monkeypatch.delenv` to verify the gate actually fires.

#### 2026-04-18 other additions



- **`concinno.tools.builtin.python_exec.PythonExecTool`** — sandboxed
  Python expression evaluator. AST whitelist (no Attribute / Lambda /
  Assign / Import) + builtin whitelist (no open / eval / exec /
  getattr / __import__ / type) + size caps (8 KB source, 256 nodes).
  Pure expression only; runtime errors surface as ``"error: ..."``
  strings so agent loops observe rather than raise. 22 tests
  covering happy path (arithmetic / comprehensions / sorted+zip),
  AST reject paths (attribute escape, lambda, walrus, import),
  builtin gating (open / eval / getattr / string-method-call),
  statement rejection, size caps, and runtime error surfacing.
- **`concinno.tools.builtin.date_calc.DateCalcTool`** — calendar
  arithmetic without a generic Python sandbox. Three ops:
  ``delta`` (total days + calendar-accurate years/months/days
  breakdown), ``parse`` (strict strptime → ISO 8601), ``format``
  (re-format ISO or format-matched input). stdlib-only, strict —
  no natural-language date parsing. 14 tests.

### Notes

- Both tools target Sancio's GAIA / HAL benchmark runners
  (MEMORY #52 "Benchmark 天花板升級屬 Concinno"). `python_exec`
  replaces the "let the LLM do math in its head" footgun; `date_calc`
  replaces the bulk of year / day / birthday questions that show
  up in GAIA Level 1 & 2.
- Version bump and PyPI publish deferred to user authorization
  (MEMORY #48 Release Coordination / #50 付費不可逆必授權).

## [2.4.1] - 2026-04-17

### Changed

- **Skills now ship under a `public/` subtree** (`src/concinno/skills/public/`)
  with a sibling `private/` reserved for user-local Skills. The rule:
  anything under `public/` is PyPI-shipped universal capability, anything
  under `private/` is personal and never bundled. Removes the per-Skill
  "should this be pip'd?" decision — the folder IS the answer.
- **`concinno-skills` installer creates the same layout on the consumer
  machine** at `~/.claude/skills/{public,private}/` plus directory
  junctions (`mklink /J` on Windows, `os.symlink(target_is_directory=True)`
  elsewhere) from the flat skills root to each `public/<name>/` so Claude
  Code's flat-scan auto-discovery keeps working. Re-running the installer
  is idempotent (stale junctions get replaced, real directories refuse
  to be clobbered).
- **Three legacy file-Skills** (`cortex-guard` / `cortex-schedule` /
  `cortex-hooks`) also move under `public/` with matching junctions.

### Added

- **`installer._ensure_junction(link, target)`** helper — cross-platform
  directory-junction primitive, no admin required on Windows.

### Notes

- User's local `~/.claude/skills/` was reorganized manually in the same
  session; junctions at the skills root preserve existing Claude Code
  auto-discovery with zero behavioral regression (verified via session
  start skill-list before and after the move).

## [2.4.0] - 2026-04-17

### Added

- **Three universal Skills bundled into the wheel**: `windows`, `browser`,
  `agent`. These are full Claude Code Skills (SKILL.md + Python helpers
  + tests + workflows), not just `.md` stubs — running
  `python -m concinno.skills.installer` now copies the complete directory
  tree into the user's `~/.claude/skills/` so consumers get the same
  agent tool stack as the maintainer's local setup:
  - **`windows`**: in-process Python Windows automation (UIA +
    PrintWindow + hidden-desktop workers). Replaces `windows-mcp` with
    zero MCP overhead and true-headless background operations.
  - **`browser`**: Playwright Python daemon with session-persistent
    pages. Replaces `playwright-cli`; DOM snapshot / click / fill /
    screenshot / eval all in-process.
  - **`agent`**: unified GUI/Web agent loop
    (OBSERVE / PLAN / ACT / VERIFY / RETRY) that dispatches to the
    `windows` and `browser` Skills.
- **`installer.SKILL_DIRS` tracks directory-bundled Skills** alongside
  the legacy single-file `SKILL_FILES` list. `install_skills()` now
  handles both; re-install cleanly replaces any stale destination
  directory before copying.

### Notes

- The Skills are installed into `~/.claude/skills/<name>/`, NOT
  `~/.claude/skills/cortex-<name>/` — they use their own canonical
  names because external tooling (e.g. MEMORY notes, documentation)
  already refers to them by short name.
- Consumers who only need the guard pipeline can skip the installer
  entirely; the Skills are inert data inside the wheel until
  `concinno-skills` runs.

## [2.3.1] - 2026-04-17

### Added

- **`[api-anything]` optional extra** — `pip install "concinno[api-anything]"`
  now also installs the sibling `api-anything[all]>=0.2.1` package so the
  full AI-King Python toolchain lands in a single command. Pulled into
  `[all]` (and therefore `[windows-full]`) so `pip install
  "concinno[windows-full]"` is now a genuine one-shot for the complete
  AI King stack on Windows. Anyone who only needs Concinno's guard
  pipeline can skip the extra.

## [2.3.0] - 2026-04-17

### Fixed — red-team round 3 patch set (9 FATAL + HIGH items)

This release is the post-mortem of the 2.2.0 candidate: 3 Opus red-team
agents and 1 Opus blue-team defender reviewed the 2.2.0 artifacts; 9
FATAL items were accepted by the commander, patched, and re-verified.
2.2.0 was built but never published — 2.3.0 supersedes it.

- **Project URLs repointed away from `anthropics-community` namespace**
  (Red 3 A1 — trademark / PEP 541 takedown risk). `pyproject.toml`,
  `src/concinno/__init__.py` docstring, and `src/concinno/a2a/server.py`
  Agent Card now reference `github.com/aiking931931/concinno` (the
  publishing account's own namespace). The previous URLs did not
  exist as a GitHub organization and implied Anthropic endorsement
  via name-prefix — unsafe ground for a library with `anthropic` as
  a hard dependency.
- **CI coverage gate points at the correct package** (Red 3 A3).
  `.github/workflows/ci.yml` previously ran
  `pytest --cov=tempero --cov-fail-under=80`; the `tempero → concinno`
  rename left the `--cov` flag pointing at a non-existent package, so
  coverage reported 0% or no-op-passed, rendering every red-team fix
  CI-unverified since the rename. Now `--cov=concinno`.
- **Publish workflow hardened** (Red 3 A4, Red 2 H-R2-2):
  - GitHub Actions pinned to specific tagged releases (Dependabot
    will convert to full 40-char SHAs once this workflow lives on
    main); `pypa/gh-action-pypi-publish@release/v1` replaced with
    `@v1.12.4` + `attestations: true`, emitting PEP 740 Sigstore
    attestations on PyPI for consumer verification.
  - `actions/attest-build-provenance@v2.2.0` added so consumers can
    verify the wheel was built from this commit on a GitHub runner.
  - Build step sets `PYTHONUTF8=1` so the `Summary` metadata field
    does not round-trip through a non-UTF8 locale and ship with
    U+FFFD replacement characters on PyPI (Red 3 A8).
  - Smoke test switched from `lstrip('v')` to `removeprefix('v')` —
    the former would eat every leading `v` (corrupting `vv2.2.0`
    or `version-2.2.0` and silently passing the wrong tag).
  - Test job now runs the guard + wiredo + token_counter + autocompact
    suites (not just `test_version_sync`), so any regression in a
    red-team-fix area blocks publish at CI time (Red 1 H6).
- **`DEFAULT_MODEL_BUDGETS` default lowered from 1M to 200K** for
  Opus 4.7 / Opus 4.6 / Sonnet 4.6 (Red 1 F5). The 1M window is a
  beta-gated Anthropic feature requiring `context-1m-2025-08-07`
  plus tier ≥4 rate limits; library consumers without beta access
  would silently miss their real 200K ceiling (auto-compaction
  would not trigger, user's call would hit Anthropic's 400
  "prompt too long"). Opt-in via new `CONCINNO_OPUS_1M_BETA=1`
  env var bumps Opus/Sonnet to 1M for consumers who actually
  have beta access. Haiku 4.5 unchanged at 200K.
- **`opus47` fast-mode tokenizer profile removed** (Red 1 F4). The
  ratio constants (`cjk * 2.0 + ascii / 3`) could not be anchored
  to a published Anthropic source and are now demonstrably wrong
  as a generic char-ratio approximation of a BPE tokenizer. Callers
  that need 4.7-accurate counts should use `mode='accurate'` or
  `mode='hybrid'` so the real Anthropic `count_tokens` API is
  authoritative. `_tokenizer_for()` now always returns `legacy`;
  passing `tokenizer='opus47'` to `_estimate_fast` raises
  `ValueError` rather than silently substituting.
- **`VersionSyncGuard` NotebookEdit branch actually wired up**
  (Red 2 F-R2-2). 2.2.0 added `NotebookEdit` to `_WATCHED_TOOLS`
  but the code had no explicit branch for it, so NotebookEdit
  bumps fell through the `else: MultiEdit` parse that reads
  `tool_input['edits']` — NotebookEdit uses `new_source` /
  `cell_source`, so drift was silently ALLOWed. Explicit branch
  added + 3 new tests (`new_source` drift, legacy `cell_source`
  drift, no-version quiet-allow). Unknown write tools in the
  watch set now return an explicit `"unhandled write tool"`
  advisory instead of silent ALLOW.
- **`VersionSyncGuard` audit log has an actual test** (Red 2 H-R2-3).
  The H2 fix (JSONL audit on `CONCINNO_SKIP_VERSION_GATE=1`) had
  no test coverage — 2 new tests exercise the write path end to
  end. Fallback order for the log destination now prefers
  `cache_dir`, then `<workspace>/.concinno/logs/` (was
  `_AI_BRAIN/logs/`, a CC-private path violating the library
  boundary per `CLAUDE.md:Hard Rules #1`), then XDG cache, then
  `~/.cache/concinno/`. Library-neutral fallback chain.
- **A2A Agent Card reads `concinno.__version__`** (Red 3 A14).
  `src/concinno/a2a/server.py:42` hard-coded `"version": "1.5.1"`,
  7 minor versions behind the package. Now imported from
  `concinno.__version__` so remote peers fingerprinting the
  server see the actual shipped version.
- **`vscode_extension` WIREDO recipe written tooling-neutrally**
  (Red 1 F6). The 2.2.0 recipe hard-coded `windows` agent-Skill
  API calls (`w.window_list()`, `w.screenshot_window(hwnd=…)`)
  which are CC-private to the publishing workspace — consumers
  running `pip install concinno` do not have that Skill and
  would be stuck in Tier 1 + waiver. Recipe now describes Tier 2
  UI verification abstractly: "any headless-capable UI automation
  stack" with the `windows` Skill as one example among pywinauto /
  AppleScript / Linux accessibility.
- **CHANGELOG 2.0.0/2.1.0 back-fill language honest** (Red 2
  F-R2-1, Red 3 A2). 2.2.0's back-fill paragraph cited commit
  hashes `fd781b11` and `17f097ca` as "archaeology from git log" —
  those hashes do not exist in this repo's history (`git log --all`
  earliest is `b7b2d777`); they were outer-workspace commits not
  reachable from the package repo. Language rewritten to own this
  up: the entries are authorial back-fill from the wheel diff and
  session logs, not archaeological retrieval.

### Note on PyPI 2.0.0 / 2.1.0

Both versions shipped to PyPI before `VersionSyncGuard` existed and
before the red-team review cycle was applied. The publishing account
intends to yank both via the PyPI web UI (yank, not delete — pins
still resolve but pip emits a warning). The yank reasons will point
to this 2.3.0 entry. This 2.3.0 release is the first version built
against the hardened publish pipeline above.

## [2.2.0] - 2026-04-17

### Added

- **WIREDO `vscode_extension` change_type** — UI-asset delivery recipe for
  `.vsix` files. Required dims: `W I D E O` (R is N/A). D-dim recipe
  forces two-tier evidence:
  - Tier 1 (static) — unzip the vsix, assert `package.json` version /
    `contributes.commands` / `contributes.keybindings` / `contributes.
    configuration.properties` all present, `dist/extension.js` bundle
    string grep for every new mode/slash literal, CHANGELOG entry exists.
  - Tier 2 (background UI) — `code --new-window --user-data-dir=<tmp>
    --extensions-dir=<tmp>` isolated profile install → `--list-extensions`
    confirms `publisher.name@version` → launch detached VSCode on the
    workspace → `windows` Skill `w.window_list()` + `w.screenshot_window
    (hwnd=…)` PrintWindow capture without stealing foreground focus →
    screenshot file > 10 KB + HWND title matches workspace name →
    `taskkill /PID <pid> /T /F` cleanup.
  - If `windows` Skill is unavailable this recipe degrades to Tier 1
    only; declaring delivery without Tier 2 evidence needs explicit
    operator waiver.
- **Auto-classification wiring**: `wiredo_change_type._EXT_MAP[".vsix"]
  = "vscode_extension"` + new `_VSCE_CMD` regex catching `vsce package`,
  `vsce publish`, `@vscode/vsce package` → `detect_from_command` picks
  the new recipe on `.vsix` writes or `vsce` invocations.
- **`token_counter._estimate_fast(text, tokenizer="opus47")`** — Opus 4.7
  ships a new tokenizer that fits the same text into ≈1.0–1.35x more
  tokens (per Anthropic v2 release notes). Worst-case +35% on ASCII,
  +33% on CJK. `TokenCounter(model="claude-opus-4-7")` auto-picks the
  `opus47` ratio via `_tokenizer_for(model)`; default `claude-opus-4-6`
  keeps legacy `cjk*1.5 + ascii/4`. Prevents late hybrid-escalation-cliff
  surprises on 4.7 sessions.
- **`DEFAULT_MODEL_BUDGETS["claude-opus-4-7"] = 1_000_000`** in
  `cache/autocompact.py` so AutoCompactor no longer falls back to the
  200K unknown-model ceiling when running under Opus 4.7.

### Changed

- **`CHANGE_TYPES` 16 → 17** with new `vscode_extension` slot, mirrored
  in `wiredo_change_type.py` and `wiredo_loader.py` `ROUTING` map
  (`"vscode_extension": ("W","I","D","E","O")`).
  `templates/wiredo/routing.md` gained the corresponding row.

### Tests

- **+3 opus47-tokenizer tests** in `test_token_counter.py`:
  `test_fast_mode_opus47_ascii_higher_than_legacy`,
  `test_fast_mode_opus47_cjk_higher_than_legacy`,
  `test_fast_mode_opus47_via_tokencounter_model`. Token counter: 20/20.
- **`test_change_types_count_16 → _17`** with `"vscode_extension" in
  CHANGE_TYPES` assertion. `test_wiredo_loader` + `test_wiredo_change_type`
  groups: 78/78.
- **Full suite: 4990 passed** (single pre-existing fail is
  `test_version_sync`; fixed by this bump).

### Fixed

- **`test_version_sync` drift** — `concinno.__version__` / `pyproject.toml`
  / `CHANGELOG.md` latest entry were out of alignment (2.0.0 / 2.1.0 /
  1.18.1). All three now point at 2.2.0.
- **Historical gap back-filled** — 2.0.0 and 2.1.0 versions had been bumped
  in `pyproject.toml` / `__init__.py` without CHANGELOG entries (shipped
  via `auto: update` commits in an outer workspace repository; those
  commits are not reachable from this package's own git history and
  cannot be cited by SHA here). The entries below reconstruct intent
  from the wheel diff and outer-workspace session logs — treat them as
  authorial back-fill, not archaeological retrieval. Going forward the
  new `VersionSyncGuard` (see Added) makes this class of drift
  impossible, and `test_version_sync` in `publish.yml` will block any
  future release with a CHANGELOG / `__version__` / `pyproject.toml`
  mismatch from reaching PyPI.

### Added — root-cause fix for version drift

- **`VersionSyncGuard` PreToolUse gate** — any Edit / Write / MultiEdit
  that mutates the version line in `pyproject.toml` or `__init__.py` now
  triggers a cross-check: the new version must match the latest non-
  `[Unreleased]` heading in the same project's `CHANGELOG.md`. Mismatch
  returns ALLOW with a visible step-back warning listing exactly which
  of the three sources still disagree and how to align them. Policy is
  ASK (not DENY) so legitimate in-progress multi-edit flows work, but
  the agent can no longer silently ship a bump without touching the
  changelog. Env escape `CONCINNO_SKIP_VERSION_GATE=1` for archaeology
  sessions that intentionally backfill history. Test coverage locks
  the three drift patterns (py-only, init-only, changelog-missing).
- **CI `test_version_sync` gate in `publish.yml`** — the PyPI publish
  workflow now runs `pytest tests/test_version_sync.py` before the
  `publish` job; a three-source mismatch blocks upload. Pairs with the
  edit-time guard for defense-in-depth: guard catches at write time,
  CI catches at publish time.

## [2.1.0] - 2026-04-17

### Added

- **Opus 4.7 as default escalation tier** — `CONCINNO_OPUS_MODEL`
  defaults to `claude-opus-4-7` (previously `claude-opus-4-6`). The
  `CONCINNO_OPUS_MODEL` env var still overrides for pinning to an
  older model. Scored +12pp CursorBench vs 4.6 in Anthropic's own
  evaluations.
- **`_is_opus_4_7_plus(model_id)` helper** — centralised detector for
  Opus 4.7+ behavioural differences. Treats `claude-opus-4-7`,
  `claude-opus-4-7-<date>`, `claude-opus-4-8`, `claude-opus-5-0`, … as
  4.7+; `claude-opus-4-6` and earlier, plus all Sonnet/Haiku, as
  legacy.

### Changed

- **`LLMEscalator._call_anthropic` omits `temperature` on Opus 4.7+** —
  Opus 4.7 returns a 400 error when `temperature` / `top_p` / `top_k`
  are set to any non-default value. The helper now strips
  `temperature` from kwargs when the target model is 4.7+, while
  keeping the parameter intact for 4.6/Sonnet/Haiku (which still
  accept it). Callers pass the same `temperature` argument; the
  wrapper decides whether it reaches the API.
- **`LLMEscalator.escalate` default `max_tokens` raised 2048 → 4096** —
  Opus 4.7's new tokenizer uses up to ~1.35x more tokens for the same
  text, so the previous default left very little headroom. The
  kwarg is still overridable per call.

## [2.0.0] - 2026-04-16

### Changed — SemVer major bump, no code-level breaking change

- **Version jumped 1.18.1 → 2.0.0** for PyPI namespace alignment with the
  `concinno` brand. The local Python package path was already `concinno`
  before this bump; no `tempero → concinno` rename diff exists in this
  release — the source tree migration happened in earlier unrecorded
  commits. This entry is the honest attribution after the fact.
- **No public API surface change vs 1.18.1.** Downstream consumers can
  upgrade `concinno>=1.18,<3` → `concinno>=2,<3` with no import edits.
- **PyPI project-name migration is the only real breaking signal:** an
  older `tempero` project (if any downstream still pinned it) is
  orphaned from this release onward. If such a pin exists in the wild,
  re-point the requirements file at `concinno` and keep the version
  constraint. No automatic shim package is published.

### Historical attribution — back-filled 2026-04-17

The 2.0.0 entry was missing from the CHANGELOG at release time; it
shipped via an `auto:` commit on the outer workspace repo (not reachable
from this package's wheel or from `cd site-packages/concinno && git log`
on the downstream install). The 2.2.0 release adds `VersionSyncGuard` +
`publish.yml` `test_version_sync` gate so this class of drift — shipping
a version bump without a matching CHANGELOG section — cannot recur.

## [1.18.1] - 2026-04-16

### Fixed

- **`handoff_required_guard` second-layer structural gate** — a handoff file
  appearing in `git diff` is no longer sufficient on its own. The diff must
  also carry real structural content: at least 10 added lines AND at least 2
  distinct structural signals from (`✅` / `⬜` / `⏸` / `★` status markers,
  `next_step:` field, new H2 section, new `### Session` record, commit
  hash, or Markdown doc link). A one-line `last_updated:` frontmatter bump
  previously bypassed the guard — root cause of the "handoff file touched
  but nothing written" bug (`feedback_handoff_guard_too_lenient.md`).

### Added

- **`CONCINNO_HANDOFF_MINIMAL=1` env escape** — explicit acknowledgment that
  the handoff update is intentionally minimal (pointer-only bump, frontmatter
  refresh). Skips the second-layer structural gate without disabling the
  first-layer "handoff must exist" check.
- **`feature_config.handoff_required_guard` params** — `structural_gate_enabled`
  (default `True`), `min_added_lines` (default `10`), `min_signal_hits`
  (default `2`). Fine-grained control without monkey-patching the guard.

## [1.18.0] - 2026-04-16

### Added

- **Competition mode** — `HANDOFF_MODES = ("save-token", "phase", "full", "competition")`.
  New fourth handoff mode tuned for benchmark / bounty autonomous execution.
  Silences `handoff_required_guard` and `cbua_pipeline_guard` reminders so the
  agent can run long experiment loops without reminder interrupts, and
  documents a FieldRead hint flag for the competition Skill to eager-load
  track SOPs. Other modes (save-token / phase / full) keep their existing
  reminder semantics unchanged.

### Changed

- **`_behavioral_silent_ack` dual-path threshold** — the silent acknowledgment
  heuristic in `cbua_pipeline_guard` now triggers on `reads >= 3 OR bashes >= 8`
  (previously `reads >= edits` only, which never fired in heavy-edit sessions
  like 135 edits / 6 reads and left the B1 reminder shouting forever).
  A new `bash_count` state counter backs the second path. Behavioral
  markers still required, but heavy-edit sessions no longer get stuck in
  permanent reminder mode. See `feedback_cbua_markers_are_anchors.md`.

### Refactored

- **`cbua_pipeline_guard.on_post_tool` 146 → 53 lines** — extracted the
  state `_update` closure so the post-tool path reads linearly. Pure
  refactor, no behavior change; full test suite stays green.

### Fixed

- **`_is_secret` basename match** — the secret-file detector in
  `git_assist` used to substring-match the full path, which falsely
  flagged benign files whose parent directory happened to contain a
  keyword (e.g. `secrets-docs/readme.md`). Now matches on basename
  only, so `git add -A` + `git reset HEAD --` unstage path is tight.

## [1.17.4] - 2026-04-15

### Added

- **`SilentTurnEndGuard` stop-event detector** — catches the
  "silent turn end after mutating tool chain" antipattern distilled
  in `feedback_silent_turn_end_after_tool_chain.md`. When a turn
  contains at least one mutating tool call (Write / Edit / MultiEdit /
  NotebookEdit, or Bash running `git commit|push|tag|merge|rebase|
  reset|cherry-pick|add`, `twine upload`, `pip install`, `gh pr|
  release create`, `docker build|push`, `rm`, `mv`, `chmod`, write
  redirects, etc.) and the assistant ends the turn without a final
  text block above a minimum char threshold, `concinno.stop_guard`
  emits a `[silent_turn_guard]` warn to stderr pointing at the last
  mutating tool and requesting a WIREDO-D summary next turn (what
  ran / pass-fail / next ⬜). Warn-only per CC's L6 PostToolUse
  ceiling — never denies the stop event. Bash classifier defaults
  to mutating (safe-failure bias) and splits on `&&` / `;` / `||`
  so a `git status && git commit` chain still trips. Two env vars:
  `CCC_SILENT_TURN_GUARD=0` disables the detector entirely for
  power users; `CCC_SILENT_TURN_MIN_CHARS` (default `30`) tunes the
  final-text length threshold. Wired into `hooks/on_stop.py` as a
  side-effect of the existing `stop_guard` module — no new pipeline
  entry needed. 49 new tests in `test_silent_turn_guard.py` covering
  Bash classification, tool classification, detector core cases,
  env-var plumbing, on_stop stderr integration, and env-parsing
  corner cases (bad int values fall back to default).
- **`signal_patterns`, `persona_prompt`, `track_classifier`** — three
  new persona-domain building blocks, landing as part of the
  Aegis → CCC single-source-of-truth migration (master decision
  §16.5 M4/M5/M6). Each was previously duplicated inside
  `persona-api`; these canonical copies let downstream consumers
  (persona-api, Strategos, infinite-agent) share one impl.
  - `signal_patterns.py` (114 LOC): pre-compiled regex
    `GREETING_PATTERN` / `IDENTITY_PROBE_PATTERN` /
    `CHARACTER_CHALLENGE_PATTERN` + helpers `is_greeting`,
    `is_identity_probe`, `is_character_challenge`, `detect_signals`.
    CJK patterns (`你好`, `你的過去`, `你是人工智能`, etc.)
    preserved byte-exact from upstream persona-api router.
  - `persona_prompt.py` (153 LOC): `DimBehavior` dataclass,
    `pick_behavior(value, dim)` with <0.35 / >0.65 thresholds, six
    OCEAN dim constants (`O_COMM`, `O_THINK`, `C_COMM`, `E_COMM`,
    `A_COMM`, `N_COMM`), and `build_behavior_injection(ocean,
    ocean_dims=5)` producing the `[Persona Behavior Profile]`
    header text byte-identical to pre-migration output.
  - `track_classifier.py` (125 LOC): `SAFETY_KW` / `CYBER_KW` /
    `CODING_KW` / `PERSONA_KW` regex constants and
    `classify_track(text)` with priority-order logic
    (persona → coding → cyber → safety-default). `TRACKS` tuple
    omits `"persona"` because persona routes into the safety track
    by design (persona engine layers on top, not a sibling).
- **57 new tests** across `test_signal_patterns.py` (15),
  `test_persona_prompt.py` (21), `test_track_classifier.py` (21).
  Pure stdlib, zero new dependencies. Direct submodule import only
  (not re-exported at package top level), matching the
  `concinno.cache.append_only_log` pattern.
- **`CbuaPipelineGuard` dichotomy framing detector** — anchors the
  model away from RLHF's comparative-analysis bias. When agent output
  contains binary A-or-B framing (`保留 or 改`, `二選一`, `either A
  or B`, `keep or switch`, etc.) without any integrative synthesis
  language (`A+B`, `共存`, `dual-mode`, `multi-mode`, `unified
  framework`), the guard injects a reminder to ask "can A+B coexist
  at a higher level?" first. Targets a real failure mode: experiments
  disprove one method, the model frames the fix as A-vs-B instead of
  finding the dual-mode framework that keeps both. Five new tests in
  `test_cbua_pipeline_guard.py` cover the regex patterns and
  reminder generation gates.

## [1.17.3] - 2026-04-15

### Added

- **`concinno.cache.append_only_log`** — lossless cross-session
  event log targeting CC's L9 ceiling (compact is a single LLM
  summary; events not in the top-5 files × 5K + top-skills × 5K
  are lost forever, see `services/compact/compact.ts:122-131`).
  This module captures raw events BEFORE compact so the original
  chain can be replayed at any time — useful for acquisition due
  diligence, debug postmortems, and cross-session reasoning replay.
  - `LogEvent` dataclass: event_id / session_id / timestamp /
    event_type / payload / parent_event_id / tokens_estimate /
    schema_version.
  - `AppendOnlyLog` class: `append`, `append_batch`, `read_session`,
    `iter_session`, `list_sessions`, `search`, `stats`.
  - Append-only by design: no update or delete methods.
  - JSON lines format, one event per line. Streaming-safe append,
    crash-safe (partial lines silently skipped on read).
  - Session-scoped files: `<log_dir>/YYYY-MM-DD/<session_id>.jsonl`,
    O(1) daily retention + session lookup.
  - Schema-versioned: `schema_version` field for future migration.
  - Size-bounded replay: `read_session(limit=N)` for safety.
  - Log dir overridable via `CCC_APPEND_LOG_DIR` env var;
    defaults to `~/.concinno_cache/append_only_log/`.
  - Zero new dependencies — stdlib only (json / time / uuid /
    pathlib / os). Keeps CCC core zero-dep rule intact.
  - ZIQ index hook left as future work (comment-tagged in source)
    to avoid coupling append path to retrieval layer in this release.
- **`tests/test_append_only_log.py`** — 31 new tests covering
  dataclass shape, env-var override, append + batch, cross-day
  session reads, iter_session streaming, list_sessions filters,
  search with date_range + limit, stats counters, crash-safe
  corrupted-line handling, empty/missing session, and unicode
  payloads (ensure_ascii=False).

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
  `.concinno_cache/<namespace>/` were accumulating because each guard
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
  `os.path.basename(root)` (the project tag, e.g. `concinno`) and
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

- **`concinno.agent.retrieve_pipeline`** — new module exposing
  `ZIQCascadePipeline` orchestrator and `CascadePipelineResult`
  dataclass. Composes `IterativeRetriever` (L3→L2→L1 cascade) with
  `ZIQRetrieval` (FTRL source-weight rerank). Runs the cascade and
  only feeds L1 `raw_hits` through `ZIQRetrieval.rerank`; L3/L2
  cache-only results pass through unreranked because FTRL source-type
  classification depends on file path structure that in-memory pool
  sections do not cleanly expose. 6 new tests.
- **`concinno.agent` facade re-exports** — `IterativeRetriever`,
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
parallel dispatch, permission FSM, and bash validator pipeline into concinno
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
  `CONCINNO_MEMDIR` env var. 642 lines, 32 tests.

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

- `concinno.cache` subpackage re-exports all 10 P0/P1/P2 classes +
  constants + sinks.
- `concinno.agent` subpackage extends with fork_context + parallel_dispatch
  symbols + `COORDINATOR_PROMPT_SNIPPET` constant.
- `concinno.security` subpackage with permission_mode + bash_validators
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
  `pip install concinno` now get the same fork-cache / bash-validator /
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
  imports so `import concinno.escalation` stays cheap. Claude tiers silently
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

- **Default install now includes LLM deps** — `pip install concinno`
  gives you regex guards + LLM semantic guards out of the box.
  No more guessing which extra to pick.
- **Install tiers**:
  - `pip install concinno` — full power (LLM included, ~5MB extra)
  - `pip install concinno[all]` — everything (LLM + RAG)
  - `pip install concinno[rag]` — adds RAG retrieval (~2GB)
  - `pip install concinno[llm]` — LLM only (same as default now)
  - `pip install concinno[lite]` — zero-dep, regex only
- Existing users can upgrade tiers seamlessly:
  `pip install --upgrade concinno[all]` adds missing deps without
  reinstalling.

## [1.7.0] - 2026-04-12

Break the zero-dep ceiling: LLM-backed semantic guards.

### Added

- **`concinno[llm]` optional dependency** — `pip install concinno[llm]`
  adds `anthropic` + `openai` SDK. Core remains zero-dep.
- **`LLMGuard` abstract base class** — subclass it, set `judge_prompt`,
  get LLM-level semantic judgment with fail-open fallback. If no LLM
  SDK installed, returns None (ALLOW) so regex guards still protect.
- **`SemanticInjectionGuard`** — first concrete LLM guard. Detects
  prompt injection via semantic analysis: encoded attacks, multi-
  language injection, indirect injection in data, social engineering,
  role-play injection. Goes beyond what regex can catch.
- Supports Anthropic (Haiku default) and OpenAI (gpt-4o-mini fallback).
  Model configurable via `CONCINNO_LLM_MODEL` env var.

### Notes

- `pip install concinno` still zero-dep (regex-only, 0ms).
- `pip install concinno[llm]` adds semantic depth (~500ms per check).
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
  regex if concinno not installed. Zero breaking change to Aegis API.
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
- **Three shipped rewriters** (`concinno.guards.rewrite_guards`):
  - `BashDryRunRewriter` — `rm -rf .` / `rm -fr <glob>` → `echo
    '[dry-run] would have run: …'`, preserving the original form
    as a shell comment.
  - `WriteSecretFileRewriter` — `Write(.env | credentials.json |
    secrets.yaml | …)` → `.env.example` / `credentials.example.json`
    / `secrets.example.yaml`. Preserves flavor suffixes
    (`.env.prod` → `.env.example.prod`). Edit is intentionally
    left alone (rotation, not materialisation).
  - `BashPipeToShellRewriter` — `curl … | bash` / `wget … | sh`
    rewritten to `curl -fsSL URL -o /tmp/concinno-download.sh &&
    echo 'inspect before running'`.

  Registered early in the QUALITY layer so downstream guards
  (SecretScan, ExfilGuard, …) see the rewritten input.
- **`concinno.prompt_hooks`** — LLM-as-Judge reopen (1.3.0 H1). CCC
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
- **`concinno` top-level exports** — `BashDryRunRewriter`,
  `BashPipeToShellRewriter`, `WriteSecretFileRewriter`, `PromptJudge`,
  `HALLUCINATION_JUDGE`, `EXCUSE_SCANNER_JUDGE`, `CODE_QUALITY_JUDGE`,
  `ALL_JUDGES`, `build_hook_config`, `install_prompt_hooks`,
  `uninstall_prompt_hooks`, `list_installed_judges`. Strangers can
  now reach the 1.4.0 surface with a single `import concinno`.

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
  `concinno.__version__`. Drift now fails the suite.

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
  `projects/concinno/src/concinno/cognitive_anchor.py` and
  `.claude/rules/00-L0.md` — both absolute to the author's workspace.
  New load order: `CONCINNO_IDENTITY_PATH` env var →
  `<workspace>/.concinno/identity.md` → (empty); iron laws come from
  `CONCINNO_L0_RULES_PATH` → `<workspace>/.concinno/l0.md` →
  `<workspace>/CLAUDE.md` → (empty). CCC hard rule #1 (no personal
  paths in source) upheld. Dead helper `_extract_anchor_identity`
  removed.
- **`concinno.__version__` aligned with `pyproject.toml`** — was
  stuck at `"1.1.0"` before this release; a stranger calling
  `concinno.__version__` at runtime got a value that matched no
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
  `src/concinno/_cognitive/__init__.py`; sdist already had it via
  the sdist force-include fix. `pip install concinno` and
  `pip install --no-binary=:all: concinno` now produce identical
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
- **P2 Persona Router** — `concinno.persona_router` module
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
  and sdist. Wheel verified to contain `concinno/_cognitive/__init__.py`
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
- **Skills system**: `concinno skills install/list/create` + SKILL.md templates
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

- **Autopilot module**: Autonomous task execution with MODULES registry + CLI 8 subcommands (`concinno autopilot status/start/stop/config/logs/history/retry/reset`)
- **Destruction Guard module**: R0-R4 risk classification for tool calls, backup engine, audit logging, integrated into `on_pre_tool.py`
- **Backup CLI**: `concinno backup list/cleanup/restore/pin/unpin` — 5 subcommands for managing safety backups
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
- **Benchmark suite**: 7 benchmarks with competitor comparison (`concinno benchmark`)
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
- **C2**: `concinno init` copies real hook templates (not missing source files)
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
- **CLI**: `concinno init`, `concinno enable/disable`, `concinno status`
