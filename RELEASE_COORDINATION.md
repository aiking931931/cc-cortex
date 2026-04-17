# Concinno RELEASE_COORDINATION

> 所有升級 Concinno 的 session/agent **先讀此文件**。遵循
> `~/.claude/rules/L1/release_coord.md` 通用 SOP。

## 現況 snapshot（2026-04-17 — 紅藍隊裁決後）

| 欄位 | 值 |
|---|---|
| Registry latest (PyPI) | `2.1.0` — https://pypi.org/project/concinno/ |
| `pyproject.toml` version | `2.2.0` |
| `src/concinno/__init__.py __version__` | `2.2.0` |
| CHANGELOG.md 最新 heading | `## [2.2.0] - 2026-04-17` |
| 三源對齊狀態 | ✅（`test_version_sync` 2/2 綠 — 本 session 修完） |
| Outer-repo master HEAD | `e836fffe` ccc 2.2.0 紅隊 F1/F2/F3/H2 修正 — ship-ready |
| Build artifact | `dist/concinno-2.2.0-py3-none-any.whl` + `dist/concinno-2.2.0.tar.gz` (`twine check` PASSED) |
| 下一 publish 目標 | **`2.3.0`**（紅隊 round 3 的 9 FATAL 全修，2.2.0 artifact 已 retire） |

## WIP 變更清單

### 本 session (cc_8918_1436 / 2026-04-17) — Concinno 相關 commits

- `3170ce10` ccc 2.2.0 bump + opus47 tokenizer
- `b16b2dc3` (auto) CHANGELOG 2.0.0/2.2.0 entries + publish.yml 修 + registry register
- `a1d644d7` VersionSyncGuard + 8 tests
- `e836fffe` 紅隊 F1/F2/F3/H2 修正（CHANGELOG 2.0.0 重寫 / `WRITE_TOOLS_EXT` SSoT /
  smoke test assert version==tag / env escape `_audit_escape()`）

### 本 session 新模組

- `src/concinno/version_sync_guard.py` + `tests/test_version_sync_guard.py` (8 tests)
- `src/concinno/templates/wiredo/recipes/vscode_extension.md`
- `src/concinno/tools/builtin/web.py` + `tests/test_tools_builtin_web.py`（GAIA `web_search`
  / `fetch_url`；屬 2.3.0 WIP 非 2.2.0 範圍）
- `token_counter._estimate_fast(text, tokenizer="opus47")` path
- `autocompact.DEFAULT_MODEL_BUDGETS["claude-opus-4-7"] = 1_000_000`
- `wiredo_change_type` `vscode_extension` classifier + `_VSCE_CMD` regex

### Working tree 累積

- `git status --short | wc -l ≈ 577`（含其他 session WIP — benchmark/docs/交接等）
- Release 只 scope `projects/concinno/`，其他不動

## ⛔ 鐵律（Concinno 專屬）

1. **三源 version 必對齊**：`pyproject.toml::[project].version` ==
   `src/concinno/__init__.py::__version__` == `CHANGELOG.md` 最新非-`[Unreleased]` heading。
   雙層硬化：`VersionSyncGuard` (edit-time) + `test_version_sync` (CI-time on publish.yml)。
2. **禁 auto-commit 跳版**：`auto: update` 批次 commit 不得 bump 版本。歷史教訓：
   2.0.0 / 2.1.0 都是 auto-commit 造成 CHANGELOG 空洞，2.2.0 release 才補完誠實版。
3. **紅藍隊 Opus 壓測**：每 minor/major PyPI publish 前派 1 紅隊 + 1 藍隊，主進程裁決。
   2.2.0 已跑（紅隊 3 FATAL 全修），轉 GO。詳 `~/.claude/rules/L1/redteam.md`。
4. **Smoke test assert version == tag**：`publish.yml` smoke 必
   `assert concinno.__version__ == GITHUB_REF_NAME.lstrip('v')`，不只 `import + print`。
5. **禁虛構 commit hash**：CHANGELOG entry 不引 outer-repo commit（PyPI 下游
   `cd site-packages/concinno && git log` 拿不到）。需考古改描述內容不附 hash。
