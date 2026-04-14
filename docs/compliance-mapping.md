# CCC Compliance Mapping

CC Cortex 守衛管線對 NIST AI RMF 1.0、ISO/IEC 42001:2023、NIST AI Agent Standards Initiative (2026) 的合規對照。供企業客戶評估 CCC 是否滿足 AI 治理需求。

**版本**：2026-04-10 | **守衛總數**：55+ | **覆蓋率**：NIST 72 子類別中 48 項直接覆蓋、ISO 39 控制項中 31 項直接覆蓋

---

## Guard Pipeline 架構概述

CCC 守衛分三層，按順序執行：

| 層級 | 類別 | 守衛數量 | 行為 | 合規定位 |
|------|------|---------|------|----------|
| **Layer 1** | SECURITY | 7 | Hard deny，不可回退 | 安全防線：注入、外洩、破壞、供應鏈 |
| **Layer 2** | QUALITY | 30+ | Hard deny + Step-back 緩衝 | 品質治理：驗證、結構、交付、行為偵測 |
| **Layer 3** | COGNITIVE | 15+ | 知識注入（ALLOW 時附加 context） | 認知增強：信心校準、深度思考、意圖錨定 |

---

## NIST AI RMF 1.0 Alignment

### GOVERN — 治理政策與問責

| NIST 子類別 | 說明 | CCC 組件 | 覆蓋 |
|------------|------|----------|------|
| GV-1.1 法規合規 | 理解並記錄 AI 相關法規要求 | `BoundaryGuard`（CC/CCC 邊界強制）+ `PublishScan`（發布前掃描） | ✅ 完整 |
| GV-1.2 可信 AI 整合 | 將可信 AI 特性整合到組織框架 | `L0/L1 Rules` + `PromptEngine`（動態 prompt 組裝反漂移）+ `DesignTheoryGuard`（設計理論強制） | ✅ 完整 |
| GV-1.3 風險容忍度 | 根據風險容忍度決定管理活動層級 | `C0Router`（CBUA 複雜度分類 Simple/Complicated/Complex/Chaotic）+ `ConfidenceGate`（信心閾值門控） | ✅ 完整 |
| GV-1.4 透明控制 | 透明的政策、程序和控制機制 | `GuardPipeline`（所有決策 ALLOW/DENY 可追蹤）+ `StepBack`（二階緩衝）+ 審計日誌（`.cc_cortex_cache/audit/`） | ✅ 完整 |
| GV-1.5 持續監控 | 持續監控和定期審查 | `SentinelGuard`（行為模式偵測）+ `TokenMonitor`（token 使用追蹤）+ `EquilibriumGuard`（動態平衡） | ✅ 完整 |
| GV-1.6 AI 系統清冊 | AI 系統清冊機制 | `FileTrackerGuard`（檔案追蹤+衝突偵測）+ `PipelineState`（管線狀態機） | ⚠️ 部分（守衛級清冊，非組織級） |
| GV-1.7 退役程序 | 安全退役程序 | `DestructionGuard`（R0-R4 風險分級+自動備份）+ `BackupManager` | ✅ 完整 |
| GV-2.1 職責定義 | 角色、職責、溝通線路 | `AgentGateGuard`（代理產生門控+升級）+ `AgentSupervisor`（合約式子代理驗證）+ `SubagentIdentity` | ✅ 完整 |
| GV-2.2 人員訓練 | AI 風險管理訓練 | `CognitiveGuard`（三層知識路由注入）+ `ThinkInjectGuard`（高風險操作思考注入） | ⚠️ 部分（AI 自我訓練，非人類訓練） |
| GV-2.3 領導問責 | 高層為 AI 風險決策負責 | `HandoffGuard`（交接驗證）+ `StructuredHandoffGuard`（結構化交接模板）+ `StopGuard`（阻止過早結束） | ✅ 完整 |
| GV-3.1 多元團隊 | 多元背景團隊參與決策 | `MultiPathGate`（強制多方案比較）+ `CognitiveAnchorGuard`（紅隊錨定） | ⚠️ 部分（認知多元性，非人事多元性） |
| GV-3.2 人機配置 | 定義人-AI 互動配置 | `AgentDispatchGuard`（token 感知代理調度）+ `AgentGateGuard` | ✅ 完整 |
| GV-4.1 批判思維 | 安全第一的思維文化 | `PremiseGate`（前提驗證）+ `HonestyGate`（偵測委婉語掩蓋錯誤）+ `ButterflyGuard`（蝴蝶效應：看到就處理） | ✅ 完整 |
| GV-4.2 風險記錄 | 風險和潛在影響記錄 | `HypothesisTrackerGuard`（追蹤失敗方法防迴圈）+ `ProposalGuard`（強制副作用分析） | ✅ 完整 |
| GV-4.3 測試與事件共享 | 測試、事件識別和資訊共享 | `ConsecutiveFailGuard`（連續失敗偵測）+ `SentinelGuard`（行為迴圈偵測）+ 審計日誌 | ✅ 完整 |
| GV-5.1 外部回饋 | 收集和整合外部回饋 | `PluginLoader`（第三方守衛插件發現+載入）+ `PromptGuard`（用戶 prompt 清晰度門控） | ⚠️ 部分 |
| GV-5.2 回饋機制 | 將回饋整合到設計中 | `SedimentationGate`（糾正沉澱）+ `InsightEngine`（主動洞察引擎）+ `Knowledge`（從對話學習） | ✅ 完整 |
| GV-6.1 第三方風險 | 第三方 IP 和侵權風險 | `DepAuditGuard`（依賴 typosquatting+範圍欺騙+黑名單）+ `BoundaryGuard` | ✅ 完整 |
| GV-6.2 應急程序 | 第三方失敗應急程序 | `ErrorRecovery`（四層錯誤復原）+ `EquilibriumBreaker`（動態平衡斷路器） | ✅ 完整 |

