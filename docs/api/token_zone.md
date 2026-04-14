# Token Zone

`cc_cortex.token_zone` — Three-zone token management with per-model profiles.

Classifies the current token count into `GREEN` / `YELLOW` / `RED` zones using
absolute thresholds adapted per model (Opus/Sonnet 1M, Haiku 200K). Generates
UX display strings and handoff/compact injection text. Supports `persona_mode`
(silent) and `full mode` (no forced handoff).

## Model Profiles

| Model | Context Limit | Quality Zone | Force Handoff |
| --- | --- | --- | --- |
| Opus 4.6 | 1,000,000 | 200,000 | 800,000 |
| Sonnet 4.6 | 1,000,000 | 200,000 | 800,000 |
| Haiku 4.5 | 200,000 | 200,000 | 170,000 |
| Mythos 2M | 2,000,000 | 400,000 | 1,600,000 |

## API

::: cc_cortex.token_zone
    options:
      show_root_heading: false
      members:
        - Zone
        - MODEL_PROFILES
        - detect_model
        - detect_zone_abs
        - format_ux
        - zone_injection
        - should_gate_tool
