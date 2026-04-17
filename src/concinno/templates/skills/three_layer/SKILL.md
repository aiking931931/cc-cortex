---
name: three_layer
description: Three-layer thinking v4.0. Root cause → sweet spot → strategy + direction questioning + consequence foresight + macro/micro toggle. CBUA C2 core tool. Triggers on "三層", "分析", "trade-off", "choose".
user-invocable: true
---

# /three_layer — 三層思考 v4.0（CBUA C2 核心工具）

I don't jump to solutions. I diagnose first, then design. I question my own direction before I question the problem.

**L1 Root Cause + Direction Check**:
- What broke? Fix cause not symptom. "What changed?" · "Where does it NOT happen?" · "What should be here but isn't?"
- List 3+ possible causes → each scored: CP = likelihood × ease-of-verify → verify highest CP first.
- **Direction questioning**: Am I solving the right problem? Does the premise still hold? What if I overturn this direction entirely?
- **Skeptical overturn**: Am I anchored by sunk cost? Would I still choose this if starting fresh?
- Can fix root → fix it. Can't → treat symptom consciously, don't pretend it's a cure.

**L2 Sweet Spot + Consequence Foresight**:
- 2-3 options, pick simplest that solves root cause.
- **Six questions** (every option, mandatory):
  1. What does this solve?
  2. What does this sacrifice? (= side effects, present cost)
  3. Is the sacrifice acceptable?
  4. 3 steps / 10 steps / 100 steps later? (= consequences, future impact)
  5. Short pain vs long pain: which, and why?
  6. Is there a simpler way?
- Fewer moving parts wins. Reversible > irreversible. Don't over-engineer.
- **Inversion check**: How would this fail? Avoid those paths.

**L3 Strategy + Macro/Micro Toggle** (only when stuck or high complexity):
- Step-Back (reframe higher) · Decomposition (split sub-problems) · Analogy (where did similar succeed?)
- **Macro/micro toggle**: Big picture → system-level view. Detail → implementation zoom. Never both simultaneously. C4 auto-switches by task phase.
- **Feynman check**: Explain in simple words. Can't explain = don't understand.

**Entry gate**: Simple → just do it. Complex → L1→L2. Stuck 2+ rounds → L3.
Multiple options with no clear winner → silently iterate 3 rounds, don't ask user.

根據 `$ARGUMENTS` 對指定問題跑 L1→L2→L3 分析，輸出結論和選定方案。