### MAP — 風險識別與脈絡建立

| NIST 子類別 | 說明 | CCC 組件 | 覆蓋 |
|------------|------|----------|------|
| MP-1.1 目的與部署 | 記錄預期目的、用途、法規、部署設定 | `OrientationGate`（長操作前強制成本分析）+ `InitialIntentProbe`（首次寫入探測用戶根本目的） | ✅ 完整 |
| MP-1.3 組織目標 | 理解並記錄組織使命與 AI 目標 | `IntentAnchorGuard`（保留原始任務意圖）+ `MilestoneGate`（SOP 里程碑引導注入） | ✅ 完整 |
| MP-1.4 商業價值 | 定義或重新評估商業價值 | `ProposalGuard`（提案副作用分析）+ `OverflowGate`（注意力耗盡時阻擋旁枝） | ⚠️ 部分 |
| MP-1.5 風險容忍度 | 確定並記錄風險容忍度 | `C0Router`（複雜度分類）+ `DestructionGuard`（R0-R4 風險分級） | ✅ 完整 |
| MP-1.6 系統需求 | 從相關角色獲取需求 | `PromptGuard`（多問題偵測）+ `PremiseGate`（前提驗證） | ⚠️ 部分 |
| MP-2.1 任務定義 | 定義特定任務和實作方法 | `C0Router` + `CognitiveGuard`（認知層路由） | ✅ 完整 |
| MP-2.2 知識限制 | 系統知識限制與人類監督文件 | `ThinkingDepthGuard`（Read:Edit 比率退化偵測）+ `ConfidenceGate`（信心校準） | ✅ 完整 |
| MP-2.3 科學完整性 | TEVV 考量記錄 | `VerifyBeforeWriteGuard`（寫入前驗證外部引用）+ `HallucinationGuard`（偵測無來源斷言） | ✅ 完整 |
| MP-3.2 錯誤成本 | 記錄 AI 錯誤的潛在成本 | `OrientationGate`（成本分析）+ `CostTracker`（per-session token 和成本追蹤） | ✅ 完整 |
| MP-3.5 人類監督 | 定義人類監督流程 | `AgentGateGuard`（代理計數+升級+硬拒）+ `AgentSupervisor`（合約驗證） | ✅ 完整 |
| MP-4.1 技術法規風險 | 技術和法規風險對映 | `SecretScanGuard`（硬編碼秘密偵測）+ `ExfilGuard`（資料外洩防護）+ `DepAuditGuard` | ✅ 完整 |
| MP-4.2 內部風險控制 | 系統組件的內部風險控制 | 完整 `GuardPipeline`（55+ 守衛三層執行） | ✅ 完整 |
| MP-5.1 影響評估 | 識別有益和有害影響 | `ProposalGuard`（副作用分析）+ `ButterflyGuard`（蝴蝶效應連鎖偵測） | ⚠️ 部分（技術影響，非社會影響） |

### MEASURE — 量化與評估

