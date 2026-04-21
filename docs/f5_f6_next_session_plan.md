# F5 + F6 下 session 動手 plan（2026-04-21 user directive）

> 交接 next_step F5/F6 的具體做事單。原本都是「blocked on external」
> 被 driven 回「自己挑值得的先裝」。不等外部回饋。

## F5 — Guard Hub 重定義（user 糾正 2026-04-21）

### 舊定義（廢棄）

> 「等 SkillsMP 接收回饋再決定方向」

**邏輯錯**：concinno 尚未推廣開源，沒人用 → 等不到回饋。等於永遠不動。

### 新定義

**邏輯好 + 實用 + benchmark/competition 用得到的 guards，全先裝進 Concinno ship default pipeline，不等外部回饋。**

不需要自建 marketplace / web infra / 第三方 submission — 由主 repo 維護者（AI King）判斷哪些有價值直接加進
[src/concinno/guards/registry.py](../src/concinno/guards/registry.py)
`create_default_pipeline`。

### 動手步驟（下 session P0）

1. **Audit 現有 63 guards**：逐條評估 value density（是否真 save user from bugs）+ ship cost（false positive rate）。列 KILL / DEMOTE / KEEP 清單。
2. **新 guards 候選**（benchmark / competition 向）：
   - `DeterministicReproGuard` — benchmark 跑分需可重現。攔 non-deterministic API call（`random.*` without seed / `time.time()` in test fixture / network call to non-mocked endpoint）。
   - `TokenEfficientGuard` — competition 多半 token cost 有限。stderr warn 當 agent prompt 單 turn >50k 或 subagent spawn brief >20k（Goodhart 警戒：不硬 block，signal only）。
   - `BenchmarkSetupGuard` — 偵測常見 benchmark harness（GAIA / AgentBench / OSWorld / HumanEval / MMLU / BEIR），自動 inject 對應 SOP reminder（如 GAIA 要 log token cost / OSWorld 要 screenshot proof）。
   - `SeedPropagationGuard` — multi-seed ablation experiments，偵測 user 跑一次但未 set `random_state` / `numpy.random.seed` / `torch.manual_seed`，stderr warn「seed not set, reproducibility lost」。
   - `ResultFileGuard` — ablation / benchmark 跑分 output 必須 commit 進 artifacts dir（非 `/tmp/`）。偵測 `json.dump(result, open('/tmp/...', 'w'))` → warn「result goes to tmp, likely lost on reboot」。
3. **共通 safety / quality guards**（任何 user 受益，非 ai-king 特化）：
   - `ImportCycleGuard` — pytest collection fail 常見根因是 import cycle。PreToolUse Write/Edit Python 檔偵測 `from X import Y` 和 import graph 形成循環 → warn。
   - `FunctionLengthGuard` — 單 function >100 行 warn（signal only）。很多 legacy refactor 的起點。
   - `MagicNumberGuard` — 偵測 hardcoded constants（`sleep(60)` / `timeout=30`）且出現 ≥3 次 → suggest extract to named constant。
4. **實作**（每個 guard 約 50-100 行 Python + 對應 test）：
   - 沿用 `concinno.guards.base.BaseGuard` pattern
   - 加到 `guards/registry.py` `create_default_pipeline`
   - 加 SKILL.md 到 `skills/public/<guard-name>/` 給 SkillsMP scraper
5. **Bump 2.12.0 minor** — 每 5-10 個 new guard 打一次 minor（避免每個 guard 一次 minor 過碎）。按 release coord 鐵律 #6：master >30 commit 未 PyPI 才 bump minor。

### 不該做的反模式

- ❌ 自建 web marketplace / web server / DB — F5 舊定義已廢
- ❌ 接受第三方 PR guard code without 紅藍 review — 安全風險太高
- ❌ 為了「讓 guard 多」而加低價值 guard — 維持 signal-to-noise ratio 才是重點

---

## F6 — Workspace Convention Engine

### 定義

規範 AI 建檔的「放哪 / 叫什麼名字」，把 `ConventionGuard` 的 ai-king 特化規則抽象成 generic engine + config DSL，每個 Claude Code user 可配置自己的 workspace convention。

