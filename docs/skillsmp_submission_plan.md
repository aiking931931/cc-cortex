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

## 上架機制（2026-04-21 WebSearch unblocked）

**SkillsMP** (`skillsmp.com`) 是 **GitHub-scraped community marketplace**，NOT
Anthropic 官方平台。66,541+ skills (2026-01)，filter: min 2 GitHub stars。
**上架 = 公開 GitHub repo 加 SKILL.md → scraper 自動抓**（無 submission form
無 API account）。Concinno 已在 GitHub `aiking931931/concinno` + 已有 5
skills 在 `src/concinno/skills/public/{agent,browser,windows,general-mode,competition-mode}`。

別的 marketplace（如 `claudeskills.info`）有 manual submission form + review
流程，但 SkillsMP 自動 path 是優先。

### Submission 真步驟（簡化）

1. `src/concinno/skills/public/<guard-name>/SKILL.md` for each of 10 picks
2. `pyproject.toml [tool.hatch.build.targets.{sdist,wheel}.force-include]`
   加新路徑（避 git-ignore exclusion）— pattern 跟既有 5 skill `_cognitive`
   的 force-include 一樣
3. ship 2.11.0 — bump + CHANGELOG + build + twine check + tag + push +
   `twine upload`（user `go publish concinno 2.11.0` 字串授權）
4. SkillsMP scraper 在 next sync 自動抓（時間 unspecified，依其後台）
5. GitHub stars ≥2 是 filter；若 stars < 2 不顯示
6. （optional）`claudeskills.info` 平台手動 submit form 加快曝光

## SKILL.md 範本（複製即用）

```markdown
---
name: concinno-destruction-guard
description: Block accidental rm -rf, git reset --hard, force push main,
  DROP TABLE before execution. R0-R4 risk gating with per-op escape flags.
trigger_keywords: [rm -rf, reset --hard, force push, drop table, delete]
category: safety
license: Apache-2.0
upstream: https://github.com/aiking931931/concinno
version: pinned-to-concinno-2.10.5
---

# DestructionGuard — Pre-execution irreversible-op block

## What it does

Hooks into Claude Code's PreToolUse pipeline; intercepts dangerous Bash
patterns:

- **R4 catastrophic**: `rm -rf /`, `dd if=/dev/zero of=/dev/sda`, ...
- **R3 high-impact**: `git reset --hard origin/main`, `DROP DATABASE`,
  `rm -rf <large dir>`, ...
- **R2 partial-loss**: `git push --force` to main/master, ...
- **R1 reversible-with-effort**: `git checkout -- .`, `mv` to existing
  path, ...
- **R0 trivial**: passes through.

Each tier requires either a per-op escape env flag (audit-visible) or
explicit AskUser confirmation, depending on risk level.

## Install

```bash
pip install concinno
claude --install-skill concinno-destruction-guard  # or your platform's
                                                    # skill install path
```

## Configuration

settings.json hook registration (auto-generated by `concinno init`):

```json
{
  "hooks": {
    "PreToolUse": [{
      "command": "python -m concinno.guards.destruction_guard"
    }]
  }
}
```

## Escape (when you really mean it)

```bash
CONCINNO_INLINE_SQUASH=1 claude  # whitelists squash_auto_commits R3
```

See [destruction_guard.py:1364](https://github.com/aiking931931/concinno/blob/main/src/concinno/destruction_guard.py#L1364)
for full per-op escape map.
```

## 下 session P0（具體動手）

1. Copy 範本 → 每個 guard 的 `src/concinno/skills/public/<name>/SKILL.md`
   （10 個檔，可批次寫）
2. `pyproject.toml` 加 10 個 `force-include` 路徑（model after `_cognitive`
   既有 force-include block）
3. pytest 確認 `concinno-skills install` CLI 能 list 新 skills
4. Bump 2.11.0 + CHANGELOG entry「Added: 10 SkillsMP-ready skill wrappers」
5. Build + twine check + commit + tag + push + AskUser
   `go publish concinno 2.11.0`
6. 確保 GitHub repo 有 ≥2 stars（SkillsMP filter requirement）
7. 等 SkillsMP scraper sync (refer skillsmp.com docs for cadence)

## Deferred / 留給後續

- F4 AgentBeats competition Safety track — blocked on benchmark infra
- F5 Guard Hub (社群貢獻) — 架構大 scope，先看 SkillsMP 接收
- F6 Workspace Convention Engine — 架構大 scope

## 參考

- Concinno guard pipeline: [src/concinno/guards/registry.py](../src/concinno/guards/registry.py)
- Guard base class: [src/concinno/guards/base.py](../src/concinno/guards/base.py)
- Existing skills: `src/concinno/skills/public/{agent,browser,windows,general-mode,competition-mode}`