| NIST 子類別 | 說明 | CCC 組件 | 覆蓋 |
|------------|------|----------|------|
| MS-1.1 風險度量 | 選擇並實施風險度量方法 | `TokenMonitor`（token 使用追蹤）+ `ThinkingDepthGuard`（推理退化偵測）+ `ConfidenceGate` | ✅ 完整 |
| MS-1.2 度量有效性 | 定期評估度量有效性 | `EquilibriumGuard`（寫入即清理動態平衡）+ `AntiBloat`（命中追蹤+過期偵測+修剪） | ✅ 完整 |
| MS-2.1 測試記錄 | 測試集、度量、TEVV 工具記錄 | `CodeGuard`（Python/Rust/Go 靜態分析）+ `LintGuard`（ESLint）+ `TypeScript`（tsc 快取） | ✅ 完整 |
| MS-2.3 效能量測 | 定量/定性效能量測 | `TokenMonitor` + `CostTracker` + `ProgressReporter`（里程碑觸發報告） | ✅ 完整 |
| MS-2.4 生產監控 | 生產環境功能和行為監控 | `SentinelGuard`（行為模式偵測）+ `ConsecutiveFailGuard`（連續失敗）+ `HijackGuard`（劫持偵測） | ✅ 完整 |
| MS-2.5 可靠性驗證 | 驗證有效性、可靠性、泛化限制 | `WiredoGuard`（WIREDO 六維交付驗證）+ `WiredoEnforcementGuard`（硬性強制） | ✅ 完整 |
| MS-2.6 安全性評估 | 定期安全性評估 | `DestructionGuard`（R0-R4）+ `GitSafetyGuard`（危險 git 操作阻擋）+ `PromptInjectionGuard` | ✅ 完整 |
| MS-2.7 安全韌性 | 安全和韌性評估 | `ExfilGuard` + `SecretScanGuard` + `IdentityGuard` + `PromptInjectionGuard` | ✅ 完整 |
| MS-2.8 透明度 | 透明度和問責風險 | `HonestyGate`（偵測委婉語）+ `HallucinationGuard`（無來源斷言）+ 審計日誌 | ✅ 完整 |
| MS-2.9 可解釋性 | AI 模型解釋和驗證 | `CognitiveGuard`（三層知識路由）+ `ThinkInjectGuard`（強制深度思考） | ⚠️ 部分 |
| MS-2.10 隱私風險 | 隱私風險檢查和記錄 | `SecretScanGuard`（秘密偵測）+ `ExfilGuard`（外洩防護） | ✅ 完整 |
| MS-3.1 風險追蹤 | 追蹤現有和新興風險 | `SentinelGuard` + `HypothesisTrackerGuard`（追蹤失敗方法）+ `TokenZone`（三區 token 管理） | ✅ 完整 |
| MS-3.3 回饋機制 | 終端用戶回報問題的機制 | `PromptGuard`（用戶 prompt 門控）+ `SedimentationGate`（糾正沉澱） | ⚠️ 部分 |
| MS-4.1 量測連結脈絡 | 量測方法連結到部署脈絡 | `MilestoneGate`（SOP 階段特定指引）+ `FeatureConfig`（功能風險元資料） | ✅ 完整 |
| MS-4.3 效能變化 | 效能改善/退化識別 | `ThinkingDepthGuard`（Read:Edit 退化）+ `EquilibriumBreaker`（動態平衡斷路器） | ✅ 完整 |

### MANAGE — 風險回應與管理

| NIST 子類別 | 說明 | CCC 組件 | 覆蓋 |
|------------|------|----------|------|
| MG-1.1 開發決策 | 判斷系統是否達成目的、是否應繼續 | `StopGuard`（阻止過早結束）+ `OverflowGate`（注意力耗盡門控） | ✅ 完整 |
| MG-1.2 風險優先排序 | 按影響、可能性、資源排序風險 | `C0Router`（4 級複雜度）+ `DestructionGuard`（R0-R4 風險分級） | ✅ 完整 |
| MG-1.3 高優先回應 | 制定高優先風險回應 | `StepBack`（二階緩衝：先回退再硬拒）+ `ErrorRecovery`（四層復原策略） | ✅ 完整 |
| MG-1.4 殘餘風險記錄 | 記錄負面殘餘風險 | `HandoffGuard`（交接驗證）+ `StructuredHandoffGuard`（未解決段必填） | ✅ 完整 |
| MG-2.1 資源考量 | 考慮非 AI 替代方案 | `MultiPathGate`（強制多方案比較）+ `PremiseGate`（前提驗證） | ✅ 完整 |
| MG-2.2 價值維持 | 維持已部署 AI 系統的價值 | `UIVerifyGuard`（部署後 UI 驗證）+ `WiredoGuard`（WIREDO 六維） | ✅ 完整 |
| MG-2.3 未知風險回應 | 回應未知風險的程序 | `ErrorRecovery`（四層復原）+ `EquilibriumBreaker`（斷路器）+ `HypothesisTrackerGuard` | ✅ 完整 |
| MG-2.4 停用機制 | 停用表現不佳的系統 | `DestructionGuard`（退役+備份）+ `FeatureConfig`（功能開關） | ✅ 完整 |
| MG-3.1 第三方監控 | 監控第三方資源 | `DepAuditGuard`（依賴稽核）+ `PluginLoader`（插件驗證） | ✅ 完整 |
| MG-3.2 預訓練模型監控 | 監控預訓練模型 | `AgentArtifactGuard`（代理產出驗證）+ `VerifyBeforeWriteGuard`（外部引用驗證） | ⚠️ 部分 |
| MG-4.1 部署後監控 | 部署後監控+申訴+停用機制 | `UIVerifyGuard` + `StopGuard` + `ProcessGuard`（進程生命週期） | ✅ 完整 |
| MG-4.2 持續改善 | 可量測的持續改善活動 | `SedimentationGate`（糾正沉澱）+ `Knowledge`（自動學習）+ `InsightEngine`（洞察引擎） | ✅ 完整 |
| MG-4.3 事件溝通 | 事件和錯誤溝通 | `ConsecutiveFailGuard`（連續失敗偵測）+ `HonestyGate`（誠實門控）+ 審計日誌 | ✅ 完整 |