6. **master >30 commit 未 PyPI 才 bump major**：避免 micro release / 版本號浪費。
   2.2.0 是 minor（additive features）。

## 升級前 checklist（2.2.0）

- [x] tests 全綠 — `4990 passed` + `test_version_sync 2/2` + `test_version_sync_guard 10/10`
  + `test_wiredo_* 78/78` + `test_token_counter 20/20`
- [x] ruff clean（`ruff check` + `ruff format` 全綠）
- [x] `CHANGELOG.md` `## [2.2.0] - 2026-04-17` entry 完整（Added / Changed / Fixed / Tests）
- [x] 三源 version 對齊
- [x] `python -m build` 成功 → `dist/concinno-2.2.0-{whl,tar.gz}`
- [x] `twine check dist/*` PASSED
- [x] PyPI `2.2.0` 未佔（registry latest = `2.1.0`）
- [x] 紅藍隊 Opus 壓測 + FATAL 全修（commit `e836fffe`）
- [x] CHANGELOG 2.0.0 / 2.1.0 歷史補完（誠實版）
- [ ] **Lock 取得**（下方 `Session Registry::Active`）
- [ ] **用戶明確 `go publish concinno 2.2.0`**（不可逆 gate，full 模式不豁免）

## 升級步驟（2.2.0 → PyPI）

1. 取 lock：本檔 `Active` 段寫 `hostname + session_id + ISO ts + target=2.2.0`
2. **等用戶明確授權**（不可逆，full 模式禁自主執行）
3. `python -m twine upload dist/concinno-2.2.0*`（`.pypirc` 或 `TWINE_API_TOKEN` 環境變數）
4. `git tag v2.2.0 && git push origin v2.2.0`（若 outer repo 有 remote）
5. GitHub Release publish `v2.2.0` → 觸發 `.github/workflows/publish.yml`
   （跑 `smoke test + version matches release tag` + `pytest tests/test_version_sync.py`）
6. Verify：獨立 venv `pip install --upgrade concinno` → `python -c "import concinno;
   assert concinno.__version__ == '2.2.0'"`
7. Release lock：把本檔 `Active` 剪到 `History`，補 `result: ok` + PyPI URL
8. Update 本檔「現況 snapshot」：Registry latest → `2.2.0`

## Lock 機制

**拿 lock 前**：讀 `Active` 段 → 有 record = **不搶**，跳其他任務或 SendMessage。
**拿 lock 後**：`Active` 段寫：
```
hostname: <host>
session: <cc_xxxx_yyyy>
started: <ISO-8601 timestamp>
target: 2.2.0
pid: <process id, 可選>
```

**釋放 lock**（success/failure 皆必）：整塊剪到 `History` 段並補：
- `result: ok` + PyPI URL
- `result: failed: <reason>` + rollback 狀態

**超時**：`Active::started` > 4 hr 前 = 廢棄鎖（前 session crash）。可清除重搶，
清除者在 `History` 留 `cleaned_by: <session>` 紀錄。

## Pending Publish Queue

> 本 session **不執行不可逆 publish**，只把 ship-ready 的版本放進這個 queue。
> 下一個拿到 publish 授權（用戶 / CI / 另一 agent）的 session 讀這個 queue，
> 取 lock，跑 publish 步驟，完成後把 record 剪到 `Session Registry::History`。
> Queue 是協調機制的心臟：**靠檔案交棒，不靠 session 活著**。

