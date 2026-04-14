# 硬閘設計

## 已實作硬閘（CCC 43 guards 中的 RLHF 專用閘）

### 1. 溢出閘 OverflowGate — `overflow_gate.py`

- **攔截**：B1 注意力溢出
- **觸發**：token zone ORANGE+ 且偵測到 burst（短時間多次工具調用）
- **動作**：deny + 具體指引（當前 zone + 建議收斂步驟）
- **紅隊實測**：4/10 繞過率（已修復）→ **fail-closed**（zone 不可讀 = YELLOW，不再 fail-open）
- **教訓**：fail-open 是安全閘的致命設計錯誤。讀不到狀態 = 假設最壞情況

### 2. 定向閘 OrientationGate — `orientation_gate.py`

- **攔截**：B2 短視本能 / B3 行動本能
- **觸發**：長時間操作（deploy/install/build/clone 等）無先前 planning evidence
- **動作**：deny → 強制先回答成本/替代/失敗後果三問
- **紅隊實測**：~1/10 繞過率（已修復）→ **移除 `run_in_background` 豁免**
- **教訓**：`run_in_background` 是資源管理策略，不是 planning evidence。系統規則要求 >30s 一律 background → 豁免 background = gate 自毀。消融實驗 FP（npm install 被攔）實為正確行為
- **Effectiveness**：Medium（regex 偵測 planning keywords，有上限但成本低）

### 3. 誠實閘 HonestyGate — `honesty_gate.py`

- **攔截**：A5 損失規避
- **觸發**：regex 偵測 20 個淡化短語（含中文「略有偏差」「大致上」等）
- **動作**：deny → 要求直白重述
- **紅隊實測**：2/10 繞過率
- **Effectiveness**：Low — regex 永遠抓不到**省略型討好**（不提錯誤）。需 2nd LLM call（Aegis 層級）
- **保留理由**：成本極低（幾行 regex），能抓到的雖少但抓到就值

### 4. 多路閘 MultiPathGate — `multipath_gate.py`

- **攔截**：B4 收束過早 / B5 首答鎖定
- **觸發**：Complicated+ 決策點只列 1 個選項
- **動作**：deny → 強制列 ≥3 選項
- **紅隊實測**：6/10 繞過率（定位正確但產出稻草人方案）
- **Effectiveness**：Medium — 能強制產出多方案，但方案品質無法用 regex 判斷
- **天花板**：判斷「方案品質」需 2nd LLM call，CCC 層做不到

### 5. 里程碑閘 MilestoneGate — `milestone_gate.py`

- **攔截**：D3 SOP 漂移
- **觸發**：GREEN=20步 / YELLOW=10步 / ORANGE+=5步 注入回檢
- **動作**：additionalContext 注入具體指引（task name + 下一步 + 偏離修正）
- **紅隊實測**：v1 為 0/10（劇場式「你偏了嗎？」無效）→ v2 改為具體指引後有效
- **教訓**：這是軟警告定律 v2 的起源。模糊提醒 = 負收益，具體指引 = 正收益

### 6. 均衡熔斷器 EquilibriumBreaker — `equilibrium_breaker.py`

- **攔截**：Guard 過度 deny 導致 AI 癱瘓
- **觸發**：壓力值 ≥5（累計 deny 次數，陰增快 +1.0 / 陽增慢 -0.2）
- **動作**：QUALITY 層暫停 10 步（SECURITY 永不暫停）+ tick() 每步衰減
- **紅隊實測**：3 bug 已修（cooldown 改 per-tool-call / pressure 可在 cooldown 中衰減 / tick() 整合 pipeline）

## 消融實驗結果（Aegis 22 場景）

| 指標 | 數值 |
| --- | --- |
| Baseline detection | **100%**（22/22 惡意場景全攔截） |
| False positive | **10%**（1/10 benign = OrientationGate 對 npm install 攔截，已確認正確） |
| SAFETY 層貢獻 | **+45%**（移除 → 2 漏網：SQL injection + XSS） |
| QUALITY 層貢獻 | **+25%**（移除 → 降 FP，但犧牲品質保護） |
| EFFICIENCY 層貢獻 | +5% |
| COGNITIVE 層貢獻 | 0%（stateful only，消融測試無法覆蓋） |

**關鍵發現**：

- `injection_guard` 是 SQL/XSS 的**唯一防線**（移除 → 2 漏網）
- `destruction_guard` 與 `git_safety` 有冗餘保護（移除一個另一個仍攔截）
- QUALITY 層移除反而降 FP，但這不代表 QUALITY 無用 — 它防的是品質漂移，不是安全漏洞

## 無法硬化的副作用（CCC 天花板）

| 副作用 | 原因 | 解法層級 |
| --- | --- | --- |
| C1 討好（省略型） | CCC 看不到思考過程，只看工具 I/O | **Aegis**：三位一體 2nd LLM call |
| D1 模式套用 | 無法判斷「套模式」vs「模式就是對的」 | **Aegis**：AG 閘 R2 輪 |
| D2 過度修正 | 需量化修正幅度 vs 問題嚴重度 | **Aegis**：ARBITER 平衡器 |
| 方案品質 | regex 能判斷數量不能判斷品質 | **Aegis**：2nd LLM 評分 |
| 純文字回覆 | AI 不調工具時 CCC 完全無法介入 | **無限代理**：母代理主動監視 |
| 跨對話模式 | CCC 每次 tool call 獨立，無長期記憶 | **無限代理**：持久化行為分析 |

## 軟警告定律 v2（2026-03-26 修正）

三級判斷：

| 情境 | 做法 | ROI |
| --- | --- | --- |
| 能物理阻斷 | gate deny（硬擋） | **最高** |
| 不能硬擋，但能給**具體指引** | additionalContext + 具體修正步驟 | **正收益** |
| 只能說「小心」「你偏了嗎？」 | **不做** | **負收益** |

具體指引格式（≤3 行，可執行）：

```text
⚠ [缺陷ID] [哪步錯] → [該怎麼做]
  證據：[觸發條件/數據]
  修正：[具體動作，一步到位]
```

範例：

```text
⚠ A2 虛假精確：估算 dense 嵌入時間無計算依據
  證據：給出「約 20 分鐘」但無 benchmark 數據
  修正：計算實測速率 × 剩餘量，或回答「無法估算」
```

**注意力挾持時效果**：2026-03-26 實驗顯示，即使 AI 在高負荷/犯錯狀態，具體指引（指出錯在哪 + 怎麼修）仍有糾正效果。模糊提醒則無效。

## 母子代理反轉架構（AI King 提出）

**核心**：母代理 = 監督掌舵，子代理 = 執行（可替換）。

- 母代理永不停機，專注評分/指路/回饋
- 子代理壞了/溢出 → 殺掉重開新的，帶交接訊息
- 解決：永續對話、注意力溢出、SOP 漂移
- 詳見：`_AI_BRAIN/05_Planning/RLHF副作用函數.md`