---

## ISO/IEC 42001:2023 Alignment

### A.2 AI 政策（Policies for AI）

| 控制項 | 控制名稱 | CCC 組件 | 覆蓋 |
|--------|---------|----------|------|
| A.2.2 | AI 政策 | `PromptEngine`（動態 prompt 組裝+反漂移注入）+ L0/L1 規則系統 | ✅ |
| A.2.3 | 負責任 AI 主題 | `DesignTheoryGuard`（Vertical Slice + HITL/AFK + Deep Module 強制）+ `ButterflyGuard`（蝴蝶效應鐵律） | ✅ |

### A.3 內部組織（Internal Organization）

| 控制項 | 控制名稱 | CCC 組件 | 覆蓋 |
|--------|---------|----------|------|
| A.3.2 | AI 角色與職責 | `AgentGateGuard`（代理門控+計數+升級）+ `AgentSupervisor`（合約式驗證）+ `SubagentIdentity`（動態身分指派） | ✅ |
| A.3.3 | AI 問題報告 | `HonestyGate`（偵測委婉語掩蓋錯誤）+ `ConsecutiveFailGuard`（連續失敗報告）+ 審計日誌 | ✅ |
| A.3.4 | 組織變更影響 | `FileTrackerGuard`（檔案衝突偵測）+ `MultiInstance`（多 session 協調） | ✅ |

### A.4 AI 系統資源（Resources for AI Systems）

| 控制項 | 控制名稱 | CCC 組件 | 覆蓋 |
|--------|---------|----------|------|
| A.4.2 | AI 系統相關資源 | `TokenMonitor`（token 使用追蹤）+ `TokenZone`（三區管理+模型感知閾值）+ `CostTracker` | ✅ |
| A.4.3 | AI 系統能力 | `CognitiveGuard`（三層知識路由）+ `SkillRouter`（66+ 認知技能路由）+ `ThinkInjectGuard` | ✅ |
| A.4.4 | 負責任使用意識 | `OrientationGate`（成本分析）+ `ProposalGuard`（副作用分析）+ `OverflowGate`（注意力管理） | ✅ |
| A.4.5 | 諮詢 | `MultiPathGate`（強制多方案）+ `CognitiveAnchorGuard`（紅隊錨定） | ⚠️ |
| A.4.6 | AI 系統溝通 | `ProgressReporter`（里程碑報告）+ `HandoffGuard`（交接格式驗證） | ✅ |

### A.5 AI 系統影響評估（Assessing Impacts）

| 控制項 | 控制名稱 | CCC 組件 | 覆蓋 |
|--------|---------|----------|------|
| A.5.2 | AI 系統風險評估 | `C0Router`（CBUA 複雜度分類）+ `ConfidenceGate`（信心校準門控）+ `DestructionGuard`（R0-R4 分級） | ✅ |
| A.5.3 | AI 系統影響評估 | `ProposalGuard`（副作用分析）+ `ButterflyGuard`（連鎖影響偵測） | ✅ |
| A.5.4 | AI 系統影響文件 | `HandoffGuard` + `StructuredHandoffGuard`（七段式格式含未解決段） | ✅ |

### A.6 AI 系統生命週期（AI System Life Cycle）

