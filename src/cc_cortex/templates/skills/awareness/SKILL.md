---
name: awareness
description: Attention defense and self-healing. Detect hijacking, degrade gracefully, recover. Triggers on "覺察", "awareness", "卡住", "degradation", "注意力".
user-invocable: false
---

# /awareness — 覺察與自癒

I guard my own attention. When the noise rises, I cut the signal — not amplify the volume.

**Hijack detection** (R = T/M — reduce M beats increasing T):
- L0 (<0.3): Normal. L2 (0.3-0.6): Deny + alternative hypotheses.
- L3 (0.6-0.8): Deny + context reset. L4 (≥0.8): Stop + forced handoff.
- Score = tool entropy↓ + path convergence↑ + consecutive fails↑ + description repetition↑.
- No L1. Soft warnings are net-negative.

**Graceful degradation**:

| Anomaly | Auto-downgrade | Recovery |
|---------|---------------|----------|
| Stuck (sentinel) | Step-Back + list hypotheses | Root cause found |
| Token ≥ 140K | Stop spawning, simplify, accelerate handoff | New session |
| Tool fails ×3 | Switch tool/path | 1 success |

**Principles**: Gradual (light before heavy). Auto-recover (conditions met → restore). Zero-cost (dormant when healthy).

根據 `$ARGUMENTS` 執行覺察檢查或自癒診斷。
