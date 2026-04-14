---
name: mode
description: Switch cc-cortex profile between daily / competition / handoff. Competition mode silences advisory guards (CBUA / WIREDO / Read:Edit ratio) while keeping safety guards loud. Triggers on keywords like "mode", "模式", "切換", "profile", "daily", "competition", "handoff".
user-invocable: true
disable-model-invocation: true
---

# /mode — profile switcher

Switch the active cc-cortex profile. This is the knob users turn to
decide how much hook-injected prose the LLM sees.

## Profiles

| 參數 | Profile | Advisory guards | Safety guards | Notes |
|------|---------|------------------|---------------|-------|
| `daily` | `standard` | loud (inject) | loud | default — full coaching |
| `competition` | `competition` | **silenced** (audit only) | loud | benchmark / contest runs |
| `handoff` | `paranoid` | loud | loud | session-end, max audit |
| empty / `status` | — | — | — | show current profile |

Safety guards (destruction, bash validators, permission FSM, secret
scan, premise gate, butterfly, handoff-required, sentinel consecutive
fail) stay loud in every profile. Competition mode only silences
advisory prose (B1/C1/U1 markers, WIREDO six-dim reminder, Read:Edit
ratio, token zone, streak UX, think-inject nudges).

## Execution

1. Parse `$ARGUMENTS` — lowercase, trim. Empty / `status` → print
   current profile via `python -c "from cc_cortex.feature_config import get_active_profile; print(get_active_profile())"`.
2. Map alias → canonical profile:
   ```
   daily        → standard
   competition  → competition
   handoff      → paranoid
   ```
   Anything else → print error + list of known aliases, exit.
3. Apply it:
   ```bash
   python -c "from cc_cortex.feature_config import apply_profile; print('\n'.join(apply_profile('<canonical>')))"
   ```
4. Optionally export `CC_CORTEX_PROFILE=<canonical>` in the current
   shell so new subprocesses see the profile without re-reading
   `cc_config.json`.
5. Print confirmation:
   ```
   ⚙ cc-cortex profile: <alias> (<canonical>)
     └ advisory guards: <silenced|loud>
     └ safety guards:   loud
   ```

## How advisory silencing works

`GuardResult.allow_advisory(...)` marks a result as silenceable.
`GuardPipeline._collect_context` checks the active profile on each
result: when the profile is `competition` and the result is advisory,
the pipeline routes the context string to
`pipeline.advisory_audit` instead of injecting it into the LLM
prompt. Non-advisory (safety) results are never affected.

## When to use which

- **daily** — normal development. You want the coaching signals.
- **competition** — benchmark runs, contest submissions, or any
  long-running loop where hook prose keeps derailing the LLM into
  meta-commentary. Safety still blocks dangerous tool calls; you
  just stop seeing the nags.
- **handoff** — session wrap-up with maximum audit depth.

## Auto-detect hints

Words like 「比賽」「跑分」「benchmark」「ablation」 → suggest `competition`.
Words like 「交接」「handoff」「wrap up」 → suggest `handoff`.
Everything else defaults to `daily`.

## Reference

- `cc_cortex/feature_config.py::PROFILES` — canonical profile list
- `cc_cortex/feature_config.py::get_active_profile` — resolver
- `cc_cortex/guards/base.py::GuardResult.allow_advisory` — helper
- `cc_cortex/guards/pipeline.py::GuardPipeline._collect_context` — routing