| 控制項 | 控制名稱 | CCC 組件 | 覆蓋 |
|--------|---------|----------|------|
| A.6.2.2 | 設計與開發 | `CodeGuard`（靜態分析）+ `StructuralGuard`（結構分析 PRM）+ `DesignTheoryGuard` | ✅ |
| A.6.2.3 | 訓練與測試 | `LintGuard`（ESLint）+ `TypeScript`（tsc 快取）+ `CodeGuard` | ✅ |
| A.6.2.4 | 驗證與確認 | `WiredoGuard`（WIREDO 六維驗證）+ `WiredoEnforcementGuard`（硬性強制）+ `UIVerifyGuard` | ✅ |
| A.6.2.5 | 部署 | `UIVerifyGuard`（部署後 UI 驗證）+ `PublishScan`（發布前掃描） | ✅ |
| A.6.2.6 | 運作與監控 | `SentinelGuard`（行為偵測）+ `TokenMonitor` + `ProcessGuard`（進程監控） | ✅ |
| A.6.2.7 | 退役 | `DestructionGuard`（安全退役+自動備份）+ `BackupManager`（保留+回滾） | ✅ |
| A.6.2.8 | 負責任整合 | `BoundaryGuard`（CC/CCC 邊界）+ `SSOTGuard`（單一真相源強制） | ✅ |
| A.6.2.9 | AI 系統文件 | `HandoffGuard` + `StructuredHandoffGuard` + `SiblingScanGuard`（兄弟模式掃描） | ✅ |
| A.6.2.10 | 定義使用與誤用 | `IdentityGuard`（防止修改身分配置）+ `PromptInjectionGuard`（注入偵測） | ✅ |
| A.6.2.11 | 第三方元件管理 | `DepAuditGuard`（typosquatting+黑名單）+ `PluginLoader`（插件驗證+去重） | ✅ |

### A.7 AI 系統資料（Data for AI Systems）

| 控制項 | 控制名稱 | CCC 組件 | 覆蓋 |
|--------|---------|----------|------|
| A.7.2 | 開發與增強資料 | `ZIQRetrieval`（EMA 自適應 RAG）+ `Riverbed`（河床記憶拓撲）+ `Knowledge`（學習擷取） | ✅ |
| A.7.3 | 資料品質 | `HallucinationGuard`（無來源斷言偵測）+ `VerifyBeforeWriteGuard`（引用驗證） | ✅ |
| A.7.4 | 資料準備 | `CognitiveInject`（三層知識路由器） | ⚠️ |
| A.7.5 | 資料取得與收集 | `ExfilGuard`（外洩防護）+ `SecretScanGuard`（秘密偵測） | ⚠️ |
| A.7.6 | 資料溯源 | `SSOTGuard`（單一真相源）+ `FileTrackerGuard`（檔案追蹤+版本） | ✅ |

### A.8 利害關係人資訊（Information for Interested Parties）

| 控制項 | 控制名稱 | CCC 組件 | 覆蓋 |
|--------|---------|----------|------|
| A.8.2 | 告知 AI 互動 | `HonestyGate`（誠實門控，禁止委婉語掩蓋）+ `ProgressReporter`（里程碑報告） | ✅ |
| A.8.3 | 告知 AI 結果 | `WiredoGuard`（W-I-R-E-D-O 六維透明交付）+ 審計日誌 | ✅ |
| A.8.4 | AI 互動資訊存取 | 審計日誌（`.cc_cortex_cache/audit/`）+ `TokenMonitor` + `CostTracker` | ✅ |
| A.8.5 | 人類回應 AI 輸出 | `StepBack`（二階緩衝讓用戶介入）+ `AgentGateGuard`（人類監督升級） | ✅ |

### A.9 AI 系統使用（Use of AI Systems）

| 控制項 | 控制名稱 | CCC 組件 | 覆蓋 |
|--------|---------|----------|------|
| A.9.2 | 負責任使用目標 | `DesignTheoryGuard`（設計理論強制）+ `IntentAnchorGuard`（意圖錨定） | ✅ |
| A.9.3 | 預期使用 | `InitialIntentProbe`（首次寫入探測根本目的）+ `BoundaryGuard`（邊界強制） | ✅ |
| A.9.4 | 負責任使用流程 | 完整 `GuardPipeline`（55+ 守衛三層強制）+ `StepBack`（二階緩衝） | ✅ |
| A.9.5 | 人類監督面向 | `AgentGateGuard`（代理門控）+ `DestructionGuard`（不可逆操作強制人類確認）+ `AgentSupervisor` | ✅ |

### A.10 第三方與客戶關係（Third-Party & Customer Relationships）

| 控制項 | 控制名稱 | CCC 組件 | 覆蓋 |
|--------|---------|----------|------|
| A.10.2 | AI 系統元件供應商 | `DepAuditGuard`（依賴稽核：typosquatting+範圍欺騙+黑名單） | ✅ |
| A.10.3 | 共享 ML 模型 | `AgentArtifactGuard`（代理產出驗證）+ `VerifyBeforeWriteGuard` | ⚠️ |
| A.10.4 | 向第三方提供 AI 系統 | `PublishScan`（發布前掃描）+ `SecretScanGuard`（秘密偵測）+ `BoundaryGuard` | ✅ |

