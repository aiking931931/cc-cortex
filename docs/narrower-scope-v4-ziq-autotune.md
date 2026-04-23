# narrower-scope v4 — ZIQ autotune loop + cascade propagation reaffirmed

> Appends two user directives (2026-04-23) onto v3:
> 1. "ZIQ 自調最佳參數" — ZIQ not only routes, it **learns optimal values**
>    from outcome signal and writes back into the preset cascade.
> 2. "一切換就牽一髮動全身" — cascade is the core mechanism; one
>    `set_preset()` call cascades to every dependent switch automatically.

## v3 → v4 delta

### 1. ZIQ autotuner loop (three-stage lifecycle per switch)

```
Stage 1 (boot):
    preset.summary[key] → initial cascade value at set_preset() time
Stage 2 (runtime):
    ZIQ collects outcome signal (SPS × FTRL α_t feedback)
    cached until "update-worthy" signal delta accumulates
Stage 3 (writeback):
    a. preset.json::presets.<name>.summary[key] updated — cascade
       default **evolves** without human edit
    b. append to ~/.concinno/ziq_autotune_log.jsonl
       (who changed / when / old / new / reason / α_t at decision)
    c. user_pinned.json untouched — user override always wins
```

Priority chain (unchanged from v3):

```
user_pinned > ZIQ_runtime > preset(now ZIQ-learned) > FEATURE_META default
```

Code sketch:

```python
class ZIQAutoTuneLoop:
    def tick(self, context: dict) -> list[AuditEntry]:
        changes = []
        for key, meta in FEATURE_META.items():
            if not meta.ziq_autotunable or meta.cosmetic:
                continue                          # rule #6 excluded
            outcome = collect_outcome(key, context)
            new_val = self.tuner.update(key, outcome)
            current = current_preset_value(key)
            if significant_delta(new_val, current, meta):
                set_preset_value(
                    active_preset, key, new_val,
                    origin=('ziq', 'autotune'),
                )
                changes.append(audit_log(key, current, new_val))
        return changes
```

### 2. Cascade propagation scope reaffirmed

`set_preset("benchmark")` cascades across **four layers**, not just Concinno
library:

1. **Concinno library switches** — `release_auth`, `destruction_guard`,
   `handoff_required_guard`, `sweep_guard`, `redteam`, `butterfly_guard` etc.
   (via `feature_config.set_feature(origin=('preset', preset_name))`)
2. **Sancio runtime switches** — if Sancio consumer is installed, cascade
   reads via `entry_points` group `concinno.preset_consumers` and calls each
   consumer's `apply_preset(preset_name)` hook. Sancio hook example:
   `persona.runtime.apply_preset` flips `SANCIO_REQUIRE_AUTH` + benchmark
   audit minimal + ollama provider selection when it lands.
3. **Skill-level switches** — global skills in `~/.claude/skills/*/` with
   frontmatter `preset_aware: true` + `preset_cascade: {benchmark: ...}`
   have their in-skill flags read at trigger time (new-feature phase gates,
   credentials rotation cadence, etc.). Skills do NOT auto-fire — they
   just read the current preset when invoked.
4. **Hook-level configurations** — hooks registered in settings.json that
   read `get_active_preset()` adjust per-run (e.g. `token_gate.agent_threshold`
   uses preset value + ZIQ autotune delta).

The cascade is typed via **Pydantic PresetModel** (ship-time schema
validation) + property-tested via **hypothesis sampled_from(preset) ×
sampled_from(FEATURE_KEYS)** (ship-time drift catch).

## LOC delta v3 → v4

| component | v3 | v4 delta | v4 total |
|---|---|---|---|
| Preset three-layer schema + Pydantic | 60 | 0 | 60 |
| effective_value router | 50 | 0 | 50 |
| FieldRead L3 lazy-load | 20 | 0 | 20 |
| CLI `concinno preset show/set/list` | 50 | 0 | 50 |
| Config.set_feature origin tuple | 40 | 0 | 40 |
| File lock + atomic rename | 30 | 0 | 30 |
| SessionStart additionalContext | 20 | 0 | 20 |
| DSPy-style test compile | 60 | 0 | 60 |
| Multi-layer preset discovery | 30 | 0 | 30 |
| **ZIQAutoTuneLoop.tick()** | 0 | **+40** | **40** |
| **JSONL audit log** | 0 | **+20** | **20** |
| **entry_points `concinno.preset_consumers`** | 0 | **+20** | **20** |
| **Sancio apply_preset hook** | 0 | **+30** | **30** (persona-api side) |
| **Code total** | **360** | **+110** | **~470** |
| **Tests** | 80 | +25 (ZIQ→preset propagation verify) | ~105 |
| **Docs** | 350 lines | +60 lines | ~410 lines |

Still < the 900-LOC red-team estimate. v4 adds auto-evolution without
architectural bloat because every new line reuses an existing module
(`ziq_autotuner`, `feature_config.set_feature`, `entry_points`).

## Next-session precondition (appended to v3's 5)

