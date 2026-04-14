# 指令索引（L1 按需載入）

指令在 `.claude/skills/<名>/SKILL.md`，用 `/名` 觸發。`交接 <專案>` 為自然語言觸發。

⛔ **創建/重寫 KB Skill 時**：先讀 `.claude/skills/SKILL_TEMPLATE.md` → 按模板結構寫。三層架構（L1 frontmatter / L2 SKILL.md ≤50行 / L3 主題檔按需讀取）+ 氣態語言（第一人稱信念）+ 固態語言（You MUST 硬性要求）= 必備三件套。缺任何一個不算完成。

| 指令 | 說明 |
|------|------|
| `/handoff` | 交接模式（省token/跑階段/跑完整） |
| `/hook` | Hook 控制 |
| `/mode` | 工作模式切換 |
| `/evolve` | 研究升級 |
| `/tidy` | 全面清理 |
| `/schedule` | 排程管理 |
| `/ps` | 進程狀態 |
| `/ux` | 成癮 UX 控制 |
| `/wiredo` | WIREDO 交付清單開關（預設開） |
| `/status` | 全局狀態儀表板 |
| `/sync` | Git 同步模式（pass 直接推/review 開 PR） |
| `/collab` | 多機協作分支模式（split 各開/same 同branch） |
| `/think` | 需求挑戰（Pipeline Step 1） |
| `/prd` | PRD 生成（Pipeline Step 2） |
| `/tdd` | TDD 紅綠循環（Pipeline Step 3） |
| `/review` | Staff Engineer 二階段審查（Pipeline Step 4） |
| `/qa` | Diff-aware QA（Pipeline Step 5） |
| `/ship` | 非互動式發布（Pipeline Step 6） |
| `/backup` | 備份管理（建立/回滾/清理/狀態） |
| `/three_layer` | 三層思考（L1根因→L2甜蜜點→L3策略） |
| `/first_principles` | 第一性原理分析 |
| `/prompt_select` | 思考策略選擇器（CoT/ToT/Step-Back） |
| `/debug_loop` | 結構化除錯（觀察→假設→測試→縮小） |
| `/decision_journal` | 決策日誌記錄 |
| `/pdca` | PDCA 執行循環 |
| `/judgment` | 判斷力（元認知+不確定性+因果） |
| `/awareness` | 覺察與自癒（注意力防禦+降級恢復） |
| `/learning_loop` | 學習循環（糾正→提煉→驗證→自動化） |
| `/locale` | 語言管理（設定/新增語言包） |
| `交接 <專案>` | 讀→摘要→按待辦做 |
| `交接 進化` | 上述+全局審查 5 步 |