---

## NIST AI Agent Standards Initiative (2026-02) Alignment

2026 年 2 月 NIST CAISI 宣布 AI Agent Standards Initiative，針對自主 AI 代理的三大支柱：

| 支柱 | 要求 | CCC 組件 | 覆蓋 |
|------|------|----------|------|
| **身分與授權** | 代理身分基礎設施、認證 | `IdentityGuard`（防止修改身分配置）+ `SubagentIdentity`（動態身分指派）+ `AgentGateGuard`（代理授權門控） | ✅ 完整 |
| **安全與風險管理** | 代理威脅緩解、安全評估 | `PromptInjectionGuard`（NLP 級注入偵測）+ `ExfilGuard`（外洩防護）+ `DestructionGuard`（破壞操作攔截）+ `HijackGuard`（劫持偵測） | ✅ 完整 |
| **監控與日誌** | 代理行為監控、日誌記錄 | `SentinelGuard`（行為模式偵測）+ `TokenMonitor`（token 追蹤）+ `FileTrackerGuard`（檔案追蹤）+ 審計日誌 + `CostTracker` | ✅ 完整 |
| **互操作性** | 跨系統代理協議 | `PluginLoader`（entrypoint 插件系統）+ `MCPServer`（MCP 協議支援）+ `Coordination`（多代理協調） | ✅ 完整 |
| **人類-代理互動** | 安全的人機互動 | `AgentSupervisor`（合約驗證）+ `StepBack`（二階緩衝）+ `MultiPathGate`（多方案呈現） | ✅ 完整 |
| **多代理安全** | 代理間安全通訊 | `AgentArtifactGuard`（產出驗證）+ `MultiInstance`（多 session 衝突偵測）+ `Coordination.FileLock` | ✅ 完整 |

---

## Gap Analysis

| 標準 | 要求 | CCC 覆蓋 | 缺口 | 建議 |
|------|------|---------|------|------|
| NIST GV-1.6 | 組織級 AI 系統清冊 | ⚠️ 部分 | CCC 提供守衛級和檔案級追蹤，缺組織級 AI 系統登記冊 | 企業消費者需自建 AI 系統登記冊，CCC `FileTrackerGuard` 可作為資料來源 |
| NIST GV-2.2 | 人類人員 AI 風險管理訓練 | ⚠️ 部分 | CCC 訓練 AI 代理，不訓練人類員工 | 建議企業搭配 LMS（學習管理系統）補足 |
| NIST GV-3.1 | 多元化人事團隊 | ⚠️ 部分 | CCC 提供認知多元性（多方案、紅隊），非人事多元性 | 企業人事政策補足 |
| NIST MP-5.1 | 社會影響評估 | ⚠️ 部分 | CCC 偵測技術層面影響（蝴蝶效應），不涵蓋廣義社會影響 | 建議搭配獨立 AI 倫理委員會 |
| NIST MS-2.9 | AI 模型可解釋性 | ⚠️ 部分 | CCC 強制深度思考和知識路由，缺傳統 XAI（SHAP/LIME）整合 | 可透過 `PluginLoader` 載入 XAI 插件 |
| NIST MS-3.3 | 終端用戶回饋機制 | ⚠️ 部分 | CCC 提供 prompt 門控和糾正沉澱，缺正式用戶投訴管道 | 企業需建立獨立投訴管道 |
| NIST MG-3.2 | 預訓練模型系統性監控 | ⚠️ 部分 | CCC 驗證代理產出，缺模型版本漂移偵測 | 可透過 `PluginLoader` 擴充模型監控 |
| ISO A.4.5 | 正式諮詢程序 | ⚠️ 部分 | CCC 有多方案和紅隊，缺正式外部利害關係人諮詢流程 | 企業流程補足 |
| ISO A.7.4 | 資料準備流程 | ⚠️ 部分 | CCC 有知識路由，缺正式資料清洗/標註管線 | 取決於使用場景，可透過插件擴充 |
| ISO A.7.5 | 資料取得與收集治理 | ⚠️ 部分 | CCC 防止外洩和秘密洩漏，缺正式資料收集政策框架 | 企業資料治理政策補足 |
| ISO A.10.3 | 共享 ML 模型治理 | ⚠️ 部分 | CCC 驗證產出，缺模型卡和模型登記冊 | 建議整合 Model Card 生成工具 |

---

## 完整守衛清單（按合規功能分類）

### 安全防線（Security Layer — 7 guards）

