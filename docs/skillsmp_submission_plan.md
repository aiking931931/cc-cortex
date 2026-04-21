# SkillsMP Submission Plan — Top 10 Concinno Guards

> 交接表 F2 (⬜) — 挑 10 個高價值 guards 包裝成 Claude skill format 上架
> SkillsMP (Claude skills marketplace)。本文是 submission prep draft，
> 非上架本身（上架需外部平台帳號 + API/UI 流程，留下一 session 動手）。
>
> Source: `src/concinno/guards/registry.py` 63 guards on default pipeline。

## Top 10 picks (rationale)

每條 pick 選 criteria：① 任一 Claude Code user 都受益（非 ai-king 特化）
② 獨立可用（不需整個 Concinno 依賴）③ 效果可一句話講清

| # | Guard | Category | Value proposition |
|---|---|---|---|
| 1 | **DestructionGuard** (R0-R4) | PreToolUse / R0-R4 硬擋 | 攔 `rm -rf /` / `git reset --hard origin/main` / `DROP TABLE` / `force push main`。四級風險 gating + per-op escape env flag。 |
| 2 | **SecretScanGuard** | PreToolUse Write / Edit | Basename + word-boundary 正則：`.env` / `credentials.*` / `id_rsa` / `.pypirc`. Test-dir / pytest-prefix 白名單避假陽性。 |
| 3 | **ButterflyGuard** | PostToolUse observe | 偵測 pre-existing 問題 + 本次改動引入的新問題，Stop 前強制處理或寫入交接「未解決」段。 |
| 4 | **ConsecutiveFailGuard** | PreToolUse signal | 同 op 連續 2 次失敗 → 強制觸發 RAG（grep memory / WebFetch docs）。連續 3 次 → 停手 (三敗鐵律)。 |
| 5 | **HallucinationGuard** | PostToolUse scan | 寫入內容含具體斷言（URL / 版本號 / API 名）但 session 無 Read/Grep/Bash 證據 → stderr warn。 |
| 6 | **PremiseGate** (Mode 1 + 2) | PreToolUse | Mode 1：比賽/需求條件未讀原始文件不開工。Mode 2：引用平台限制（CC L1-L8 / API version）前必 WebFetch 驗。 |
| 7 | **VerifyBeforeWriteGuard** | PreToolUse Write | 寫入引用外部檔案/API 前必先 Read 該檔 / 實測該 API。防「猜前先查」違反。 |
| 8 | **WiredoGuard** | PostToolUse Stop | 交付資產前注入 WIREDO 六維清單（Wired/Inherited/Responsive/Extensible/Defended/Observable）。D 維強制功能驗證 UI → 截圖。 |
| 9 | **BashDryRunRewriter** | PreToolUse rewrite | Input rewriting, ALLOW-only：`rm -rf X` → `rm -rf --dry-run X` 先預演。不 deny，降副作用到 safe-preview。 |
| 10 | **HandoffGuard** | Stop hook | Session 結束 >20min 或 ≥3 file 變動而無交接 file 變動 → stderr warn + 給 minimal handoff template。 |

## Skill wrapping pattern

每個 guard 包裝成 user-invocable skill (Claude skills format)：

```
.claude/skills/<name>/
├── SKILL.md          # Frontmatter: name, description, trigger keywords
├── hook_template.md  # 如何 wire 到 Claude Code settings.json
└── examples.md       # 實際 deny / allow / warn 案例
```

SKILL.md frontmatter 範本：

```yaml
---
name: concinno-destruction-guard
description: Block accidental rm -rf / git reset --hard / DROP TABLE before
  execution. R0-R4 risk gating with per-op escape flags.
trigger_keywords: [rm -rf, reset --hard, force push, drop table, delete]
category: safety
license: Apache-2.0
upstream: https://github.com/aiking931931/concinno
version: pinned-to-concinno-2.10.5
---
```

## Submission checklist (per guard)

- [ ] Extract guard Python class → standalone module (依賴 base.py +
  guard 自己，零 Concinno deep imports)
- [ ] Write SKILL.md + hook_template.md + examples.md
- [ ] Test in clean venv：`pip install concinno && claude --install-skill`
- [ ] Screenshot (D 維 WIREDO) — guard trigger case 成功擋下示例
- [ ] License / attribution：Apache-2.0, link upstream concinno PyPI
- [ ] Version pin：`concinno>=2.10.5` (post-red-team-fix stable)

## 不可抽離的 (留作 Concinno-internal)

以下 guards 太依賴 Concinno cognitive 層（CBUA / Cigito / FieldRead）或 ai-king 特殊 layout，不適合單獨 skill 包裝：

- CbuaPipelineGuard (22-stage enforcement，需整個 CBUA 六定律 context)
- CognitiveGuard / ConfidenceGate / HypothesisTrackerGuard (cognitive 層)
- IntentAnchorGuard / InitialIntentProbe (要整個 intent pool)
- ConventionGuard (ai-king 特化命名規則)
- RedteamSpawnGuard (紅藍隊 SOP 整包)

## 下 session P0 動手步驟

1. 研究 SkillsMP 上架 API（ClawHub / Claude Skills Marketplace 官方）
2. `projects/concinno/skillsmp/` dir 建 10 個 skill wrapping
3. pytest 每個 skill 在 clean venv 可獨立跑
4. Batch 上架 + 標記上架進度（CHANGELOG [Unreleased]）
5. 2.11.0 bump when first 10 upped

## Deferred / 留給後續

- F4 AgentBeats competition Safety track — blocked on benchmark infra
- F5 Guard Hub (社群貢獻) — 架構大 scope，先看 SkillsMP 接收
- F6 Workspace Convention Engine — 架構大 scope

## 參考

- Concinno guard pipeline: [src/concinno/guards/registry.py](../src/concinno/guards/registry.py)
- Guard base class: [src/concinno/guards/base.py](../src/concinno/guards/base.py)
- Existing skills: `src/concinno/skills/public/{agent,browser,windows,general-mode,competition-mode}`
