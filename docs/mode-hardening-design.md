# Mode-hardening design — `concinno.mode.Mode`

> User directive 2026-04-23: 「把所有靠 LLM 自律的都硬化成 hook 或 python 代碼；
> 模式選擇、開關、SOP、規則，能硬化就硬化。」

> Target: Concinno next-minor (tentative 2.21.0). Red-blue CBUA attack before
> implementation.

## Problem

Today's mode switching leans on **prompt-layer LLM self-discipline**:

- `/competition-mode` / `/general-mode` / `/handoff-mode` are markdown skills.
  Switching = user types a command, LLM reads a ~50-line md, LLM is supposed
  to remember for the session.
- Consequence: LLM drifts. Observed twice this cycle:
  - `SANCIO_REQUIRE_AUTH` and `BENCHMARK_MODE` stayed on because session md
    didn't enumerate them as cascade targets.
  - `release_authorization.disabled=True` honored only after repeated user
    corrections (MEMORY #71).
- Root cause: **one md file cannot be the source of truth for N switches**.
  Each switch lives in its own module, and the md is hand-authored → drift is
  certain.

## Solution — Three-layer mode object

### Layer A — `concinno.mode.Mode` enum + cascade table (source of truth)

```python
# src/concinno/mode/__init__.py
from enum import Enum

class Mode(str, Enum):
    GENERAL = "general"       # continuous + auto-compact + memory file
    HANDOFF = "handoff"       # phase-based + manual handoff (AI King personal)
    BENCHMARK = "benchmark"   # high autonomy, no auth, minimal audit
    FULL = "full"             # autonomous to completion, no session-split
```

**Cascade table** — declarative, single source of truth. One edit cascades
to every gate / guard / feature:

```python
_MODE_CASCADE: dict[Mode, dict[str, Any]] = {
    Mode.BENCHMARK: {
        "release_authorization.disabled": True,
        "handoff_required_guard.enabled": False,
        "sweep_guard.enabled": False,
        "redteam.skip_low_radius": True,
        "redteam.high_radius_mandatory": False,  # benchmark prefers speed
        "audit.level": "minimal",
        "rate_limit_tracker.enabled": False,
        "cbua_pipeline_guard.behavioral_only": True,
        "token_gate.soft_limit": 560_000,
        "destruction_guard.enabled": True,   # never disable
    },
    Mode.HANDOFF: {
        "handoff_required_guard.enabled": True,
        "auto_commit.enabled": True,
        "sweep_guard.enabled": True,
        "redteam.high_radius_mandatory": True,
        "butterfly_guard.enabled": True,
        "destruction_guard.enabled": True,
    },
    Mode.FULL: {
        "release_authorization.disabled": True,
        "handoff_required_guard.enabled": False,
        "user_question_gate.disabled": True,
        "token_gate.soft_limit": 1_000_000,
        "subagent.default_override_on_complicated": True,
        "destruction_guard.enabled": True,
    },
    Mode.GENERAL: {
        "auto_commit.enabled": True,
        "auto_compact.enabled": True,
        "handoff_required_guard.enabled": False,
        "destruction_guard.enabled": True,
    },
}
```

### Layer B — Detection + application functions (deterministic)

```python
def detect_mode() -> Mode:
    """Priority: env > ~/.concinno/mode.json > ~/.concinno/config.json.mode
    > default=GENERAL. All branches are pure functions — no LLM, no prompt."""

def apply_mode(mode: Mode, dry_run=False) -> dict:
    """Write cascade into FEATURE_META + ~/.concinno/*.json atomically.
    Returns diff {key: (old, new)}. Preserves user overrides at higher
    priority (explicit > mode default)."""

def describe_mode(mode: Mode, layer: str = "summary") -> str:
    """Three-layer output matching L0 rule #6:
    - 'index'   → 1 line, <80 chars  (L1 index layer)
    - 'summary' → cascade table, <500 chars  (L2 summary layer)
    - 'full'    → per-switch rationale  (L3 full layer)
    LLM / hook / CLI all consume this same function."""

def set_mode(mode: Mode) -> None:
    """User-facing — write mode.json + apply cascade in one atomic op."""
```

### Layer C — SessionStart hook enforcement (kills "LLM forgot")

```python
# ~/.claude/hooks/on-session-start.py (appended block)
from concinno.mode import detect_mode, describe_mode
import sys

mode = detect_mode()
# Inject into stderr — CC turns stderr hook output into system-reminder.
# LLM sees "Active mode: benchmark" on the first turn, unconditionally.
print(describe_mode(mode, "summary"), file=sys.stderr)
```

L1 enforcement chain becomes:

```
Mode selection  ← env / json file  ← deterministic, never LLM
     ↓
Cascade table   ← declarative dict  ← change one place, N gates realign
     ↓
apply_mode()    ← FEATURE_META writes  ← atomic, audited
     ↓
SessionStart hook ← injects summary  ← LLM can't miss
     ↓
Individual guards  ← read FEATURE_META  ← already-hardened
```

No LLM self-discipline required. Mode drift is now **mechanically
impossible** — changing a switch requires editing `_MODE_CASCADE` or the
env var, neither of which the LLM does casually.

## What stays prompt-layer (honest scoping)

Not everything should be hardened. Rule of thumb: if the decision has
**structured input** (finite set of flags / booleans / thresholds), harden
it. If the decision needs **natural-language judgment** (is this response
honest? is this code clean?), keep it prompt-layer + Opus judge.

Keep prompt-layer:
- Content-quality judgments (hallucination detection, excuse scanner, code
  cleanliness — Concinno already uses `PromptJudge` Opus/Haiku for these)
- Creative work (writing, design ideation)
- Empathy / tone / user-facing language
- Novel-situation reasoning (not-yet-seen task patterns)

Harden:
- Mode selection  ← this proposal
- Switch / feature enable states
- Rate limits, thresholds, timeouts
- Permission rules (already hooks)
- Release gates (already hooks)
- Format / schema validation (already `format_guard`)
- Audit levels
- Workflow routing (new-feature 9-phase pipeline can harden too)

## Alignment with 2026 SOTA (prior-art audit)

This design borrows from seven industry patterns (none are novel on their
own; the novelty is the combination mapped onto Claude Code hooks):

| Pattern | Adopted | Source |
|---|---|---|
| Token-level constraints | N/A (no generation control here) | LMQL, Outlines, Guidance |
| Declarative compilation | cascade table = declarative not imperative | DSPy |
| Policy engine FSM | implicit — mode is the state, cascade is the transition | NeMo Guardrails |
| Output validation + reask | out-of-scope (format_guard already covers) | Guardrails AI |
| Graph-structured agent | SessionStart → mode → cascade is a one-shot DAG | LangGraph |
| Neurosymbolic split | mode = symbolic, LLM = perception + novel reasoning | ACT-R, Voyager |
| Schema-first config | FEATURE_META typed + Mode enum | Pydantic |

## 6-point DoD self-check (L0 rule #6)

| # | Point | Status |
|---|---|---|
| 1 | Switchable | Mode itself IS the switch. `~/.concinno/mode.json` + env var |
| 2 | ZIQ-aligned | N/A — mode has finite enum, no ZIQ bandit needed |
| 3 | 3-layer | `describe_mode(layer='index'/'summary'/'full')` built-in |
| 4 | Lazy-load | Cascade applied only on `apply_mode`, not module import |
| 5 | CP / SOTA / logic-max | CP — 200 LOC vs saving N drift incidents/month |
| 6 | CBUA-optimal | Red-blue attack **pending** — this doc is the input |

## Open questions for red-blue attack

1. **Mode collision**: two envs disagree (env says benchmark, json says
   handoff). Priority rule covers it, but is the priority "env wins"
   defensible? What if someone sets env from a script intending override,
   but the json is the persistent user preference?
2. **Per-switch user override**: user sets `toast_enabled=true` explicitly,
   then switches to benchmark (cascade sets toast=false). Should user's
   explicit write survive mode change? Current answer: yes, at FEATURE_META
   priority tier 6 (user session) — but no code enforces this yet.
3. **Test surface**: 30+ switches × 4 modes = 120 cells. How do we prevent
   a drift between cascade table and actual guard behavior? Property test
   with hypothesis?
4. **Mode composition**: benchmark + full (wanting "full autonomy in
   benchmark mode")? Current design: orthogonal, no composition. Is this
   the right call?
5. **LLM still needs to know the mode**: even though enforcement is code,
   the LLM should adjust its behavior too (e.g. not ask clarifying
   questions in full mode). The SessionStart hook injection handles this.
   But is stderr-level injection the right channel, or should it be a
   proper Claude Code `context` block?

## Next steps (blocked on red-blue verdict)

- [ ] Red Opus attacks this design (prompt template in `rules/L1/redteam.md`)
- [ ] Blue Opus defends (same template, opposite anchor)
- [ ] Commander 5-axis verdict (真做完 / 接線 / 功能正常 / AI 能力提升 /
      UX 方便)
- [ ] If ACCEPT → implement in `src/concinno/mode/` + wire SessionStart hook
      + 4 Mode × N switch property test + CHANGELOG entry
- [ ] If ACCEPT-降級 → trim scope, ship partial
- [ ] If 駁回 → document reason + file as a non-starter
- [ ] If 保留 → carry forward to next cycle
- [ ] If 反問 → new Opus with narrower brief