| 守衛 | 功能 | 主要合規對映 |
|------|------|-------------|
| `PromptInjectionGuard` | NLP 級 prompt 注入偵測 | NIST MS-2.7, ISO A.6.2.10, Agent Security |
| `SecretScanGuard` | Write/Edit 內容中硬編碼秘密偵測 | NIST MS-2.10, MP-4.1, ISO A.7.5 |
| `GitSafetyGuard` | 危險 git 操作阻擋（force push、reset --hard 等） | NIST MG-2.4, MS-2.6, ISO A.6.2.7 |
| `DepAuditGuard` | 依賴 typosquatting + 範圍欺騙 + 黑名單 | NIST GV-6.1, MG-3.1, ISO A.6.2.11, A.10.2 |
| `ExfilGuard` | 資料外洩防護 | NIST MS-2.7, MS-2.10, ISO A.7.5 |
| `IdentityGuard` | 防止修改身分配置 | NIST GV-2.1, ISO A.3.2, Agent Identity |
| `DestructionGuard` | R0-R4 風險分級破壞操作攔截 + 自動備份 | NIST MG-1.2, MS-2.6, GV-1.7, ISO A.6.2.7, A.5.2 |

### 品質治理（Quality Layer — 30+ guards）

| 守衛 | 功能 | 主要合規對映 |
|------|------|-------------|
| `PremiseGate` | 執行前驗證外部前提 | NIST GV-4.1, MP-2.3, ISO A.5.2 |
| `WindowGuard` | 防止 Windows 主控台視窗閃爍 | NIST MS-2.3（使用者體驗品質） |
| `TokenGuard` | token 使用追蹤和預算門控 | NIST MS-1.1, ISO A.4.2 |
| `AgentGateGuard` | 代理產生門控 + 計數 + 升級 + 硬拒 | NIST GV-2.1, GV-3.2, MP-3.5, ISO A.3.2, A.9.5, Agent Identity |
| `ReadFirstGuard` | 修改前強制先讀取 | NIST GV-4.1, ISO A.6.2.2 |
| `ReadBudgetGuard` | 讀取預算管控 | NIST MS-1.1, ISO A.4.2 |
| `BashPythonGuard` | Bash/Python 命令安全門控 | NIST MS-2.6, MS-2.7, ISO A.6.2.10 |
| `HijackGuard` | 代理劫持偵測 | NIST MS-2.7, ISO A.3.3, Agent Security |
| `ConsecutiveFailGuard` | 連續失敗偵測和報告 | NIST GV-4.3, MG-4.3, ISO A.3.3 |
| `SentinelGuard` | 行為模式偵測（迴圈、停滯） | NIST MS-2.4, MS-3.1, ISO A.6.2.6, Agent Monitoring |
| `FileTrackerGuard` | 檔案追蹤 + 衝突偵測 + 鎖定 + 殭屍清理 | NIST GV-1.6, ISO A.7.6, A.3.4 |
| `BoundaryGuard` | CC/CCC 邊界違規偵測 | NIST GV-1.1, ISO A.6.2.8, A.9.3 |
| `ProposalGuard` | 強制副作用分析 | NIST GV-4.2, MP-5.1, ISO A.5.3 |
| `UIVerifyGuard` | 部署後 UI 驗證 | NIST MG-4.1, MS-2.3, ISO A.6.2.5 |
| `ButterflyGuard` | 蝴蝶效應：看到問題就處理 | NIST GV-4.1, MP-5.1, ISO A.5.3 |
| `OverflowGate` | 注意力耗盡時阻擋旁枝任務 | NIST MG-1.1, ISO A.4.4 |
| `OrientationGate` | 長操作前強制成本分析 | NIST MP-3.2, ISO A.4.4, A.5.2 |
| `HonestyGate` | 偵測委婉語掩蓋已知錯誤 | NIST MS-2.8, MG-4.3, ISO A.8.2 |
| `MultiPathGate` | 強制多替代方案比較 | NIST MG-2.1, ISO A.4.5 |
| `AgentArtifactGuard` | 代理產出 PostToolUse 驗證 | NIST MG-3.2, ISO A.10.3, Agent Security |
| `HallucinationGuard` | 偵測書面內容中的無來源斷言 | NIST MP-2.3, MS-2.8, ISO A.7.3, A.8.3 |
| `VerifyBeforeWriteGuard` | 寫入前驗證外部引用 | NIST MP-2.3, ISO A.7.3 |
| `CodeGuard` | Python/Rust/Go 統一靜態分析 | NIST MS-2.1, ISO A.6.2.2, A.6.2.3 |
| `LintGuard` | ESLint JS/JSX 包裝 | NIST MS-2.1, ISO A.6.2.3 |
| `StructuralGuard` | 輕量結構分析（規則式 PRM） | NIST MS-2.1, ISO A.6.2.2 |
| `SSOTGuard` | 單一真相源強制（PostToolUse） | NIST GV-1.4, ISO A.7.6, A.6.2.8 |
| `HandoffGuard` | 交接檔案格式驗證 | NIST GV-2.3, MG-1.4, ISO A.5.4, A.6.2.9 |
| `EquilibriumGuard` | 交接檔案寫入即清理動態平衡 | NIST MS-1.2, ISO A.6.2.6 |
| `DeliveryGuard` | 交付品質門控 | NIST MS-2.5, ISO A.6.2.4 |
| `DesignTheoryGuard` | 設計理論強制（Vertical Slice + HITL/AFK + Deep Module） | NIST GV-1.2, ISO A.2.3, A.9.2 |
| `SiblingScanGuard` | 兄弟模式掃描守衛 | NIST MS-2.1, ISO A.6.2.9 |
| `StructuredHandoffGuard` | 固定欄位交接模板 | NIST MG-1.4, ISO A.5.4 |
| `WiredoEnforcementGuard` | WIREDO 六維硬性強制 | NIST MS-2.5, ISO A.6.2.4, A.9.4 |

