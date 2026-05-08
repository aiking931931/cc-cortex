"""Auto-generated partition 4/7 of FEATURE_META — DO NOT hand-edit boundaries.

Source range: feature_config.py legacy lines 2727-3536 (part4_core_behav).

To edit a feature here, edit *this file*. To re-split,
run ``python _tmp/split_feature_config.py`` (boundaries
declared in PARTS list there).
"""
from __future__ import annotations

from typing import Any

_FEATURE_META_PART_4: dict[str, dict[str, Any]] = {
    "ocr_fallback": {
        "category": "context",
        "description": (
            "Route text-heavy images through OCR + text-LLM reasoning "
            "(charts / headstones / documents) before vision"
        ),
        "description_zh": (
            "text-heavy 圖像走 OCR + text-LLM reasoning path（圖表 / "
            "headstone / 文檔），先試 OCR 再 fallback vision"
        ),
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {
            "min_chars": {
                "type": "int",
                "default": 40,
                "min": 10,
                "max": 500,
                "recommended": 40,
                "risk_low": (
                    "min_chars<10 accepts even noisy OCR output, "
                    "reasoning will be fed garbage"
                ),
                "risk_high": (
                    "min_chars>500 rejects most OCR signal — vision "
                    "path always wins, OCR never activates"
                ),
            },
        },
    },
    "plugins_enabled": {
        "category": "core",
        "description": (
            "Master switch for entry-points plugin discovery "
            "(concinno.features + concinno.skills). Off = ignore all "
            "installed concinno-skills-* packages. Off via env "
            "CONCINNO_PLUGINS_ENABLED=0 or allowlist restrictions via "
            "CONCINNO_PLUGINS_ALLOWLIST=pkg-a,pkg-b. Same trust model "
            "as pytest/flask/mkdocs plugins -- pip install is the "
            "trust boundary."
        ),
        "description_zh": (
            "Entry-points plugin 探測總開關（concinno.features + "
            "concinno.skills）。關閉 = 忽略所有已裝 concinno-skills-* "
            "套件。可用 env CONCINNO_PLUGINS_ENABLED=0 關閉，或用 "
            "CONCINNO_PLUGINS_ALLOWLIST=pkg-a,pkg-b 限制允許的套件。"
            "信任邊界 = pip install，同 pytest/flask/mkdocs 慣例。"
        ),
        "enabled": True,
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {},
    },
    # 3.2.0 — Sancio state_store v1 client (concinno.state_client). Thin
    # client implementing the project-scoped key-value API per
    # _AI_BRAIN/05_Planning/sancio_state_store_spec_2026-04-26.md. v1
    # default backend = JSON-per-project file under ~/.sancio/state/;
    # automatic fallback to legacy ~/.concinno/state/ for projects
    # written under the pre-spec layout. Sancio HTTP daemon backend is
    # ready (urllib transport) but the daemon itself is not yet shipped,
    # so the auto-detect chain reaches the file backend in practice.
    "state_client": {
        "category": "core",
        "description": (
            "Cross-session project-scoped key-value store. Replaces the "
            "kb_handoff §0 'one-key resurrection' markdown placeholders "
            "with a live source readable from any hook. Backend chain: "
            "Sancio daemon HTTP -> file (~/.sancio/state) -> legacy file "
            "(~/.concinno/state). Sancio port via SANCIO_STATE_STORE_PORT "
            "env (default 8530, deliberately off persona-api 8500 / "
            "llama-cpp 9000). preferred_backend chooseable via "
            "CONCINNO_STATE_BACKEND env or ~/.concinno/state_client.json."
        ),
        "description_zh": (
            "跨 session、project-scoped 的 key-value store。取代 "
            "kb_handoff §0「一鍵復活」markdown placeholder，讓任何 hook "
            "都能從活的來源讀。Backend chain: Sancio daemon HTTP -> file "
            "(~/.sancio/state) -> legacy file (~/.concinno/state)。"
            "Sancio port 透過 SANCIO_STATE_STORE_PORT env（default 8530，"
            "刻意避開 persona-api 8500 / llama-cpp 9000）。"
            "preferred_backend 可由 CONCINNO_STATE_BACKEND env 或 "
            "~/.concinno/state_client.json 覆寫。"
        ),
        "enabled": True,
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {
            "preferred_backend": {
                "type": "str",
                "default": "auto",
                "options": ["auto", "sancio_http", "file", "legacy_file"],
                "recommended": "auto",
                "risk_off": (
                    "'legacy_file' is read-only - writes raise "
                    "BackendUnavailable until backend changed back."
                ),
                "risk_off_zh": (
                    "'legacy_file' 是 read-only - 寫入會 raise "
                    "BackendUnavailable，直到 backend 改回。"
                ),
            },
            "sancio_port": {
                "type": "int",
                "default": 8530,
                "min": 1024,
                "max": 65535,
                "recommended": 8530,
                "risk_low": "Below 1024 needs root on POSIX",
                "risk_high": "Outside 1-65535 is invalid",
                "risk_low_zh": "低於 1024 在 POSIX 需 root",
                "risk_high_zh": "超出 1-65535 範圍無效",
            },
        },
    },
    # 2.36.0a1 — register intent_anchor as a first-class FEATURE_META row.
    # Was previously only a guard (concinno.intent_anchor_guard.IntentAnchorGuard)
    # picked up by the GuardPipeline dispatch via cfg.feature("intent_anchor",
    # "enabled"); never had a metadata row, so the GUI showed no severity /
    # consequences hint and the redteam (R#8) flagged it self-contradictory
    # ("severity none" while task_execution.md Stage 0 calls it Hard).
    "intent_anchor": {
        "category": "behavioral",
        "description": (
            "CBUA Stage 0 / B4 anchoring: re-inject the user's original "
            "intent every N write-tools to prevent scope drift, redteam "
            "tangents, and direction-loss in long sessions."
        ),
        "description_zh": (
            "CBUA 第 0 / B4 階段意圖錨定：每 N 個寫工具重新注入用戶原始意圖，"
            "防止 scope 漂移 / 紅隊岔題 / 長 session 方向丟失。"
        ),
        "ziq_autotunable": False,
        "cosmetic": False,
        "recommended": True,
        "severity_if_off": "major",
        "consequences_if_off": (
            "原始意圖不再被定期重新注入，長 session / 紅隊壓測後容易飄離主線"
        ),
        "consequences_if_off_en": (
            "Original intent stops being re-injected; long sessions and "
            "post-redteam loops drift from the user's first ask."
        ),
        "params": {},
    },
    # 2026-04-26 — DAG-aware time-scheduling hook for autonomous agent.
    # Six capabilities: pre-spawn ⬜ DAG visualiser, pre-spawn contention
    # check, idle-waiting detection, sub-agent budget tracker, re-triage
    # on completion, cancel-restart heuristic. Supersedes the placeholder
    # ``parallel_spawn_reminder`` shipped earlier the same day. All six
    # are syntactic / state-counter only — no LLM-as-judge call on the
    # hot path, zero per-turn API cost. Behavioural-signal driven (looks
    # at the agent's own recent turns + sub-agent registry), never the
    # user's prompt text.
    "time_steward": {
        "category": "behavioral",
        "description": (
            "DAG-aware time scheduling for autonomous agent: prevent "
            "wall-clock waste while sub-agents run in parallel. Six "
            "capabilities (DAG visualiser / contention check / idle "
            "detection / budget tracker / re-triage / cancel-restart)."
        ),
        "description_zh": (
            "自主代理 DAG 感知時間調度：防止子代理並行時主代理空轉浪費"
            " wall-clock。六項能力（⬜ DAG 視覺化 / spawn 前衝突檢查 /"
            "等待偵測 / 子代理預算追蹤 / 完成後再分流 / 取消重啟啟發式）。"
        ),
        "ziq_autotunable": False,  # UX behavioural judgement, not metric-optimisable
        "cosmetic": False,
        "recommended": True,
        "severity_if_off": "minor",
        "consequences_if_off": (
            "子代理並行時主代理可能 idle 等待 / 重複 spawn 衝突檔案 /"
            "子代理跑超時不被察覺"
        ),
        "consequences_if_off_en": (
            "Main agent may idle while sub-agents run, double-spawn on "
            "conflicting files, or miss stuck sub-agents past their "
            "estimate."
        ),
        "params": {
            # Capability #7 — polling watchdog (3.2.0). Surfaces stale
            # polling state when a sub-agent has been running long enough
            # to warrant a status check but the operator's polling
            # script either hasn't run, has gone stale, or is reporting
            # a non-RUNNING pod state. Reads
            # ``~/.concinno/state/poll_status.json``.
            "polling_watchdog_enabled": {
                "type": "bool",
                "default": True,
                "recommended": True,
                "risk_off": (
                    "Stuck sub-agents on dead pods may go unnoticed for "
                    "hours; operator does not get the inline reminder to "
                    "resume / re-poll."
                ),
                "risk_off_zh": (
                    "Pod 死掉後 stuck 子代理可能數小時無人察覺，operator"
                    "拿不到 inline 提醒去 resume / 重 poll。"
                ),
            },
            "polling_stale_minutes": {
                "type": "int",
                "default": 10,
                "min": 1,
                "max": 240,
                "recommended": 10,
                "risk_low": (
                    "Below 5 fires too eagerly on healthy long-running "
                    "sub-agents — noise."
                ),
                "risk_high": (
                    "Above 60 lets dead-pod incidents linger far past the "
                    "point a human would have noticed manually."
                ),
                "risk_low_zh": "低於 5 對健康長跑子代理 false-positive 太多",
                "risk_high_zh": "高於 60 會讓死 pod 事件拖很久才被發現",
            },
            "polling_inject_token_budget": {
                "type": "int",
                "default": 120,
                "min": 40,
                "max": 400,
                "recommended": 120,
            },
        },
    },
    # 4.1.0 — polling watcher: detect "agent is waiting on X" patterns
    # at PostToolUse, register a poll-able wait record, fan-in active
    # waits + drained alerts at UserPromptSubmit. Backed by a daemon
    # thread that re-runs check commands every interval_seconds.
    # NOT in DEFAULT_OFF_4_0_0 — productivity feature, ships on by default.
    "polling_watcher": {
        "category": "behavioral",
        "description": (
            "Auto-detect wait states (sub-agent dispatch, background bash, "
            "upload/deploy/CI) and run a real OS-timer daemon polling "
            "loop. Surfaces active waits + drained status alerts on "
            "every UserPromptSubmit. Independent of sub-agent "
            "notifications — the agent always knows what's pending."
        ),
        "description_zh": (
            "自動偵測等待狀態（子代理派發 / 背景 bash / 上傳 / 部署 / "
            "CI），啟動真實 OS-timer daemon 輪巡。每次 UserPromptSubmit "
            "fan-in active waits + 狀態變化 alerts。**不依賴**子代理通知 — "
            "agent 永遠知道有什麼 pending。"
        ),
        "ziq_autotunable": False,  # behavioural; binary judgement
        "cosmetic": False,
        "recommended": True,
        "severity_if_off": "minor",
        "consequences_if_off": (
            "等待狀態（上傳 / 部署 / 子代理）無自動 polling，agent 需手動"
            "ScheduleWakeup 或盯著背景任務"
        ),
        "consequences_if_off_en": (
            "Waits (upload / deploy / sub-agent) have no auto-polling; "
            "agent must remember ScheduleWakeup or babysit background "
            "tasks."
        ),
        "params": {
            "interval_seconds": {
                "type": "int",
                "default": 60,
                "min": 30,
                "max": 600,
                "recommended": 60,
                "risk_low": (
                    "Below 30 spams the daemon thread + check_cmd "
                    "subprocess at no benefit (status changes don't "
                    "happen sub-30s for these workloads)."
                ),
                "risk_high": (
                    "Above 600 (10 min) lets transitions linger past the "
                    "point a human-in-the-loop would have noticed manually."
                ),
            },
            "stale_age_seconds": {
                "type": "int",
                "default": 86400,
                "min": 3600,
                "max": 7 * 86400,
                "recommended": 86400,
            },
        },
    },
    # 4.2.0 — pip aftermath hint: detect ``pip install/uninstall``
    # touching concinno + check whether the long-running Memoria tray
    # app is still ticking via its heartbeat file. If heartbeat is
    # stale, emit a restart hint. Addresses "Memoria 整個不見了"
    # after a pip-upgrade cycle that left no traceback in the log
    # (daemon thread's logger garbage-collected with dying process).
    # Productivity feature, ships ON.
    "pip_aftermath_hint": {
        "category": "behavioral",
        "description": (
            "Detect pip install/uninstall touching concinno + check "
            "Memoria heartbeat freshness. Emit a restart hint when "
            "the heartbeat is stale (>5 min) — addresses the silent "
            "Memoria-died-mid-install class of failure."
        ),
        "description_zh": (
            "偵測 pip install/uninstall 動到 concinno + 檢查 Memoria "
            "heartbeat 新鮮度。過期 (>5 min) 提示 restart — 解 "
            "「Memoria 中途 import 失敗 silent die」這類無 traceback "
            "失蹤案例。"
        ),
        "ziq_autotunable": False,
        "cosmetic": False,
        "recommended": True,
        "severity_if_off": "minor",
        "consequences_if_off": (
            "pip 動 concinno 後 Memoria 若 silent die 不會有提醒，"
            "用戶下次發現 tray 不見才察覺"
        ),
        "consequences_if_off_en": (
            "If Memoria silently dies on a mid-install ImportError "
            "after a pip operation on concinno, no reminder is "
            "emitted; user notices when the tray icon is gone."
        ),
        "params": {
            "stale_threshold_seconds": {
                "type": "int",
                "default": 300,
                "min": 60,
                "max": 3600,
                "recommended": 300,
            },
        },
    },
    # 4.4.0 — FieldRead v2 ZIQ tunable: per-complexity compression breakeven.
    # Below this token count, handoff/memory text passes through
    # uncompressed (compression's quality loss exceeds token savings).
    # The C0Router fans out per-complexity overrides
    # (`COMPRESS_BREAKEVEN_BY_COMPLEXITY` in field_read) — this entry is
    # the *outcome-tunable global ceiling*: ZIQ FTRL nudges it within
    # [vmin=1500, vmax=4000] using the `expand()` callback trigger rate
    # as the outcome signal (frequent expand → breakeven was too low →
    # raise it; rare expand + budget pressure → lower it). Bus wiring
    # lives in ziq_outcome_bus (Sub-agent A scope); we register the
    # schema here so the bus has an entry to bind on.
    "field_read": {
        "category": "context",
        "description": (
            "Selective field extraction (handoff / memory) with "
            "per-complexity compression breakeven and "
            "<system-context-elided/> breadcrumbs."
        ),
        "description_zh": (
            "選擇性欄位抽取（交接/記憶），按複雜度動態壓縮 breakeven + "
            "<system-context-elided/> 麵包屑回填 LLM 認知。"
        ),
        "ziq_autotunable": True,
        "cosmetic": False,
        "recommended": True,
        "severity_if_off": "none",
        "consequences_if_off": (
            "Handoff / memory injection 走 v1 silent-compress 路徑，"
            "LLM 不知有東西被省，可能誤認原本沒這資訊"
        ),
        "consequences_if_off_en": (
            "Handoff / memory injection falls back to v1 silent "
            "compression — the LLM is unaware sections were elided "
            "and may hallucinate that the information was never there."
        ),
        "params": {
            "enabled": {
                "type": "bool",
                "default": True,
                "recommended": True,
                "risk_off": (
                    "Disabling drops back to v1 silent compressor — "
                    "no breadcrumbs, no expand() recall."
                ),
                "risk_off_zh": (
                    "關閉會退回 v1 silent compressor，無麵包屑、"
                    "無 expand() 召回。"
                ),
            },
            "compress_breakeven_tokens": {
                "type": "int",
                "default": 2500,
                "min": 1500,
                "max": 4000,
                "recommended": 2500,
                "risk_low": (
                    "Below 1500 elides too aggressively — "
                    "small handoffs lose actionable detail."
                ),
                "risk_high": (
                    "Above 4000 keeps verbose history in the prompt "
                    "and risks Lost-in-the-Middle attention drop."
                ),
                "risk_low_zh": "低於 1500 過度壓縮，小交接會丟可執行細節",
                "risk_high_zh": (
                    "高於 4000 prompt 拖長，命中 Lost-in-the-Middle 注意力"
                    "下沉曲線"
                ),
            },
            "include_breadcrumbs": {
                "type": "bool",
                "default": True,
                "recommended": True,
                "risk_off": (
                    "Skipping the breadcrumb tag saves ~50 tokens but "
                    "removes the LLM's awareness signal of elision."
                ),
                "risk_off_zh": (
                    "省掉麵包屑 tag 節省 ~50 tokens，但 LLM 失去「有東西"
                    "被省」的覺察訊號"
                ),
            },
        },
    },
    # 2026-05-03 — concinno 5.6.0 — fieldread/ 5-namespace governance core
    # (Cigito v3 patent moat axis 3, governance side). Standalone — does NOT
    # depend on aiking_core (Concinno is upstream of aiking_core; the
    # aiking_core.fieldread.namespaces is a separate AGPL implementation
    # detail with the same patent surface for license-firewall reasons).
    "fieldread.compressor": {
        "category": "context",
        "description": (
            "Concinno 5-namespace FieldRead compressor + breadcrumb "
            "audit trail (Cigito v3 patent moat axis 3, governance side). "
            "3-tier compression L1 (≤200ch index) / L2 (≤1500ch summary) / "
            "L3 (unbounded archive). Standalone — no aiking_core runtime dep."
        ),
        "description_zh": (
            "Concinno 5 命名空間 FieldRead 壓縮器 + 麵包屑審計鏈"
            "（Cigito v3 專利護城河第 3 軸，治理側）。三層壓縮 "
            "L1 (≤200 字索引) / L2 (≤1500 字摘要) / L3 (無上限存檔)。"
            "獨立模組，不依賴 aiking_core runtime。"
        ),
        "ziq_autotunable": True,
        "cosmetic": False,
        "recommended": True,
        "severity_if_off": "minor",
        "consequences_if_off": (
            "FieldRead 5-namespace 壓縮關閉，governance 端壓縮路徑不啟動，"
            "改走原始 content + breadcrumb 直通；不影響其他功能"
        ),
        "consequences_if_off_en": (
            "5-namespace FieldRead compression disabled — content "
            "flows through unchanged with the breadcrumb still attached. "
            "No other feature affected."
        ),
        "params": {
            "enabled": {
                "type": "bool",
                "default": True,
                "recommended": True,
                "risk_off": (
                    "Disabling skips the patent-moat compressor — "
                    "governance flows still work but lose ≤200ch / ≤1500ch "
                    "tier budgets."
                ),
                "risk_off_zh": (
                    "關閉會跳過專利護城河壓縮，治理流程仍可運作但失去 "
                    "≤200/≤1500 字層級預算"
                ),
            },
        },
    },
    # 2026-04-27 — MAR (Multi-Agent Reflexion) 4-perspective C5 self-correction.
    # Dispatches engineer / user / attacker / auditor Opus subagents in
    # parallel via the harness Agent dispatcher; aggregates findings with
    # ZIQ-FTRL-tuned weights. See concinno.guards.multi_perspective_reflection_guard.
    "mar_4perspective_reflection": {
        "category": "behavioral",
        "description": (
            "C5 self-correction reflection across 4 perspectives "
            "(engineer / user / attacker / auditor). Each perspective is "
            "an Opus subagent dispatched via the harness Agent tool; "
            "findings are aggregated with weights tuned by ZIQ FTRL "
            "from next-turn outcome signal. Triggered on task failure, "
            "user correction, or B5 anchor invocation."
        ),
        "description_zh": (
            "C5 自我修正 4 視角反省（工程師/用戶/攻擊者/審計者）。"
            "每個視角為 Opus 子代理，findings 用 ZIQ FTRL 學到的權重聚合。"
            "由任務失敗、用戶糾正或 B5 anchor 觸發。"
        ),
        "ziq_autotunable": True,
        "cosmetic": False,
        "recommended": True,
        "severity_if_off": "minor",
        "consequences_if_off": (
            "C5 自我修正失去 4 視角覆蓋，反思只走主代理單視角，"
            "易漏 user / attacker / auditor 維度"
        ),
        "consequences_if_off_en": (
            "C5 self-correction loses 4-perspective coverage; "
            "reflection collapses to main-agent self-view, missing "
            "user / attacker / auditor lenses."
        ),
        "params": {
            "engineer_weight": {
                "type": "float",
                "default": 0.30,
                "min": 0.0,
                "max": 1.0,
                "recommended": 0.30,
            },
            "user_weight": {
                "type": "float",
                "default": 0.25,
                "min": 0.0,
                "max": 1.0,
                "recommended": 0.25,
            },
            "attacker_weight": {
                "type": "float",
                "default": 0.20,
                "min": 0.0,
                "max": 1.0,
                "recommended": 0.20,
            },
            "auditor_weight": {
                "type": "float",
                "default": 0.25,
                "min": 0.0,
                "max": 1.0,
                "recommended": 0.25,
            },
            "fatal_severity_threshold": {
                "type": "int",
                "default": 2,
                "min": 1,
                "max": 10,
                "recommended": 2,
            },
            "max_concurrent_perspectives": {
                "type": "int",
                "default": 4,
                "min": 1,
                "max": 4,
                "recommended": 4,
            },
        },
    },
    # 2026-04-27 — DSPy MIPROv2 Bayesian prompt optimizer (wave-1).
    # Opt-in only — optimization runs burn LLM credits. Ships default-OFF
    # (also in DEFAULT_OFF_4_0_0 frozenset above).
    # Targets: mas_prompts critic/judge (GAIA exact-match metric).
    "dspy_prompt_optimization": {
        "category": "behavioral",
        "description": (
            "DSPy MIPROv2 Bayesian prompt optimizer for CBUA stage prompts. "
            "Auto-tunes critic/judge instructions from GAIA training examples "
            "instead of manual feedback-loop iteration. Default OFF — each "
            "optimization run calls the LM (credit cost). Enable to opt in."
        ),
        "description_zh": (
            "DSPy MIPROv2 Bayesian prompt 自動優化器，針對 CBUA stage prompts。"
            "從 GAIA training examples 自動 tune critic/judge instructions，"
            "取代人工反覆改 prompt 的 feedback loop。預設 OFF — "
            "每次 optimize 會呼叫 LLM（燒 credit）。啟用才生效。"
        ),
        "enabled": False,
        "ziq_autotunable": False,  # optimizer itself; would be circular
        "cosmetic": False,
        "recommended": False,
        "severity_if_off": "none",
        "consequences_if_off": (
            "CBUA critic/judge prompt 需人工 tune，無法自動 Bayesian search 最佳版本"
        ),
        "consequences_if_off_en": (
            "CBUA critic/judge prompts require manual tuning; "
            "no automatic Bayesian search for better instructions."
        ),
        "params": {
            "auto_mode": {
                "type": "str",
                "default": "light",
                "options": ["light", "medium", "heavy"],
                "recommended": "light",
                "risk_off": (
                    "'medium'/'heavy' run more trials and cost more. "
                    "'light' is appropriate for dev iteration."
                ),
            },
        },
    },
    # ── CBUA SOTA-borrow gap-fill (2026-04-27) ───────────
    "reflexion_guard": {
        "category": "soft_cognitive",
        "description": (
            "C5 Reflexion: synthesise why_failed narrative on tool failure, "
            "replay on next PreToolUse via additionalContext."
        ),
        "description_zh": (
            "C5 Reflexion：失敗時合成為什麼錯，下一次 PreToolUse "
            "用 additionalContext 顯示給模型看。"
        ),
        "ziq_autotunable": True,
        "cosmetic": False,
        "params": {
            "enabled": {
                "type": "bool",
                "default": True,
                "recommended": True,
            },
            "max_words": {
                "type": "int",
                "default": 80,
                "min": 30,
                "max": 200,
                "recommended": 80,
            },
            "injection_ttl_calls": {
                "type": "int",
                "default": 2,
                "min": 1,
                "max": 5,
                "recommended": 2,
            },
        },
    },
    "tot_branch_explorer": {
        "category": "soft_cognitive",
        "description": (
            "C3 Tree-of-Thought branch planner: recommend branch count + "
            "force convergence above budget threshold."
        ),
        "description_zh": (
            "C3 ToT 分支規劃：建議並行分支數 + 預算超閾強制收斂。"
        ),
        "ziq_autotunable": True,
        "cosmetic": False,
        "params": {
            "enabled": {
                "type": "bool",
                "default": True,
                "recommended": True,
            },
            "max_branches": {
                "type": "int",
                "default": 3,
                "min": 1,
                "max": 5,
                "recommended": 3,
            },
            "convergence_pct": {
                "type": "float",
                "default": 0.5,
                "min": 0.3,
                "max": 0.7,
                "recommended": 0.5,
            },
        },
    },
    "action_phase_signal": {
        "category": "soft_cognitive",
        "description": (
            "OODA/PDCA/ReAct behavioural phase counter. Emits a phase-"
            "distribution advisory summary every summary_interval calls."
        ),
        "description_zh": (
            "OODA/PDCA/ReAct 行為階段計數器，每 summary_interval 次呼叫"
            "輸出一次階段分布摘要。"
        ),
        "ziq_autotunable": True,
        "cosmetic": False,
        "params": {
            "enabled": {
                "type": "bool",
                "default": True,
                "recommended": True,
            },
            "summary_interval": {
                "type": "int",
                "default": 10,
                "min": 5,
                "max": 30,
                "recommended": 10,
            },
        },
    },
    "redblue_green_review": {
        "category": "behavioral",
        "enabled": True,
        "ziq_autotunable": True,
        "cosmetic": False,
        "description": (
            "Red+Blue+Green (RBG) review dispatch guard — 5-axis aggregation, "
            "5-state verdict, 4-step framing check, ZIQ-tuned axis weights."
        ),
        "description_zh": (
            "紅藍綠（RBG）審查派遣 guard — 5 軸聚合、5 態裁決、4 步 framing "
            "檢查、ZIQ 調 axis 權重。"
        ),
        "params": {
            "real_done_weight": {
                "type": "float",
                "default": 0.20,
                "min": 0.0,
                "max": 1.0,
                "recommended": 0.20,
            },
            "wired_weight": {
                "type": "float",
                "default": 0.20,
                "min": 0.0,
                "max": 1.0,
                "recommended": 0.20,
            },
            "functional_weight": {
                "type": "float",
                "default": 0.25,
                "min": 0.0,
                "max": 1.0,
                "recommended": 0.25,
            },
            "ai_capability_weight": {
                "type": "float",
                "default": 0.20,
                "min": 0.0,
                "max": 1.0,
                "recommended": 0.20,
            },
            "ux_friction_weight": {
                "type": "float",
                "default": 0.15,
                "min": 0.0,
                "max": 1.0,
                "recommended": 0.15,
            },
            "green_pm_trust": {
                "type": "float",
                "default": 0.70,
                "min": 0.0,
                "max": 1.0,
                "recommended": 0.70,
            },
            "fatal_threshold": {
                "type": "int",
                "default": 3,
                "min": 1,
                "max": 5,
                "recommended": 3,
            },
            "radius_chaotic_threshold": {
                "type": "float",
                "default": 0.90,
                "min": 0.0,
                "max": 1.0,
                "recommended": 0.90,
            },
            "max_concurrent_opus": {
                "type": "int",
                "default": 5,
                "min": 1,
                "max": 10,
                "recommended": 5,
            },
            "review_timeout_seconds": {
                "type": "int",
                "default": 300,
                "min": 60,
                "max": 1800,
                "recommended": 300,
            },
            "wire_into_u_stage": {
                "type": "bool",
                "default": False,
                "recommended": False,
            },
        },
        "recommended": True,
        "severity": "minor",
    },
    "review_router_ziq": {
        "category": "behavioral",
        "enabled": True,
        "ziq_autotunable": True,
        "cosmetic": False,
        "description": (
            "ZIQ-routed review method dispatcher — picks MAR (breadth) vs "
            "R+B+G (depth) vs sequential / parallel composites per task. "
            "SPS structural prior + FTRL outcome posterior."
        ),
        "description_zh": (
            "ZIQ 路由審查方法派遣 — 按任務挑 MAR（廣度）/ RBG（深度）/ "
            "順序 / 並行組合。SPS 結構先驗 + FTRL 結果後驗。"
        ),
        "params": {
            "meta_mar_every_n_chaotic": {
                "type": "int",
                "default": 10,
                "min": 1,
                "max": 100,
                "recommended": 10,
            },
            "ftrl_takeover_after_n_samples": {
                "type": "int",
                "default": 30,
                "min": 5,
                "max": 1000,
                "recommended": 30,
            },
            "cost_adjustment_factor": {
                "type": "float",
                "default": 1.0,
                "min": 0.1,
                "max": 10.0,
                "recommended": 1.0,
            },
        },
        "recommended": True,
        "severity": "minor",
    },
}
