<!-- concinno-official-rule: do-not-edit -->

# WIREDO 交付驗證清單（L1 按需載入）

**switch**: `wiredo` — 查 `~/.rules/switches.md#11`。`/wiredo off` → 跳過六維強制檢查，tsc/lint/D 維視覺驗證不硬擋。

交付任何資產前載入此檔。`/wiredo on|off` 控制。

## 六維定義

| 維度 | 全名 | 要求 |
|------|------|------|
| **W** | Wired（接線） | 與系統正確連接，import/export/路由都通 |
| **I** | Inherited（母版） | 遵循母版/架構約定，不自創標準 |
| **R** | Responsive（響應） | 跨裝置 + 性能達標 |
| **E** | Extensible（可配置） | 可擴展，不硬編碼 |
| **D** | Defended（驗證） | **功能驗證 = 跑得起來做到該做的事** |
| **O** | Observable（可觀測） | 有日誌/監控/錯誤追蹤 |

## D 維度鐵律（最常被違反）

- **D = 功能驗證**，不是 tsc 通過、不是 lint 乾淨、不是「應該可以」
- **UI/前端改動 → 必須視覺驗證（截圖）**，只有純後端或無瀏覽器才能跳過
- 截圖流程：`node scripts/tools/psyche-screenshot.js`（Playwright headless）→ 存 `screenshots/verify/`
- 桌面版（≥1024px）+ 手機版（≤768px）雙版本
- 無法功能驗證 → ⏸ 延遲到里程碑再驗，**不假驗**
- WiredoGuard 自動偵測資產類型注入清單

## 七類型資產

每類資產的 WIREDO 權重不同，WiredoGuard 根據檔案類型自動注入對應清單。