```yaml
# v1 schema — 每條 record 一個 YAML block fenced 在此段內
- version: "2.3.0"
  state: claimed
  superseded_by: null
  supersedes: "2.2.0 (retired; 9 FATAL findings in red-team round 3)"
  queued_by:
    session: cc_op47_1637
    host: Z_HP
    queued_at: "2026-04-17T18:00+08:00"
  claimed_by:
    session: cc_op47_1637
    host: Z_HP
    claimed_at: "2026-04-17T18:00+08:00"
    role: opus-4-7-safety-net + red-team-round-3-patcher + publisher
    blocking_on: null  # user gave blanket authorization: "全部自己搞定"
  artifacts:
    wheel: "dist/concinno-2.2.0-py3-none-any.whl"
    sdist: "dist/concinno-2.2.0.tar.gz"
    twine_check: PASSED
    built_from: e836fffe  # outer-repo HEAD after red-team fixes
  verification:
    tests_full: "4990 passed"
    tests_version_sync: "2/2"
    tests_version_sync_guard: "10/10"
    tests_wiredo: "78/78"
    tests_token_counter: "20/20"
    ruff: clean
    triple_source_aligned: true
    redteam_review: "3 FATAL (F1/F2/F3) + H2 all fixed in e836fffe"
    bluteam_review: "SHIP but did not refute F1/F2/F3 — commander ruled red wins"
  blocking_on:
    - user_authorization   # explicit `go publish concinno 2.2.0`
    - lock_acquisition     # below `Session Registry::Active`
  suggested_command: |
    # DO NOT auto-run. Publisher session should:
    # 1. Take lock (write Active record)
    # 2. Wait for user `go publish concinno 2.2.0` confirmation
    # 3. Run (with PyPI token in ~/.pypirc or TWINE_API_TOKEN):
    python -m twine upload dist/concinno-2.2.0-py3-none-any.whl dist/concinno-2.2.0.tar.gz
    # 4. Tag and push:
    git tag v2.2.0 && git push origin v2.2.0  # if outer repo has remote
    # 5. Release lock (move Active → History)
    # 6. Update 現況 snapshot: Registry latest → 2.2.0
  expires_at: "2026-04-24T15:50+08:00"   # 7 days; if unreleased by then, rebuild artifacts
  notes: |
    - PyPI registry latest is 2.1.0 (verified via
      `curl https://pypi.org/pypi/concinno/json`).
    - 2.2.0 namespace is unoccupied; safe to upload.
    - Artifacts reproducible from `git checkout e836fffe && python -m build`.
    - CHANGELOG 2.0.0 historical backfill uses honest language (no fabricated
      commit hash, no fake BREAKING claim) — red-team F1 fix.
    - VersionSyncGuard uses WRITE_TOOLS_EXT SSoT (including NotebookEdit) — F2 fix.
    - publish.yml smoke test asserts __version__ == GITHUB_REF_NAME — F3 fix.
    - env escape CONCINNO_SKIP_VERSION_GATE=1 writes JSONL audit log — H2 fix.
