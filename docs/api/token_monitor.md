# Token Monitor

`cc_cortex.token_monitor` — Read real token usage from transcript and enforce
model-aware budget gates.

Reads `usage` fields from the Claude transcript file directly (no estimation).
Blocks Agent spawns when context approaches model-specific thresholds, derived
from `MODEL_PROFILES` in [`token_zone`](token_zone.md).

## API

::: cc_cortex.token_monitor
    options:
      show_root_heading: false
      members:
        - read_real_token_usage
        - check_budget_gate
        - TokenGuard
