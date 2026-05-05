# 專利日期規則 — Draft Date vs Legal Filing Date

> 新手最常搞錯的一題：檔案上的日期 vs USPTO 表格上的 filing date。
> 本 SOP 從 FieldRead 2026-04 filing 實踐整理。
> ⚠ **非法律意見**，第一次 filing 建議諮詢美國專利律師 $500-1000 initial consult。

## 三類日期，各自規則不同

### A. 事實紀錄日期（Historical Fact）

**定義**：實驗做於 X 天 / claim 改於 X 天 / 紅隊跑於 X 天 / commit timestamp。

**規則**：**保留真實日期，不動**。這些是 invention timeline 的證據
（reduction to practice / conception date 佐證）。Git log + lab
notebook + commit timestamp 都該一致。

**寫在哪**：
- Spec §7 Detailed Description 的實驗段
- `## Claims (紅隊壓測後修正版 — YYYY-MM-DD)` section header
- `## Scale replication evidence（YYYY-MM-DD/YY, strengthens enablement）`
- Inventor declaration 的 conception date / reduction to practice date

**為什麼真實**：如果未來訴訟 / 對抗別人的 prior art，invention date
證據（AIA 前 first-to-invent 殘留 + §102(b)(1)(A) grace period 1 年
豁免窗）要能對齊 Git/notebook。偽造 = fraud on USPTO。

### B. 計畫/目標日期（Aspirational Metadata）

**定義**：`Filing target: 2026-04`、`Plan to submit in Q2`、
「下一版 2.11.0 target」這類 draft metadata。

**規則**：**可寫可改可刪，沒有法律效力**。只是作者自己規劃，和
USPTO / priority date 無關。

**推薦寫法**：若不確定實際送件日，寫 `TBD`：

```markdown
**Filing target:** TBD — the legal filing date is set by the USPTO
receipt timestamp on submission day, not by any date in this draft.
All other dates in this file are historical (experiment runs,
red-team reviews, claim edits) and remain accurate regardless of
when filing happens.
```

**為什麼**：寫 "2026-04" 可能讓未來的自己或合作者誤以為 "已 filed at
2026-04"。TBD + 註解清楚 decouple 草稿日 vs 法律日。

### C. 法律綁定日期（Legal Filing Date）

**定義**：USPTO 送件當天，系統蓋章的 receipt timestamp。
出現在 Application Data Sheet / Filing Receipt / Application
Confirmation No.。

**規則**：
1. **等於 priority date**（Provisional 鎖 12 個月優先權從這天起算）
2. **不能偽造、不能 back-date、不能 forward-date**
3. **不出現在 draft md 裡** — 是 USPTO 回傳的系統蓋章，你手動填不了
4. 送件當天就是 filing date — 想要早的 filing date → 早一天送件

**為什麼偽造 = 刑事**：Inequitable conduct → 專利作廢 + 可能被告
fraud on USPTO（18 USC §1001 false statements, 5 年 max）。

## America Invents Act (AIA) 2013-03-16 after

美國 2013 後走 **first-to-file**（之前 first-to-invent）。對新手影響：

- **你的 invention date 不能壓制別人更早的 filing**（即使你構思更早）
- **唯一例外**：§102(b)(1)(A) 自己公開 ≤12 月前 + filing 前（很少用）
- **實務結論**：早 file 贏晚 file，**invention date 只作為自 prior-art 豁免證據**

## 決策樹

```
檔案裡有個日期 →
  這個日期描述「過去發生的事」（實驗 / commit / review）?
    ├─ Yes → A 類，寫真實日期，不動
    └─ No →
        描述「計畫發生的事」（filing target / 未來 version）?
          ├─ Yes → B 類，寫 TBD 或具體但加 "aspirational" 註解
          └─ No → C 類，只在 USPTO 表格上由系統填，不在 draft 裡
```

## 常見錯誤 vs 正確

| ❌ 錯誤 | ✅ 正確 |
|---|---|
| Draft 寫 `Filed: 2026-04-20`（但還沒送） | `Filing target: TBD` 或 `Draft as of 2026-04-20`（只是 working-copy date）|
| Inventor declaration 寫 `Invention date: <filing 日>` | 寫真實 conception date，對齊 Git log |
| 為求早 priority 填 `Filing date: 2026-01-01`（實際 2026-04 送）| 2026-04 送就是 2026-04 filing date，偽造是刑事 |
| Spec 頂部沒任何日期 metadata | 至少寫 `Draft as of <today>` 或 `Filing target: TBD` |
| `Claims (v3 — 2026-04-12)` 改日期為 filing 日混淆 | `Claims (v3 — 2026-04-12)` 保留真實 claim edit 日期 |

## 給 user 確認的最終指引

1. **Draft 裡任何日期你不確定該不該改** → 問自己：「這是過去發生的
   事嗎？」是 → 不動。否 → 改 TBD 或刪掉。
2. **USPTO 表格上 filing date 欄位** → 送件當天系統自動填，**你手
   不要碰**。
3. **Git commit 時間戳、lab notebook 手寫日期、research notebook 截
   圖** → 永遠保存，未來爭議時是 invention date 證據。
4. **若 draft 寫了計畫送件日但實際延後** → 改 TBD，或不改（都沒法
   律效力）。
5. **「我自己都不確定幾月幾號要正式申請」** → 直接寫 `TBD`，不影響
   法律 priority（送件當天才決定）。