6. Read `projects/concinno/src/concinno/ziq_autotuner.py` + verify:
   - `ZIQAutoTuner.update(key, outcome)` return signature
   - Where `outcome` signal originates (per-tool-call / per-session / per-turn)
   - Existing audit log pattern (reuse if present)

7. Read `projects/concinno/src/concinno/feature_config.py::FEATURE_META` and
   for every entry sanity-check `ziq_autotunable` + `cosmetic` flags are
   honest, not cargo-culted:
   - `toast_title` → `ziq_autotunable=False, cosmetic=True` (learning
     "right" title is absurd)
   - `token_gate.agent_threshold` → `ziq_autotunable=True, cosmetic=False`
     (outcome-learnable)
   - `destruction_guard.enabled` → `ziq_autotunable=False, cosmetic=False`
     (safety — never auto-flip)

8. Verify Sancio consumes `concinno.preset_consumers` entry_points — add to
   `persona-api/pyproject.toml::[project.entry-points."concinno.preset_consumers"]`
   `sancio = "persona.runtime:apply_preset"` pointing at a new
   `apply_preset(name: str) -> None` function that flips `SANCIO_REQUIRE_AUTH`
   + audit level + provider preference per preset.

## Why ZIQ autotune + cascade is the right combination

User's intuition validated by four independent signals:

- **MEMORY #17** — ZIQ SPS router +1.31pp on IMPLIRET proves dynamic
  learning beats static config at per-query granularity
- **MEMORY #44** — Goldilocks Prior N=351 30/30 seeds shows ZIQ's
  cold-start-to-warm trajectory works — starting from preset cascade
  baseline and converging to learned optimum matches this pattern
- **Constitutional AI** — Anthropic's own base + RLHF + CAI architecture
  is the same 3-layer (default / runtime-learned / user-override) we're
  building
- **DSPy** — "Don't prompt. Program." — preset + ZIQ autotune is the
  Python equivalent of a DSPy compiled signature that tunes over time

The design is not novel per se — it's applying proven prior art to the
specific Claude Code + Concinno harness.

## Implementation Status (2026-04-23 — Sancio session ship)

All v4 scope landed on local inner repo. PyPI ship is Sancio-coordinator
territory (see `co-session-boundary.md`). Files:

### Concinno library

- `src/concinno/preset_model.py` (121 LOC) — Pydantic schema
  (`PresetModel`, `PresetsFile`) with name-charset + key/name consistency
  validation.
- `src/concinno/preset_cascade.py` (~440 LOC) — multi-layer discovery,
  `set_preset()`, `get_effective_value()`, `set_preset_value()`, file-lock
  + atomic rename for preset.json writes. Origin sidecar at
  `~/.concinno/preset_origins.json`.
- `src/concinno/ziq_autotune_loop.py` (~220 LOC) — outcome-driven
  cascade updater with significant_delta + JSONL audit log.
  Registry-id alias resolver bridges `feature.param` ↔ source-path ids.
- `src/concinno/cli/preset_cmd.py` (~210 LOC) — `concinno preset
  show | set | list | autotune-log`; wired into `cli/main.py`.
- `src/concinno/feature_config.py` — `set_feature` gained `origin=` kwarg
  + new `list_autotunable()` helper. All 35 entries audited with honest
  `ziq_autotunable` + `cosmetic` flags (12 tunable, 5 cosmetic, 18 safety/
  quality/structural). Origin sidecar write is fail-soft.
- `src/concinno/data/__init__.py` + `data/preset_default.json` —
  built-in `benchmark` / `general` / `prod` presets (3 layers × 11 keys).
- `src/concinno/hooks/on_session_start.py` — injects
  `active_preset=<name>` into `hookSpecificOutput.additionalContext`
  so agents read cascade choice on first turn.
- `pyproject.toml` — declares
  `[project.entry-points."concinno.preset_consumers"]` group.

### Sancio consumer (persona-api)

- `src/persona/runtime.py` (110 LOC) — `apply_preset(name)` hook flips
  `SANCIO_REQUIRE_AUTH` + audit level + provider preference. Idempotent,
  safe on unknown name, zero concinno import dependency.
- `pyproject.toml` — registers
  `sancio = "persona.runtime:apply_preset"` in
  `[project.entry-points."concinno.preset_consumers"]`.

### Tests (all green)

| Suite | Count |
|---|---|
| `tests/test_preset_model.py` | 15 |
| `tests/test_preset_cascade.py` | 20 |
| `tests/test_preset_cascade_consumers.py` | 4 |
| `tests/test_ziq_autotune_loop.py` | 13 |
| `tests/test_ziq_autotune_loop_property.py` | 2 (hypothesis) |
| `tests/test_preset_cli.py` | 9 |
| `tests/test_feature_config_origin.py` | 10 |
| persona-api `tests/test_runtime_apply_preset.py` | 8 |
| **new concinno subtotal** | **73** |
| **persona-api subtotal** | **8** |

Pre-ship regression: 6389 tests collected vs 6316 baseline, all pass
(6385 pass / 1 skip / 3 xfail carried from prior cycle).
`ruff check src/ tests/` is clean.
