# Provisional Patent Spec — Skeleton Template

> Copy this structure for every new invention. Fill placeholders
> `<...>`. Field-tested format from FieldRead 2026-04 filing.
> Implemented example: `experiments/cbua_plan_a/PATENT_SKELETON.md`.

## 頂部 metadata

```markdown
<!-- markdownlint-disable MD013 -->
# US Provisional Patent Application — <Invention short name>

**Filing type:** Provisional ($160 micro-entity)
**Applicant:** <Legal name (Chinese/Taiwanese: 羅馬拼音 + 中文)>
**Correspondence address:** <Postal address that receives mail>
**Citizenship:** <國籍>
**Filing target:** TBD — the legal filing date is set by the USPTO
receipt timestamp on submission day, not by any date in this draft.
All other dates in this file are historical (experiment runs,
red-team reviews, claim edits) and remain accurate regardless of
when filing happens.
**⛔ MUST file BEFORE arxiv upload or any public disclosure.**
```

## 必備八段

### 1. Title

單行、descriptive、limited scope。好範本：
`Method and System for <specific technique> Using <input> in <context>,
Comprising a <embodiment A> and a <embodiment B>`

避免空泛（`AI Method for Efficient Computing` = 太寬）。

### 2. Field of the Invention

1 段，≤100 字，describing the technical field（e.g., "natural
language processing", "large language model inference optimization"）。

### 3. Background

Prior art 列 3-5 class，每類 1-2 個代表作 + 一句話限制：

```markdown
1. **<Prior-art class 1>** (e.g., LLMLingua 2, Pan 2024) — trained
   classifier approach; requires labeled data and inference overhead.
2. **<Prior-art class 2>** (e.g., SnapKV, Li 2024) — KV eviction in
   decode; does not operate on prompt-level token selection.
...
```

⛔ 不要 bash 競爭者。陳述事實即可。

### 4. Summary of the Invention

≤2 頁，回答：
- What problem does this solve?
- How does it differ from prior art?（1 句話）
- What embodiment(s) are claimed?
- Scale/performance evidence（引 specific 實驗數字 + source-aligned 檔案路徑）

### 5. Claims（15-20 條）

#### Independent claims（3 條上限，<claim scope> 由寬到窄）

```markdown
**Claim 1 (broadest, method)**: A method for <doing X>, comprising:
  (a) <step 1>;
  (b) <step 2>;
  (c) <step 3>;
  wherein <technical constraint>.

**Claim 1A (alternative embodiment method)**: <同 Claim 1 但替代途徑>

**Claim 1B (deployment decision rule)**: 根據 <condition> 選
<embodiment A> 或 <embodiment B>。
```

#### Dependent claims — 核心差異化

細節 claims 1-5 條，每條 narrows 一個 specific technical detail
（pooling function / layer selection / ratio / threshold），對抗
prior art 的具體 claim。

#### Dependent claims — 部署最佳化

1-3 條，描述 `cross-model deployment` / `adaptive ratio` /
`chunked scoring` 這類下游 pipeline 組合（Claim 6 泛化用）。

#### Dependent claims — Robustness

1-3 條，描述 `fallback`, `edge case`, `numerical stability` 處理。

#### Dependent claims — 技術效果限定（§101 加固）

1-2 條 narrow the invention to a specific technical improvement（避免
§101 abstract idea rejection）：

```markdown
**Claim N**: The method of Claim 1, wherein the <technique> reduces
<memory/latency/...> by at least <N%> compared to <baseline>, as
demonstrated on <specific benchmark with n=M>.
```

### 6. Brief Description of Figures

每張 figure 1-2 句 caption。用論文既有 figures，不重畫。

### 7. Detailed Description

- §1 System overview（1-2 頁，figure 解說）
- §2 Method details（每個 claim step 對應 1 段 written description）
- §3 Alternative embodiments（Claim 1A / 1B / dependent claim 對應）
- §4 Experimental validation — scale + cross-dataset + cross-model
- §5 Implementation details（code structure, hyperparameters, edge cases）

⛔ **§112 enablement**：每個 claim 必須在 §7 有 written description
passage 支持。PhD 讀完能 reproduce。

### 8. 實施例（說明書用，非 claim）

1-3 個 concrete 部署組態 + 實驗數據：

```markdown
**優選實施例 A（<name> — <zero-shot / pilot-calibrated / ...>）**:
- Config: <具體參數>
- 數據：<benchmark> n=<N> F1 <X> vs <baseline> <Y> (+<Δ>pp,
  p<<0.01> paired bootstrap B=<10000> <correction>)
- Cross-family: <Model A / Model B / Model C 驗證>
- 消融：<ablation 結果摘要>
- 適用情境：<when to use this embodiment>

**Scale replication evidence（<date>, strengthens enablement）**:
<hypothesis H1 — cells 4/4 pass + table>
<hypothesis H2 — transfer task + table>
```

## Filing Checklist（spec 完成後自檢）

- [ ] Title ≤200 chars，specific not generic
- [ ] Background 包含最近 2 年 prior art（避免 examiner 找到你沒引的）
- [ ] 所有 Independent claims 有 Alternative（Claim 1/1A/1B）
- [ ] Dependent claims ≥12 條（cover novelty depth）
- [ ] 每個 claim 有 §7 written description passage 對應
- [ ] ≥1 § Technical Effect claim（§101 加固）
- [ ] ≥2 embodiments 有實驗數據 + source-aligned JSON
- [ ] Figures 從論文匯出（不重畫）
- [ ] Prior art 表格在 spec 底部（examiner 友善）
- [ ] 發明人 legal name 正確（不用 alias）
- [ ] 日期欄位按 `date-rules.md` 處理

## Prior Art References（for examiner）

```markdown
1. <Pan et al.>, "<LLMLingua 2>", <venue year> — <1-line limitation>
2. <Jiang et al.>, "<LongLLMLingua>", <venue year> — <1-line limitation>
...
```

列 5-10 筆，每筆一行 limitation。examiner 看這段快速理解 diff。
