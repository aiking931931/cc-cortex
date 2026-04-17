---
name: prompt_select
description: Prompt engineering strategy selector. Match problem type to reasoning pattern. Triggers on "提示詞", "reasoning", "CoT", "ToT", "思考策略".
user-invocable: false
---

# /prompt_select — 思考策略選擇器

I match the problem shape to the right thinking tool.

| Problem shape | Strategy | Core idea |
|---------------|----------|-----------|
| Linear logic | Chain of Thought | Step by step |
| Multiple paths | Tree of Thought | Explore 2-3 branches, pick best |
| Tunnel vision | Step-Back | Reframe at higher abstraction |
| Too big | Plan-and-Solve | List sub-tasks before solving any |
| Need to verify | Chain of Verification | Generate → question → check each |
| Draft quality | Self-Refine | Draft → critique → improve |

**Decision flow**: One step? → Just do it. Linear? → CoT. Multiple paths? → ToT. Stuck? → Step-Back. Too big? → Decompose. Unsure? → CoVe.

**Guardrails**: ToT max 3 branches. CoT check intermediates on long chains. Step-Back after 2+ failed attempts, not before.

根據 `$ARGUMENTS` 的問題類型，選定最佳思考策略並套用。
