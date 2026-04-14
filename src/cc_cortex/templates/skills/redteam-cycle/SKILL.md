---
name: redteam-cycle
description: >-
  Full CBUA red-team cycle for critical decisions. Use when making
  architecture decisions, irreversible changes, confidence <80% on
  complex tasks, or user says "紅隊", "壓測", "重大決策".
user-invocable: true
allowed-tools: Agent(*) Read Grep
---

# Red Team Cycle

I design as blue. Red is always a clean subagent.
Self-redteam is theater.

## 觸發條件（任一命中）

- 架構級設計 / 新模組 / 新 pipeline
- 重寫核心邏輯 >200 行
- 不可逆決策（DB schema / API / release）
- 投入 >1 天的方向性選擇
- 用戶說「紅隊」「壓測」「重大決策」
- 信心 <80% 且複雜度 ≥ Complicated

## 流程

### 1. CBUA 最佳解
C0 路由 → B1 三層思考 → ≥3 替代方案 → 選甜蜜點 + 為何不選其他

### 2. 藍隊防禦
列 3-5 Top Weakness（含失敗成本 + 緩解方式）

### 3. 派 3 Opus 紅隊
每個不同角度壓測。Prompt 含完整方案 + 約束 + 「找弱點不留情」

**You MUST** 驗證紅隊 output >0 bytes（API 529 假死 → 重派）

### 4. 判斷攻擊偏移
紅隊把「需要驗證」誇大成「FATAL」= 偏移。有一說一，不被牽著走。

### 5. CBUA 修復漏洞
只修有效攻擊。偏移的標記但不改。

### 6. Checkpoint
更新交接 + 沉澱 feedback。

## ⛔ 硬上限

- 每 session 紅隊子代理 **≤2 次**
- 超過需用戶 explicit approval
- 降級：budget 耗盡 → 藍隊自爆 + 主代理驗證（不派子代理）

## 爆炸半徑分級

| 半徑 | 紅隊強度 |
|------|----------|
| High（新架構/不可逆/>$5） | 全 Opus ×3，本流程全跑 |
| Medium（已驗證延伸/可逆） | 藍隊自爆 + 主驗證，不派 Opus |
| Low（ablation/<$1/<1hr） | 跳過，checkpoint 監控 |