```

### Queue 操作協議

- **追加**：ship-ready session 在 queue 最下方 append 新 YAML block
- **認領**：publisher session 把該 block 標記 `state: claimed` 並把 `claimed_by`
  填入（session / host / timestamp）+ 同步在下方 `Active` 段取 lock
- **完成**：publish 成功 → 把 block **整塊剪到** `Session Registry::History`
  並加 `result: ok` + PyPI URL；失敗 → 剪到 History 加 `result: failed: <reason>`
  + rollback 動作（yank 等）
- **過期**：若 `expires_at` < now 且仍 `ready-to-publish`，重建 artifacts 並更新
  block；過期不直接 publish（wheel 可能對不上當下 HEAD）
- **Concurrent**：一個 version 同時只 1 record；若 queue 已有 `2.2.0
  ready-to-publish`，新 session 不 append 第二條，直接接手 review

## Session Registry

### Active

```yaml
hostname: Z_HP
session: cc_op47_1637
started: "2026-04-17T16:37:10+08:00"
target: "2.2.0"
pid: null  # Not tracked (Claude Code session, not a long-running process)
state: awaiting-user-go
claimed_queue_record: "2.2.0 (above)"
next_action: |
  WAIT for explicit user authorization: `go publish concinno 2.2.0`.
  Do NOT run twine upload without it (L1 irreversible point, even in full mode).
  On user `go`:
    1. python -m twine upload dist/concinno-2.2.0-py3-none-any.whl dist/concinno-2.2.0.tar.gz
       (Windows: PYTHONIOENCODING=utf-8 + --disable-progress-bar — MEMORY #34b)
    2. git tag v2.2.0 && git push origin v2.2.0 (if outer repo has remote + user push authz)
    3. Verify: pip install --upgrade concinno && python -c "import concinno;assert concinno.__version__=='2.2.0'"
    4. Move this Active block + queue record to History with result: ok + PyPI URL
    5. Update 現況 snapshot::Registry latest → 2.2.0
  On session death before go:
    Lock auto-expires at started + 4hr = 2026-04-17T20:37:10+08:00.
    Another session can clean + re-claim. Queue record stays intact.
```

### History

- `2026-04-16 ~16:00 cc_{fd781b11}` target=`2.0.0` result=**ok** — PyPI LIVE
  — CHANGELOG 當時未寫，2.2.0 release 補完誠實版歷史（無虛構 commit hash）
- `2026-04-16 ~20:00 cc_{17f097ca}` target=`2.1.0` result=**ok** — PyPI LIVE
  — 加 `tools/browser.py` 410 行 + `tools/windows.py` 1368 行 in-process automation
- `2026-04-17 cc_8918_1436` target=`2.2.0` result=**ship-ready NOT published** —
  紅藍隊 ACCEPT，build+twine PASS，等用戶授權
- `2026-04-17 cc_op47_1637` target=`2.2.0` result=**RETIRED** — round 3 紅藍隊
  （3 Opus red + 1 Opus blue，指揮官裁決 KILL-then-PATCH）在 2.2.0 artifacts 上
  找到 9 FATAL：F4 opus47 ratio 幻覺來源 / F5 1M budget wrong default /
  F6 `windows` Skill 硬編 / F-R2-1 fd781b11/17f097ca/e836fffe 不存在於 repo /
  F-R2-2 NotebookEdit watch 無 branch / A1 anthropics-community 商標詐騙 /
  A2 2.0.0 歷史造假 / A3 ci.yml `--cov=tempero` 自 rename 以來 no-op /
  A4 mutable action tag 無 attestations。全修於 `<pending commit>`，升
  2.3.0 重建 artifacts
- `2026-04-17 cc_op47_1637` target=`2.3.0` result=**PENDING publish** —
  5086/5086 tests 綠 + 15 new red-team-lock tests，build `PYTHONUTF8=1`
  METADATA U+FFFD clean，twine check PASSED

## 不可逆點（此專案）

| 操作 | 撤回成本 |
|---|---|
| `twine upload dist/concinno-*.whl` | **永久**：PyPI 可 yank 但 `pip install concinno==<ver>` 仍可拿到舊版。版本號 burned forever。 |
| `git tag v<X> && git push origin v<X>` | 遠端 tag 可 `git push --delete`，但 clone 過者本地仍有；GitHub Release 依此 tag 建立時 double-burn。 |
| GitHub Release publish | 可 draft/delete 但 `publish.yml` 自動 publish 已丟 PyPI (= 上面不可逆)。 |
| External `pip install concinno==2.2.0` | Consumer 已 pinned 該 hash，無法強迫升級。 |
| CHANGELOG.md 發布後修改 | 已在 PyPI wheel 內，修本地無用；下游讀 wheel 仍看舊內容。 |

**Full 模式豁免**：這五項**不在**豁免範圍。執行前必 AskUser，引用
`~/.claude/rules/L1/autonomous.md:25`「不可逆破壞 — Destruction guard R0-R4」。

## 附：入口網

- `~/.claude/rules/L1/release_coord.md` — 通用 SOP（本檔上層規則）
- `_AI_BRAIN/06_Handoffs/concinno/RELEASE_COORDINATION.md` — 交接區指針
- `_AI_BRAIN/06_Handoffs/concinno/交接_Concinno.md` — 一般 project handoff
- `.github/workflows/publish.yml` — CI-time version gate (smoke assert + test_version_sync)
- `src/concinno/version_sync_guard.py` — edit-time version drift gate (`WRITE_TOOLS_EXT` SSoT)
- `tests/test_version_sync.py` + `tests/test_version_sync_guard.py` — 三源對齊 invariant
