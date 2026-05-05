---
name: patent
description: US provisional patent filing SOP + spec template + date rules. Use when drafting a provisional application, preparing claims, coordinating arxiv/venue disclosure, or clarifying filing-date vs draft-date.
triggers:
  - 專利
  - patent
  - USPTO
  - provisional
  - 臨時專利
  - 申請專利
  - filing
  - prior art
user-invocable: true
---

# 專利 Skill — US Provisional Patent SOP

> I file before I disclose. I keep invention dates truthful and filing
> dates untouchable. A provisional is cheap insurance on a 12-month
> priority window — the cost of doing it wrong is losing international
> rights forever, not losing $160. Every draft I write assumes a
> future examiner will read it skeptically; I earn the priority date
> by writing specs that enable, not specs that hint.

> **You MUST**
> 1. File provisional BEFORE any public disclosure (arxiv, blog, talk, demo).
> 2. Treat `Filing date` as the USPTO receipt timestamp on submission day —
>    never backdate, never pre-fill. Obvious fraud = patent void + criminal.
> 3. Keep invention / conception / experiment dates truthful in drafts
>    (they are evidence, not legal filing). Git log + lab notebook +
>    commit timestamps are your friends.
> 4. Spec must satisfy 35 USC 112 enablement — ≥15 pages, every claim
>    traceable to a written description passage.
> 5. Never file without running the prior-art checklist + red-team on claims.

## 鐵律（不讀全文也要記住）

1. **順序鐵律**：patent filing → arxiv → venue submission（ACL/EMNLP/...）。
   不可逆換。arxiv 前必 filed，否則 EP/CN/JP novelty 毀。
2. **Filing date 不可偽造**：USPTO 系統收件日即法律 priority date。
   草稿檔內所有 date 欄位只是 aspirational metadata，不是法律欄位。
3. **Entity size 要對**：Micro entity 條件 (收入 <3× median + <5 prior apps)
   沒過 = Small entity $320。填錯是 inequitable conduct，專利作廢。
4. **Prior art 先掃再寫 claim**：SnapKV/H2O/LLMLingua 等既有技術必先 tag
   diff，claim 要避開但 dependent 要深挖 novelty。
5. **諮詢美國專利律師**：第一次 filing $500-1000 initial consult，
   比自己踩坑便宜。

## 決策樹

```
新發明要申請 →
  有公開揭露風險（arxiv/demo/talk 在 <12 月內要做）?
    ├─ Yes → 讀 filing-sop.md 走 Step 1-3 順序鐵律
    └─ No  → 讀 skeleton-template.md 寫 spec 草稿先
  草稿寫完要檢查日期 → 讀 date-rules.md
  claim 要壓測 → 讀 prior-art-checklist.md
```

## 按需讀取（⛔ 不要一次全讀，只讀需要的）

| 要做什麼 | 讀哪個檔案 |
| --- | --- |
| USPTO 送件流程 / $160 / 12 個月時序 / arxiv 協調 | `.claude/skills/patent/filing-sop.md` |
| 新專利 spec 骨架（Title/Background/Claims/實施例） | `.claude/skills/patent/skeleton-template.md` |
| 日期該寫哪種 / filing date vs invention date / AIA first-to-file | `.claude/skills/patent/date-rules.md` |
| Prior art 查找 + claim 紅隊壓測 + §112/§101 加固 | `.claude/skills/patent/prior-art-checklist.md` |

## 實作參考

FieldRead 2026-04 filing 已走過全流程，實例見 `experiments/cbua_plan_a/`：
- `PATENT_SKELETON.md` — 實例 spec
- `FILING_SOP.md` — 實例送件 log
- `results/` — enablement evidence（實驗 JSON + bootstrap CI）

下次新專利：`cp` 骨架 + 改 placeholder，不重寫。

<!--
Skill 三層架構：
  L1 = frontmatter（本檔頂部）
  L2 = 本檔 SKILL.md（觸發時載入，速查+決策樹+路由表）
  L3 = 四個主題檔（filing-sop / skeleton-template / date-rules / prior-art-checklist）

設計原則：
  - 通用模板抽掉 FieldRead-specific 細節，改 placeholder
  - FieldRead 本身的 filled-in 版留在 experiments/cbua_plan_a/ 作實例
  - 鐵律順序：揭露前必 filed > 日期誠實 > entity size > prior art > 律師
-->
