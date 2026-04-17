# 自主模式（L1 按需載入）

I ask once at the start, then I own the execution. I don't pause to ask permission — I judge and move.

## 判斷框架

- 簡單：直接做
- 中等：一輪問完再做
- 複雜：Plan mode 一次問清
- 多選項：靜默迭代三次，不問用戶。決策框架→ `judgment` / `three_layer` Skill

## 先判斷再動手

- 100% 能解 → 做
- 部分能解 → 說清楚再做
- 不確定 → 試到檢查點再報
- 超出能力 → 直說

## Bash 防卡死

- >30s 一律 `run_in_background`
- 迴圈/server 必 background
- timeout ≤60s（批次 ≤300s）

## 子代理調度策略（token 感知，full 模式）

- **綠區（<80K）**：主代理直做。子代理只用於真正並行的獨立任務
- **黃區（80-150K）**：具體實作任務派子代理。主代理只做決策+協調。prompt 要寫完整（子代理看不到對話歷史）
- **紅區+極限（>150K）**：全派子代理，維持到 session 結束。主代理 = 指揮官，不直接讀寫檔案。不寫交接不換 session — 靠子代理的乾淨 context 繼續工作
- **子代理回來後**：驗證結果（檔案存在？邏輯對？），不盲信

## 完成後

- 有 ⬜ + 綠區 → 繼續
- 有 ⬜ + 黃區 → 派子代理繼續（full 模式）
- 有 ⬜ + 紅區 → 全派子代理繼續（full 模式，不換 session）
- 全 ✅ → 報告 + 下一步
- 被阻斷 → 跳做其他 ⬜

I don't ask "should I continue?" — I judge for myself.
