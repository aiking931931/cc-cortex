---
name: debug_loop
description: Structured debugging loop. Hypothesize → verify → narrow. Triggers on "debug", "除錯", "bug", "broken", "troubleshoot".
user-invocable: true
---

# /debug_loop — 結構化除錯

I don't guess. I narrow. Observe → Hypothesize → Test → Narrow → Repeat.

**Observe**: What happened? What was expected? When did it start? Where does it NOT happen?

**Hypothesize**: 2-3 testable guesses ranked by likelihood. Format: "If [X], then [observable Y]."

**Test**: Most likely first. Change one thing, observe one result. Large space → binary search. Don't fix and test simultaneously — isolate cause first.

**Narrow**: Confirmed → fix root cause. Refuted → eliminate, next hypothesis. Inconclusive → refine test.

**Escape hatches**:
- 3+ hypotheses failed → Step-Back, re-observe with fresh eyes
- Can reproduce but can't locate → add tracing at boundaries
- Can't reproduce → find the environmental difference
- Fix works but don't understand why → don't ship it

根據 `$ARGUMENTS` 的 bug 描述，執行結構化除錯流程。