### 為何重要

AI 開始亂丟檔 → 幾週後找不到東西，是 Claude Code user 最常見頭痛。既有 handoff / planning / skills / kb 多層資料夾 hierarchy 只有維護人知道，新 AI session 接手全靠運氣。

### 動手步驟（下 session P0）

1. **抽象既有 ConventionGuard**：
   - 讀 [src/concinno/guards/convention_guard.py](../src/concinno/guards/convention_guard.py)
   - 分離 ai-king 特化規則（`_AI_BRAIN/05_Planning/` 等）到 config
   - 保留 guard 本體為 generic engine
2. **Config DSL 設計**（`concinno.config.convention` 或類似）：
   ```yaml
   # .concinno/convention.yaml
   rules:
     - pattern: "handoff_*.md"
       must_be_in: "_AI_BRAIN/06_Handoffs/<project>/"
       reason: "handoff files grouped by project"
     - pattern: "交接_*.md"
       must_be_in: "_AI_BRAIN/06_Handoffs/<project>/"
       reason: "Chinese handoff synonym of handoff_*.md"
     - pattern: "*_plan.md | *_planning.md"
       must_be_in: "_AI_BRAIN/05_Planning/"
     - pattern: "feedback_*.md"
       must_be_in: "$MEMORY_DIR/"
       reason: "auto-memory feedback files"
   placement_suggestions:
     - path_contains: "ablation"
       suggest_dir: "benchmarks/<scenario>/"
     - ext: ".png | .jpg | .svg"
       suggest_dir: "assets/images/"
   ```
3. **Engine 行為**：
   - PreToolUse Write/Edit on new file → consult config，若 path 違規 → warn + 建議正確 path
   - 不 hard block（signal-only），留 user 最後決定權
   - `concinno convention check` CLI — 掃 workspace 現有檔 report 違規
   - `concinno convention suggest <filename>` — 給定檔名建議放哪
4. **遷移 ai-king 既有特化**：
   - Ship default `.concinno/convention.example.yaml`（AI King 用的規則 as example）
   - `concinno init` 可選 `--convention=aiking` 一鍵套用 example
5. **Tests**：
   - unit: config parse / pattern match / placement suggest
   - integration: PreToolUse Write hook 在違規 path 時 warn
6. **Bump 2.13.0 minor** — F6 是 user-visible feature，minor bump 合理。

### 設計 edge cases

- Pattern conflict — 同檔名 match 多條 rule，取最 specific（path depth 最深 / pattern 最少 wildcard）
- Missing convention file — default to permissive（不 warn）+ `concinno convention init` 引導
- Cross-platform path — Windows `\` vs POSIX `/` 一律 normalize

### 不該做的反模式

- ❌ Hard block on violation — user 偶爾要 exception，signal-only 是正解
- ❌ 過度 opinionated rules — engine 是 framework，規則是 user config
- ❌ 重寫既有 `ConventionGuard` — 抽象化，不是砍掉重做

---

## 下 session 動手順序

1. **F5 Audit（~30 min）**：讀 registry.py + 列 KILL/DEMOTE/KEEP table
2. **F5 new guards（~3-4 hr）**：實作 3-5 個新 guards + tests
3. **F5 Bump 2.12.0**：ship + AskUser publish
4. **F6 Engine 抽象（~2 hr）**：ConventionGuard refactor + config DSL
5. **F6 CLI + tests（~2 hr）**：`concinno convention check/suggest` + tests
6. **F6 Bump 2.13.0**：ship + AskUser publish

Total 估 1-1.5 個 session（按 full mode 不分 session 可能一個 session 到底）。

## 參考指針

- 現有 guards: [src/concinno/guards/registry.py](../src/concinno/guards/registry.py)
- Guard base class: [src/concinno/guards/base.py](../src/concinno/guards/base.py)
- 既有 ConventionGuard: [src/concinno/guards/convention_guard.py](../src/concinno/guards/convention_guard.py)
- SkillsMP submission plan: [skillsmp_submission_plan.md](skillsmp_submission_plan.md)
- Release coordination: [../RELEASE_COORDINATION.md](../RELEASE_COORDINATION.md)