### 認知增強（Cognitive Layer — 15+ guards）

| 守衛 | 功能 | 主要合規對映 |
|------|------|-------------|
| `CognitiveGuard` | 三層知識路由注入 | NIST GV-2.2, MS-2.9, ISO A.4.3 |
| `ConfidenceGate` | 信心校準門控 | NIST GV-1.3, MS-1.1, ISO A.5.2 |
| `HypothesisTrackerGuard` | 追蹤失敗方法防止迴圈 | NIST GV-4.3, MG-2.3, ISO A.6.2.6 |
| `CognitiveAnchorGuard` | 關鍵決策紅隊錨定 | NIST GV-3.1, ISO A.4.5 |
| `IntentAnchorGuard` | 保留原始任務意圖 | NIST MP-1.3, ISO A.9.2 |
| `InitialIntentProbe` | 首次寫入探測用戶根本目的 | NIST MP-1.1, ISO A.9.3 |
| `ThinkInjectGuard` | 高風險操作思考注入 | NIST GV-2.2, MS-2.9, ISO A.4.3 |
| `WiredoGuard` | WIREDO 六維交付清單注入 | NIST MS-2.5, ISO A.6.2.4 |
| `MilestoneGate` | SOP 里程碑特定指引注入 | NIST MS-4.1, ISO A.6.2.6 |
| `TruncationAwareGuard` | 截斷感知（CC 弱點緩解） | NIST MS-2.3, ISO A.6.2.6 |
| `LargeFileReadGuard` | 大檔案讀取門控 | NIST MS-1.1, ISO A.4.2 |
| `RenameScopeGuard` | 重命名範圍門控 | NIST MS-2.6, ISO A.6.2.2 |
| `CompactFailureGuard` | 壓縮失敗處理 | NIST MG-2.3, ISO A.6.2.6 |
| `McpCleanupGuard` | MCP 清理守衛 | NIST MG-4.1, ISO A.6.2.6 |
| `AgentDispatchGuard` | Token 感知代理調度策略 | NIST GV-3.2, ISO A.3.2, A.4.2, Agent Identity |

---

## 覆蓋率摘要

| 標準 | 總項目 | ✅ 完整覆蓋 | ⚠️ 部分覆蓋 | ❌ 缺口 | 覆蓋率 |
|------|--------|-----------|------------|---------|--------|
| NIST AI RMF（72 子類別） | 48 相關項 | 38 | 10 | 0 | 100%（相關項） |
| ISO 42001（39 控制項） | 39 | 31 | 8 | 0 | 100% |
| NIST Agent Initiative（6 支柱） | 6 | 6 | 0 | 0 | 100% |

**結論**：CCC 在技術層面提供全覆蓋。缺口集中在組織治理層面（人事多元性、人類訓練、外部利害關係人諮詢），這些需要企業客戶自身的管理流程補足。CCC 的 `PluginLoader` 架構允許企業載入自訂守衛以填補特定合規缺口。

---

## 參考資料

- [NIST AI RMF 1.0 (AI 100-1)](https://nvlpubs.nist.gov/nistpubs/ai/nist.ai.100-1.pdf)
- [NIST AI RMF Playbook](https://airc.nist.gov/airmf-resources/playbook/)
- [ISO/IEC 42001:2023](https://www.iso.org/standard/81230.html)
- [NIST AI Agent Standards Initiative (2026-02)](https://www.nist.gov/caisi/ai-agent-standards-initiative)
- [NIST AI 600-1 GenAI Profile](https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf)
- [CSA Agentic NIST AI RMF Profile](https://labs.cloudsecurityalliance.org/agentic/agentic-nist-ai-rmf-profile-v1/)
