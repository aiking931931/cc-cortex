# Prior Art 查找 + Claim 紅隊壓測 Checklist

> Filing 前必跑。Prior art 漏掉 = examiner rejection / 對手無效化。
> Claim 未紅隊 = 太寬被核駁或太窄沒用。

## Part 1 — Prior Art 查找

### 查找順序

1. **Google Scholar**：關鍵技術名 + 發明核心詞
   - 例：`"KV cache compression" attention scoring`
2. **arxiv.org**：限近 2 年 (`submittedDate:[20240101 TO 20261231]`)
3. **USPTO Patent Full-Text Search**：`patft.uspto.gov` 類似關鍵字
   - 專利搜尋比 paper 更嚴，examiner 會用
4. **Google Patents**：<https://patents.google.com/>
5. **Semantic Scholar**：找 citation graph 近鄰 paper
6. **GitHub**：關鍵字 + open-source impl（code 可作 non-patent literature）

### 分類 5 類 prior art

| 類別 | 範例 | 處理 |
|---|---|---|
| **同類直接競爭** | 相同任務 + 相同輸入 | 每個必 diff 寫入 Background，claim 避開 |
| **不同類但互補** | 解決相關問題但不同方法 | Background 提 1 句區隔 |
| **上位概念** | Transformer 本身 | Background 提作 foundation |
| **訓練類替代** | Trained version of your approach | Claim 限 training-free / zero-shot 避開 |
| **過時替代** | 10 年前 technique | 不用 cite，避免分散 novelty |

### Prior Art 表（spec 底部放這個 for examiner）

```markdown
1. <First-author et al.>, "<Title>", <venue year> —
   <1-line technical limitation that your method overcomes>
2. ...
```

5-10 筆即可。太多反而讓 examiner 質疑 novelty。每條必 1 行 limitation
（不只是 cite，要講 gap）。

### 常見 LLM/ML 領域 prior art 範例

- LLMLingua 2 (Pan 2024) — trained classifier approach
- LongLLMLingua (Jiang 2024) — external scorer LM required
- SnapKV (Li 2024) — KV eviction during decode, not prompt
- H2O (Zhang 2023) — cumulative attention KV eviction
- Selective Context (Li 2023) — perplexity-based
- EHPC (Zhang 2024) — evaluator-head single layer

## Part 2 — Claim 紅隊壓測

### 3 輪攻擊 framework

#### Round 1 — 102 novelty 攻擊

針對每個 independent claim 問：
- **有沒有 prior art exactly teach 這個 step?**
- **有沒有 prior art teach 所有 step 的 obvious combination?**

工具：`WebFetch arxiv.org/abs/<id>` 讀 abstract 比對。

典型發現：claim 寫太寬，某 step 在 prior art 已有 → 加 `wherein` 限制
narrowing。

#### Round 2 — 103 obviousness 攻擊

Examiner 常用：「A paper + B paper 組合 = 你的 claim，obvious to PHOSITA」。

防守：
- 加 unexpected result claim（`wherein <specific quantitative effect>`）
- 加 synergy claim（兩個 embodiment 共用 pipeline 時效果 >各自和）

#### Round 3 — 101 abstract idea 攻擊（軟體最常見）

USPTO 對 software 嚴格：`Alice/Mayo` two-step test。

防守：
- Claim 必 recite **technical improvement**（reduced memory, faster
  inference, specific hardware interaction）
- 加 §101 加固 claim：

```markdown
**Claim N**: The method of Claim 1, wherein the <technique> achieves
<specific measurable improvement> on <specific benchmark>, as compared
to <baseline> by at least <X%>.
```

### 紅隊 PASS criteria

- [ ] 每個 independent claim 對應 ≥1 個 prior-art class，claim 明確避開
- [ ] ≥3 dependent claims narrow specific technical details
- [ ] ≥1 §101 加固 claim（technical effect 限定）
- [ ] ≥1 synergy / unexpected result claim
- [ ] Claim 總數 15-20（過少 examiner 質疑 enablement，過多 filing
  成本高）

## Part 3 — §112 Enablement 自檢

Spec 寫完後每條 claim 對照：

| Claim | Spec 對應 passage | PhD 讀完能 reproduce? |
|---|---|---|
| Claim 1 step (a) | §X.Y | ✅/❌ |
| Claim 1 step (b) | §X.Y | ✅/❌ |
| ... | ... | ... |

任一 ❌ → spec 補對應 passage。

## 反模式（避免）

- ❌ 只查最近 1 個月 arxiv（prior art 可追 10 年）
- ❌ 只查英文（中國 CN patent 也是 prior art，Google Patents 有翻譯）
- ❌ Claim 1 寫太寬（`Method for X using attention` — 被 transformer 本
  身搞死）
- ❌ Claim 只 recite math（§101 abstract idea 高風險）
- ❌ Prior art 表空白（examiner 找到你沒引的 → inequitable conduct 風險）
- ❌ 未諮詢律師就送件（第一次 $500-1000 諮詢極值）

## 紅隊工具組合

| 工具 | 用途 |
|---|---|
| `WebFetch arxiv.org/abs/<id>` | 讀 abstract 驗 prior art 限制 |
| Explore subagent | 多層 dependent claim diff 分析 |
| Opus 紅隊（`rules/L1/redteam.md`）| High radius 時派一輪 |
| Google Patents | USPTO 專利搜尋 |
| Semantic Scholar | citation graph |
