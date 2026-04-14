---
name: pdca
description: PDCA execution cycle. Plan → Do → Check → Adjust with pacing and bottleneck handling. Triggers on "pdca", "執行", "迭代", "節奏".
user-invocable: true
---

# /pdca — PDCA 執行循環

Knowing without doing is not knowing. Plan tight, execute at the right pace, learn from every cycle.

**Plan**: Scope · Dependencies · Estimate (S≤15K / M≤40K / L≤80K tokens) · Success criteria.

**Do** — match pace to signal:

| Signal | Pace | Behavior |
|--------|------|----------|
| Clear + simple + done before | Fast | Direct, minimal research |
| Clear + complex + experienced | Steady | Step-by-step, verify each |
| Unclear + complex + new | Careful | Research first, smallest experiment |
| Stuck | Pause | Step-Back, switch strategy |

**Check**: Works (tests pass) · No breakage (existing green) · Clean (no debug residue).
**Adjust**: Estimate accurate? What surprised me? What next time?

**Bottleneck classifier**: Technical (search) · Information (read) · Decision (L1→L2) · Resource (handoff).
**Done = three dimensions**: Functional + Robust + Clean.

根據 `$ARGUMENTS` 對當前任務跑 PDCA 微循環。
