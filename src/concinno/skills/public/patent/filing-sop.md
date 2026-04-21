# USPTO Provisional Patent Filing SOP

> 完整送件流程 — 從 spec 草稿到 USPTO receipt + arxiv 協調 + 12 個月時序。
> 實例見 `experiments/cbua_plan_a/FILING_SOP.md`。

## 順序鐵律（不可逆）

```
Day 0   : spec 完整 + prior art 掃過 + claim 紅隊過
Day 1   : USPTO provisional filing → 拿 Application Number + Filing Date
Day 2+  : arxiv preprint（只在 filing 後隔天才上傳）
Day 3+  : ACL/EMNLP/NeurIPS submission
Month 11: 提前 1 個月轉 non-provisional（$5-15K 成本 / 律師協助）
Month 12: ⛔ Provisional 失效死線，錯過不可恢復
```

⛔ **arxiv 搶先 filing = 毀國際專利**。EP/CN/JP 無 grace period，一公開就
失 novelty。美國有 1 年 AIA §102(b)(1)(A) grace，但只對 inventor 自己
公開，且只在 filing 鎖 priority 後才安全。

## Step 1 — USPTO Provisional Filing（$160 micro entity）

### 送件前 checklist

- [ ] Spec 含完整 method（enough for PhD student to reproduce）≥15 頁
- [ ] 所有 claim 在 spec 有 written description support（§112 enablement）
- [ ] 發明人**真名**（不是筆名 / alias）
- [ ] Correspondence address 能**收實體信**（USPTO 通知走郵寄）
- [ ] Micro entity 條件符合：(a) 收入 <3× 美國家庭中位數 +
  (b) 未在 >4 個 US application 列為 inventor
- [ ] 尚無任何 public disclosure（arxiv / blog / talk / demo 均否）
- [ ] 信用卡 / 銀行帳戶可付 USPTO $160（Pay.gov）
- [ ] Prior art 已跑過（讀 `prior-art-checklist.md`）
- [ ] Claim 已紅隊壓測（SnapKV / H2O / LLMLingua 類直接對手）

### 送件管道 — USPTO Patent Center（官方）

1. 註冊 <https://patentcenter.uspto.gov/>（Google 搜尋官方入口，
   USPTO URL 變動時以搜尋結果為準）
2. 選「Provisional Application for Patent」
3. 上傳四份必要文件：
   - **Application Data Sheet (ADS)** — 填發明人 / correspondence / title
   - **Specification** — `skeleton-template.md` 擴寫的完整版 PDF ≥15 頁
   - **Claims** — 15-20 條（3 independent + 12-17 dependent）
   - **Figures** — 從論文匯出的 PDF
4. **Micro Entity Certification form** — 確認資格簽名
5. Pay.gov 付 $160（大實體 $1,600 / 小 $320 / 微 $160）
6. 提交 → 取得
   - **Application Number**（17/XXX,XXX）
   - **Filing Date** = USPTO 系統收到當天時戳 = **priority date**

### Entity Fee table

| Entity | Fee (provisional) | 條件 |
|---|---|---|
| Large | $1,600 | 預設 |
| Small | $320 | <500 employees |
| **Micro** | **$160** | 收入 <3× median + <5 prior apps 列名 |

⛔ 填錯 entity = inequitable conduct，專利可能被作廢 / 敵對方利用。

### 發明人資訊模板

```yaml
inventor:
  name: <Legal name — real, not alias>
  birth: <YYYY/MM/DD>
  citizenship: <國籍 — 外籍仍可 file US>
  correspondence_address: <能收實體信的地址，USPTO 通知走郵寄>
  email: <通信 email>
  entity_size: micro | small | large
```

## Step 2 — arxiv Preprint（免費，filing 後隔天）

### 為什麼在 filing 之後

- arxiv = public disclosure（permanent, indexed）
- 國際：EP/CN/JP/... 無 grace period → filing 前 arxiv = novelty 毀
- 美國：1 年 §102(b)(1)(A) grace 保 inventor，但只在 priority 鎖定後才安全
- 保險做法：**先 filing → 隔天 arxiv**，永遠不冒險

### 上傳步驟

1. <https://arxiv.org/> 註冊（cs.CL 無需 endorsement）
2. 主文：長版 paper 轉 PDF（pandoc 或 LaTeX）
3. 短版：投 venue 用 submission version
4. Metadata：
   - Title / Authors / Abstract / Primary category / Secondary
   - Comments 欄位：例 "4 pages (short paper version)"
5. 提交 → 取得 arxiv ID（YYMM.XXXXX）

## Step 3 — Venue Submission（ACL/EMNLP/NeurIPS/ICLR）

### Venue 選擇表（2026 時點，實際 deadline 以官網為準）

| Venue | Deadline | 字數 | AI/ML 偏向 |
|---|---|---|---|
| ACL (ARR) | rolling | short 4p / long 8p | NLP |
| EMNLP | 年中 | short 4p / long 8p | NLP |
| NeurIPS | 年中 | 9p | ML broad |
| ICLR | 年底 | 9p | ML/representation |

### 投稿前 checklist

- [ ] Claude / AI tools 揭露：Acknowledgment 段，非 co-author
- [ ] 全文英文，無中文段落（non-English 段會被 desk reject）
- [ ] 真名作者 + Affiliation（Independent Research / University）
- [ ] Responsible NLP Research checklist 填完
- [ ] Reproducibility：code 在 anonymous GitHub repo
- [ ] Ethics statement：揭露 AI 輔助 + 專利 filing 狀態

### ARR（ACL Rolling Review）流程

1. 提交 ARR → 2 個月 reviewer
2. 評審通過 → 選 target venue（ACL/EMNLP/NAACL）
3. Camera-ready + presentation

## 風險管理

### 高風險（失去專利）

1. **spec 不夠完整** → non-provisional 擴展 claim 失 priority
   → 對策：spec ≥15 頁，涵蓋所有未來可能擴展方向
2. **arxiv 早於 filing** → 毀國際專利
   → 對策：順序鐵律，永不 swap
3. **Entity size 填錯** → inequitable conduct
   → 對策：送件前驗證 (a)(b) 條件，存證據

### 中風險（成本 / 策略）

4. **Non-provisional $5-15K** → 預算
   → 對策：等 paper impact / 商業價值再升級，或找 acqui-hire 時談
5. **台灣申請人 PCT 排除** → 無法一次進 150 國
   → 對策：只 file US，商業有價值時考慮 US LLC + PCT
6. **ACL desk reject** → AI co-author / 非英文段
   → 對策：投前全文掃 + AI 揭露改 ack

## 聯絡資源（URL 送件前自行驗證，官方可能變動）

- USPTO Patent Center — Google「USPTO Patent Center」
- USPTO Help: 1-800-786-9199（北美電話）
- Micro Entity form — Google「USPTO micro entity status form」
- 台灣發明人：TIPO（智慧財產局）官網有 USPTO 教學
- arxiv 入口：arxiv.org

⛔ **強烈建議**：第一次 filing $500-1000 諮詢一位美國專利律師
（initial consult）。比自己踩坑 + 失 priority 便宜 100 倍。
