---
name: first_principles
description: First principles reasoning. Decompose to fundamentals, rebuild from ground truth. Triggers on "第一性原理", "from scratch", "rethink", "本質".
user-invocable: true
---

# /first_principles — 第一性原理

I don't inherit assumptions. I decompose to ground truth and rebuild.

**Step 1 — Surface assumptions**: What am I taking for granted?
- "We need a database" → Do we? What problem does it solve?

**Step 2 — Decompose**: Separate constraints (can't change) from conventions (could change) from assumptions (chose unconsciously).

**Step 3 — Rebuild**: Start from constraints only. "If I built this today with zero legacy, what would I do?" Then bridge to reality with minimum cost.

**Anti-patterns**: Analogy trap ("Company X does it") · Sunk cost ("We already built Y") · Authority appeal ("Docs say Z" without understanding why).

**When to use**: Questioning architecture, build-vs-buy, challenging "we've always done it this way".
**When NOT to**: Routine CRUD, time-critical fixes — use /three_layer instead.

根據 `$ARGUMENTS` 對指定主題進行第一性原理分析。
