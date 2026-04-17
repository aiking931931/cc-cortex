# 研究前沿（全球最強認知/行為架構）

## AI 認知架構

### AGoT — Adaptive Graph of Thought（自適應圖推理）
- DAG 結構推理，節點可合併/精煉/回饋修改前提
- 超越 CoT（線性）和 ToT（樹狀）→ 圖結構
- **CBUA 位置**：B2 深度探索核心工具
- **優勢**：非線性推理，結論可反向修正前提

### CLAI — Cognitive Load-Aware Inference（認知負荷感知推理）
- 量化 token 消耗 vs 推理品質的關係
- 動態調整推理深度以匹配任務需求
- **CBUA 位置**：B4 預算監控 + 認知守恆定律的數值化

### Dualformer — Fast/Slow Thinking（快慢思考）
- 類 Kahneman 的 System 1/2 在 LLM 中的實作
- 快速路徑（pattern match）vs 慢速路徑（deliberate reasoning）
- **CBUA 位置**：B0（快速）vs B1/B2（慢速）的路由就是這個

### MAR — Multi-Agent Reflexion（多代理反省）
- 多視角同時反省同一問題（工程師/用戶/攻擊者/審計者）
- 超越單視角 Reflexion
- **CBUA 位置**：B5 自我修正中的多視角反省

### STaR — Self-Taught Reasoner（自學推理者）
- 用自己的正確推理來訓練自己
- 成功 → 強化該推理路徑。失敗 → 弱化。
- **CBUA 位置**：A4 適應（學習循環 = 運行時的 STaR）

### BIGMAS — Brain-Inspired Graph Multi-Agent System
- 模仿大腦區域分工的多代理系統
- 專門化代理 + 圖結構協調
- **CBUA 位置**：無限代理的跨 Agent 認知協調

## 行為/決策架構

### OODA Loop（Boyd）
- Observe → Orient → Decide → Act
- 軍事決策循環，強調速度和情境意識
- **CBUA vs OODA**：OODA 無元認知、無驗證、無自我修正。CBUA 的 C1 定向 ≈ Orient，但多了 B4 + A3 + A5

### Cynefin Framework（Snowden）
- Simple / Complicated / Complex / Chaotic / Disorder
- 基於因果關係的問題分類
- **CBUA 整合**：C0 複雜度分類直接採用 Cynefin 四域

### PDCA（Deming）
- Plan → Do → Check → Adjust
- 品質管理循環
- **CBUA 位置**：B3-A4 就是 PDCA 的超集（多了 C1 定向和 A5 防護）

### ReAct（Reasoning + Acting）
- 推理和行動交替進行
- **CBUA 位置**：A0-A2 執行的核心模式

### Reflexion
- 語言化失敗分析 → 存入記憶 → 重試
- **CBUA 位置**：B5 自我修正第一步

## CBUA vs 競品比較

| 維度 | CBUA | OODA | Cynefin | PDCA | ReAct | Reflexion |
|------|------|------|---------|------|-------|-----------|
| 認知分層 | 6 級 C0,B0-B2,B4-B5 | 2 步(O-O) | 分類框架 | 無 | 無 | 1 步 |
| 行為分層 | 6 相 C1-C2,B3,U,A0-A5 | 2 步(D-A) | 無 | 4 步 | 2 步 | 1 步 |
| 元認知 | B4 常駐 | 無 | 無 | Check | 無 | 有(語言化) |
| 自適應 | Tier×Cynefin | 速度適應 | 域→策略 | 無 | 無 | 無 |
| AI 原生能力 | 9 項 | 0 | 0 | 0 | 1(工具) | 1(反省) |
| 驗證 | WIREDO 6×5 | 無 | 無 | Check | 無 | 無 |
| 防護 | A5 8 層 | 無 | 無 | 無 | 無 | 無 |
| 自我修正 | B5 三敗升級 | 無 | 無 | Adjust | 無 | 有 |
| 複雜度匹配 | ✅ 自動路由 | ❌ | ✅ 分類 | ❌ | ❌ | ❌ |
| 人類版可用 | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ |

**CBUA 贏在哪**：
1. **唯一同時有認知+行為+元認知+防護的統一架構**——其他都只覆蓋一部分
2. **AI 原生九能力**——不是人類認知的模仿，是 AI 獨有的超越
3. **自適應**——根據模型能力和任務複雜度自動調整，其他都是固定流程
4. **WIREDO 驗證**——唯一內建多資產類型品質閘
5. **書籍理論支撐**——意識張力論/河床論/樁理論提供哲學和心理學基礎，不是純工程拼裝
