---
name: agent
description: Munio 統一 agent loop — GUI/Web 任務的標準執行循環。不分日常/比賽模式，一個版本到處用。桌面用 windows Skill，Web 用 browser Skill，自動選擇 → 規劃 → 批次執行 → 驗證 → 修正。觸發詞：agent、GUI 任務、自動化任務、桌面任務、網頁任務、OSWorld、WebArena、benchmark。
triggers:
  - agent
  - GUI 任務
  - 自動化任務
  - 桌面任務
  - 網頁任務
  - OSWorld
  - WebArena
  - benchmark
user-invocable: true
---

# Munio Agent Loop（統一版，到處用）

> 一個 agent 走天下。不分日常/比賽/產品模式。接到任務 → 判斷桌面 or Web → 選 tool → 規劃 → 執行 → 驗證 → 修正。同一個循環跑日常操作、跑 WebArena、跑 OSWorld。

> **You MUST** 不問用戶「這是比賽還是日常」— 永遠同一個流程。
> **You MUST** 先 snapshot/observe 再動手 — 不盲點盲打。
> **You MUST** 每步驗證結果 — `dom_diff` 或 `verify_action`。失敗 retry ≤3 次再換策略。
> **You MUST** batch 規劃（UFO3 pattern）— 一次規劃 3-5 步，減少 LLM 往返。

## 決策樹

```text
收到任務
  ├─ 操作目標是瀏覽器/網頁？
  │   └─ YES → import browser as b
  │       b.launch() → b.goto(url) → 走 Web Agent Loop
  ├─ 操作目標是桌面 app / 系統設定？
  │   └─ YES → import windows as w
  │       w.snapshot() 或 w.app(name) → 走 Desktop Agent Loop
  └─ 兩者都要？
      └─ 各 import 各的，交替使用
```

## Agent Loop（兩個 Skill 共用同一循環）

```text
1. OBSERVE（感知）
   Web:     snap = b.snapshot()     # ARIA tree
   Desktop: snap = w.snapshot()     # UIA tree + labels
   通用:    截圖餵多模態看一眼（大方向判斷）

2. PLAN（規劃 3-5 步）
   看 snap 的元素 → 判斷哪些該 click/fill/select
   寫成 batch list：[("click", sel), ("fill", sel, val), ...]
   
3. ACT（批次執行）
   Web:     result = b.batch_actions(steps, verify_after=..., screenshot_after=...)
   Desktop: 逐步 w.click(label=N) / w.type_text(...)

4. VERIFY（驗證）
   Web:     changes = b.dom_diff(snap_before, snap_after)
            ok = b.verify_action("expected_selector")
   Desktop: snap_after = w.snapshot()
            比對 before/after 元素差異
   
5. RETRY or NEXT（修正或下一步）
   verified = True  → 回到 1，處理下一個子目標
   verified = False → 重試（≤3 次），換策略（selector 改法 / 截圖多模態重新判斷）
   3 次失敗          → 截圖 + 報告卡住，不假裝成功
```

## 不分模式的理由

```text
日常：「幫我開 Chrome 登入 Gmail 回信」
     → OBSERVE(snapshot) → PLAN(goto gmail → fill → click send) → ACT → VERIFY

比賽：「WebArena task: 在 Reddit 搜尋 X 然後發帖」
     → OBSERVE(snapshot) → PLAN(goto reddit → fill search → click post) → ACT → VERIFY

流程 100% 一樣。唯一差別：
  日常 → 結果回報用戶
  比賽 → 結果寫入 evaluation JSON
```

## 工具速查

| 需要 | 桌面 | Web |
|---|---|---|
| 截圖 | `w.screenshot()` / `w.screenshot_window(hwnd)` | `b.screenshot()` |
| 點擊 | `w.click(label=N)` / `w.uia_click(name=...)` | `b.click("selector")` |
| 打字 | `w.type_text("...", ime_safe=True)` | `b.fill("selector", "...")` |
| 結構感知 | `w.snapshot()` (UIA tree) | `b.snapshot()` (ARIA tree) |
| 差異比對 | 比對 before/after snapshot dict | `b.dom_diff(before, after)` |
| 驗證 | 重新 snapshot 看 label 變化 | `b.verify_action("selector")` |
| 批次 | 逐步呼叫 | `b.batch_actions([...])` |
| 背景 | `w.run_in_hidden_desktop(cmd)` | headless=True（原生） |
| JS eval | N/A | `b.eval_js("...")` |

## G6 全賽道提交（Munio 通用型）

同一個 agent loop 直接打：

| 賽道 | 類型 | tool | 入場方式 |
|---|---|---|---|
| **WebArena** | Web 自動化 | browser | `b.goto(eval_url)` + agent loop |
| **OSWorld** | 桌面 GUI | windows | `w.app()` + agent loop |
| **WindowsAgentArena** | Windows tasks | windows | 同上 |
| **BrowserBench** | 瀏覽器 | browser | 同 WebArena |
| **AgentBench GUI** | 混合 | 兩者 | 自動判斷 |
| **CyberGym** | 安全 | 兩者 | CTF 風格 |

## GAIA Benchmark Runner

`gaia_runner.py`（同目錄）— 完整 GAIA benchmark 自動跑分。

```bash
# validation（有答案，驗分數）
python ~/.claude/skills/agent/gaia_runner.py --validate 20
python ~/.claude/skills/agent/gaia_runner.py --validate all

# test（301 題提交用）
python ~/.claude/skills/agent/gaia_runner.py --test
```

**已整合能力**：
- Sonnet 推理 + `FINAL ANSWER:` extraction
- 多輪 web search（Google + 點進頁面讀）
- YouTube transcript API（`youtube-transcript-api`）
- Excel reader（`openpyxl`）
- PDF reader（`PyPDF2`）
- 答案格式化（去單位/逗號/前綴）

**實測 20 題 validation = 40%**（SOTA GPT-5 Mini ~45%）。
HF 帳號 `aiking931931`，提交名 **Munio by AI King**。

## 反模式

1. ❌ 問用戶「這是日常還是比賽」— 永遠同一個流程
2. ❌ 盲打（不 snapshot 就 click）— 必先感知
3. ❌ 一步一問 LLM — batch 3-5 步減往返
4. ❌ 不驗證就繼續 — 每步 verify
5. ❌ 3 次失敗硬撐 — 截圖報告，不假裝成功
6. ❌ 切換模式（competition-mode Skill 概念廢棄）— 一個版本
