"""Auto-generated partition 5/7 of FEATURE_META — DO NOT hand-edit boundaries.

Source range: feature_config.py legacy lines 3537-4134 (part5_observ).

To edit a feature here, edit *this file*. To re-split,
run ``python _tmp/split_feature_config.py`` (boundaries
declared in PARTS list there).
"""
from __future__ import annotations

from typing import Any

_FEATURE_META_PART_5: dict[str, dict[str, Any]] = {
    # ── 4.5.0 W3 — Token Audit Autopilot ──
    #
    # Per-session token overhead audit (skills / MCP / sub-agents /
    # system floor) with a ZIQ FTRL-driven advisor for stale skills.
    # Default-OFF per 4.0.0 opt-in policy.
    "token_audit_autopilot": {
        "category": "observability",
        "enabled": True,  # 5.0.0 BREAKING — D-class promoted default-on per audit 2026-04-29
        "ziq_autotunable": True,
        "cosmetic": False,
        "severity_if_off": "none",
        "consequences_if_off": (
            "無 per-session token overhead 審計與閒置技能封存建議；"
            "context window 預算優化失去能見度。"
        ),
        "consequences_if_off_en": (
            "No per-session token overhead audit and no archive "
            "advisor for stale skills — context-window budget "
            "optimisation loses observability."
        ),
        "description": (
            "Per-session token overhead audit (skills / MCP / "
            "sub-agents / system floor) with ZIQ FTRL-routed "
            "advisor for stale skills."
        ),
        "description_zh": (
            "session 級 token 開銷審計（技能 / MCP / 子代理 / 系統地板），"
            "ZIQ FTRL 路由式建議封存閒置技能。"
        ),
        "params": {
            "system_prompt_floor_tokens": {
                "type": "int",
                "default": 14000,
                "min": 0,
                "max": 50000,
                "recommended": 14000,
                "risk_low": (
                    "0 disables the floor anchor — totals will under"
                    "report the actual prompt cost."
                ),
                "risk_high": (
                    "Above 30k anchors the floor too aggressively; "
                    "every session reports a misleading high baseline."
                ),
                "risk_low_zh": (
                    "0 取消地板錨定，總計會低估實際 prompt cost。"
                ),
                "risk_high_zh": (
                    "高於 30k 地板過度錨定，每 session 報告誤導性的"
                    "高基線。"
                ),
            },
            "skill_archive_days_threshold": {
                "type": "int",
                "default": 30,
                "min": 7,
                "max": 180,
                "recommended": 30,
                "risk_low": (
                    "Below 7 days the advisor will flag freshly-loaded "
                    "skills as stale — too aggressive."
                ),
                "risk_high": (
                    "Above 90 days the advisor never recommends — "
                    "stale skills accumulate."
                ),
                "risk_low_zh": (
                    "低於 7 天會把剛載入的技能當閒置 — 太激進。"
                ),
                "risk_high_zh": (
                    "高於 90 天 advisor 永遠不建議，閒置技能堆積。"
                ),
            },
            "archive_retention_days": {
                "type": "int",
                "default": 90,
                "min": 30,
                "max": 365,
                "recommended": 90,
                "risk_low": (
                    "Below 30 days the operator may lose recently"
                    "-archived skills before they realise they need them."
                ),
                "risk_high": (
                    "Above 180 days archive-root disk usage grows"
                    " without bound."
                ),
                "risk_low_zh": (
                    "低於 30 天會在操作員意識到需要恢復前就過期。"
                ),
                "risk_high_zh": (
                    "高於 180 天 archive 根目錄無上限成長。"
                ),
            },
            "audit_jsonl_retention_days": {
                "type": "int",
                "default": 90,
                "min": 7,
                "max": 365,
                "recommended": 90,
                "risk_low": "Below 7 days erases history too quickly.",
                "risk_high": (
                    "Above 180 days the jsonl directory grows"
                    " unboundedly."
                ),
                "risk_low_zh": "低於 7 天歷史紀錄太快被清。",
                "risk_high_zh": "高於 180 天 jsonl 無上限成長。",
            },
            "ftrl_alpha": {
                "type": "float",
                "default": 0.1,
                "min": 0.001,
                "max": 1.0,
                "recommended": 0.1,
                "risk_low": (
                    "Below 0.01 the advisor barely learns from"
                    " accept/reject feedback."
                ),
                "risk_high": (
                    "Above 0.5 a single rejection can swing weights"
                    " too aggressively."
                ),
                "risk_low_zh": (
                    "低於 0.01 advisor 幾乎不從用戶決策學習。"
                ),
                "risk_high_zh": (
                    "高於 0.5 單一拒絕會過度擺動權重。"
                ),
            },
        },
        "recommended": False,
        "severity": "minor",
    },
    # ── 4.6.0 — verbatim_relay self-branding for hook warnings ───────
    #
    # Why: prior to 4.6.0, hook warnings injected as ``[SHOW USER
    # VERBATIM] ⚠ ...`` carried no source attribution. Users seeing
    # those strings in the Claude Code transcript reasonably assumed
    # they were CC platform errors / hallucinations. Per AI King
    # 2026-04-29 directive, every Concinno hook warning must self-
    # brand with ``[Concinno: <feature>]`` so the source is
    # unambiguous and panic-free.
    #
    # ``cosmetic=True`` because this is a UX preference (display
    # shape), not a quality / safety knob. Per L0 鐵律 #6 ZIQ-vs-
    # manual priority, ZIQ FTRL must NOT autotune cosmetic params.
    # See ``concinno.hooks.relay_helpers`` for the runtime helper
    # and ``feedback_concinno_hook_warnings_must_self_brand.md``
    # for the corrective sediment.
    "verbatim_relay": {
        "category": "context",
        "severity_if_off": "none",
        "consequences_if_off": (
            "Hook warning 失去 [Concinno: <feature>] self-brand，"
            "用戶可能把 [SHOW USER VERBATIM] 誤認為 CC 平台異常"
        ),
        "consequences_if_off_en": (
            "Hook warnings lose the [Concinno: <feature>] self-brand; "
            "users may mistake [SHOW USER VERBATIM] for CC platform "
            "anomalies / hallucinations."
        ),
        "description": (
            "Self-brand hook warnings with [Concinno: <feature>] "
            "prefix so users can distinguish Concinno-controlled "
            "warnings from genuine Claude Code platform anomalies."
        ),
        "description_zh": (
            "Hook 警告加上 [Concinno: <feature>] 自我標識前綴，"
            "讓用戶能區分「Concinno 受控警告」vs「CC 平台異常」"
        ),
        "ziq_autotunable": False,  # cosmetic UX, no outcome metric
        "cosmetic": True,
        "params": {
            "mode": {
                "type": "str",
                "default": "prefix",
                "options": ["off", "silent", "prefix", "verbose"],
                "recommended": "prefix",
                "risk_off": (
                    "off mode drops warning entirely — caller must "
                    "skip emit; use only when paired with an "
                    "alternate observability channel"
                ),
                "risk_off_zh": (
                    "off mode 整段丟掉，呼叫方需 skip emit；"
                    "只在另有觀測渠道時用"
                ),
            },
        },
        "recommended": True,
    },
    "wiredo_subagent_verify": {
        "category": "behavioral",
        "enabled": True,  # 5.0.0 BREAKING — D-class promoted default-on per audit 2026-04-29
        "ziq_autotunable": True,
        "cosmetic": False,
        "description": (
            "D-axis sub-agent functional verification — schedules a "
            "distinct Opus verifier sub-agent for every WIREDO "
            "self-fill so the actor cannot grade its own homework. "
            "Per user directive 2026-04-29."
        ),
        "description_zh": (
            "D 維度子代理功能驗證 — 每個 WIREDO 自填都派一個獨立 "
            "Opus 驗證子代理，actor 不可自評。2026-04-29 用戶指令。"
        ),
        "params": {
            "retry_cap": {
                "type": "int",
                "default": 3,
                "min": 1,
                "max": 5,
                "recommended": 3,
            },
            "dispatch_radius_threshold": {
                "type": "str",
                "default": "high",
                "options": ["simple", "medium", "high", "chaotic"],
                "recommended": "high",
            },
            "timeout_ms_by_radius": {
                "type": "int",
                "default": 300000,
                "min": 60000,
                "max": 1800000,
                "recommended": 300000,
            },
            "auto_demote_state": {
                "type": "str",
                "default": "CRITICAL",
                "options": ["CRITICAL", "HIGH", "NORMAL", "SILENT"],
                "recommended": "CRITICAL",
            },
        },
        "recommended": False,
    },
    # ── 4.6.0 — 軌 B Habituation 三件套 (per 2026-04-29 commander verdict)
    #
    # The 4-channel verdict §3 軌 B identified habituation (LLM ignores
    # the same warning text after N repeats) as the real root cause that
    # channel routing alone cannot solve. The fix is three layers, each
    # with its own FEATURE_META entry below:
    #
    #   件 1 dedup        → exact (feature, msg) duplicates collapse per-session
    #   件 2 auto-demote  → N=3 consecutive ignores step the tier down
    #   件 3 FTRL ignore-rate → 5th ZIQ namespace per Hermes 4-cap §E.1
    #
    # All three are gated together by env CONCINNO_HABITUATION_DISABLED=1
    # so an operator can opt out of the entire 軌 B if they prefer the
    # legacy 4.5.0 behaviour. Per L0 鐵律 #6 ZIQ-vs-manual priority,
    # 件 1 is cosmetic (a UX preference: how often a duplicate is shown);
    # 件 2 + 件 3 are ZIQ-autotunable because their thresholds map to
    # measurable LLM-attention outcomes.
    "habituation_dedup": {
        "category": "context",
        "enabled": True,
        "ziq_autotunable": False,  # cosmetic: hash-equality dedup
        "cosmetic": True,
        "severity_if_off": "minor",
        "consequences_if_off": (
            "同 hook 同訊息單 session 內可能 inject 多次，"
            "LLM 對重複警告 habituate 後忽略後續 fire"
        ),
        "consequences_if_off_en": (
            "Same hook + same message can inject repeatedly within "
            "one session; LLM habituates and ignores later fires."
        ),
        "description": (
            "Producer-side content-hash dedup keyed on "
            "(session_id, feature, normalised_msg_hash). Same text "
            "from the same hook in the same session injects exactly "
            "once until the session boundary clears the cache."
        ),
        "description_zh": (
            "Producer 側 content-hash 去重，key=(session_id, feature, "
            "normalised hash)。同 session 同 hook 同訊息只 inject 一次，"
            "session 邊界自動清空。"
        ),
        "params": {
            "fallback_ttl_seconds": {
                "type": "float",
                "default": 300.0,
                "min": 30.0,
                "max": 3600.0,
                "recommended": 300.0,
            },
        },
        "recommended": True,
    },
    "habituation_auto_demote": {
        "category": "behavioral",
        "enabled": True,
        "ziq_autotunable": True,
        "cosmetic": False,
        "severity_if_off": "minor",
        "consequences_if_off": (
            "Hook tier 永遠不降級，CRITICAL 警告連續 ignore 後仍占用 "
            "LLM 注意力預算，新訊號被擠出"
        ),
        "consequences_if_off_en": (
            "Hook tier never demotes; CRITICAL warnings keep "
            "consuming attention budget after chronic ignore, "
            "crowding out novel signals."
        ),
        "description": (
            "Per-hook tier auto-demote: N=3 consecutive ignored fires "
            "step the tier down (CRITICAL → HIGH → NORMAL → SILENT_LOG). "
            "record_accept resets the counter; explicit reset() restores "
            "the tier."
        ),
        "description_zh": (
            "Per-hook tier 自動降級：連續 N=3 次 LLM ignore → "
            "tier 自動降一級（CRITICAL → HIGH → NORMAL → SILENT_LOG）。"
            "record_accept 重置計數；reset() 還原 tier。"
        ),
        "params": {
            "ignore_threshold": {
                "type": "int",
                "default": 3,
                "min": 1,
                "max": 10,
                "recommended": 3,
            },
        },
        "recommended": True,
    },
    "habituation_ignore_rate_ftrl": {
        "category": "ziq",
        "enabled": True,
        "ziq_autotunable": True,
        "cosmetic": False,
        "severity_if_off": "minor",
        "consequences_if_off": (
            "Hook accept-rate 不學，auto-demote tier 仍可運作但無 "
            "FTRL 自適應；4.7.0 軌 C tier 自動路由失去訓練資料"
        ),
        "consequences_if_off_en": (
            "Hook accept-rate is not learned; auto-demote still works "
            "rule-based but cannot self-tune. The 4.7.0 軌 C tier "
            "auto-router would be unable to converge without this "
            "training signal."
        ),
        "description": (
            "5th ZIQ outcome namespace ziq.outcome.hook_ignore_rate "
            "shared with verbatim_relay, dedup_layer, auto_demote, "
            "WiredoSubagentVerifyGuard and §C reliability prior. "
            "Per F7 fix, the outcome signal is the next-turn user-"
            "correction state (corrected = 0.0, silent = 1.0) — NOT "
            "behaviour-shifted, so Goodhart inflation cannot inflate "
            "the reward by the model self-rewarding."
        ),
        "description_zh": (
            "第 5 個 ZIQ outcome namespace ziq.outcome.hook_ignore_rate"
            "，與 verbatim_relay / dedup_layer / auto_demote / "
            "WiredoSubagentVerifyGuard / §C reliability prior 共用。"
            "F7 fix：outcome = 下一輪 user-correction 訊號"
            "（user 糾錯=0.0 / 靜默=1.0），非 behaviour-shifted，"
            "防 Goodhart inflation。"
        ),
        "params": {
            "alpha": {
                "type": "float",
                "default": 0.1,
                "min": 0.01,
                "max": 0.5,
                "recommended": 0.1,
            },
            "decay": {
                "type": "float",
                "default": 0.99,
                "min": 0.5,
                "max": 0.999,
                "recommended": 0.99,
            },
            "pending_ttl_seconds": {
                "type": "float",
                "default": 1800.0,
                "min": 300.0,
                "max": 7200.0,
                "recommended": 1800.0,
            },
        },
        "recommended": True,
    },
    # 4.6.0 — GUI Marketplace tab discovery layer (HP3). Pulls the curated
    # ``concinno-skills-*`` package list from PyPI, caches the result for
    # ``refresh_interval_hours`` and surfaces it in the Concinno GUI's
    # Marketplace tab. The fetch is offline-tolerant: a network failure
    # falls back to the last-known cache, so disabling the feature only
    # hides the UI tab — it never blocks the agent loop.
    "marketplace_discovery": {
        "category": "ux",
        "enabled": True,
        "ziq_autotunable": False,
        "cosmetic": False,
        "severity_if_off": "minor",
        "consequences_if_off": (
            "GUI Marketplace tab 不顯示 PyPI 套件清單，"
            "用戶仍可手動 pip install concinno-skills-*"
        ),
        "consequences_if_off_en": (
            "GUI Marketplace tab will not list PyPI packages; users can "
            "still manually pip install concinno-skills-* sub-packages."
        ),
        "description": (
            "PyPI-backed marketplace discovery for the Concinno GUI's "
            "Marketplace tab. Refreshes the curated concinno-skills-* "
            "list every refresh_interval_hours and caches the result so "
            "an offline session still renders the last-known list."
        ),
        "description_zh": (
            "GUI Marketplace 分頁的 PyPI 探索層。每 refresh_interval_hours "
            "重抓 concinno-skills-* 套件清單並快取，離線時仍顯示上一次抓到的清單。"
        ),
        "params": {
            "refresh_interval_hours": {
                "type": "int",
                "default": 1,
                "min": 0,
                "max": 24,
                "recommended": 1,
            },
        },
        "recommended": True,
    },
    # ── 4.7.0 W2 Option B — skill_usage_counter PreToolUse telemetry
    #
    # Per AI King 6.0 W2 Option B (2026-05-07 user directive 「做一個計數功能
    # 之後就知道哪些用到哪些用不到」). PreToolUse hook intercepts Skill /
    # Read tool calls, increments per-skill counter at
    # ~/.concinno/state/skill_usage_counter.json. After 1-2 weeks of telemetry
    # accumulation, enables data-driven informed-retire decisions for
    # protected-tier skills (28 user / 22 core / 19 plugin per SA-W2 §5.1).
    #
    # Default ON (cosmetic-light: raw counter, ~3KB JSON, no learning loop).
    # Per L0 鐵律 #6 cosmetic example — manual counter, ZIQ 不調.
    "skill_usage_counter": {
        "category": "context",
        "enabled": True,
        "ziq_autotunable": False,
        "cosmetic": True,
        "severity_if_off": "minor",
        "consequences_if_off": (
            "無 W2 Option B informed-retire telemetry，後續 protected-tier "
            "skill retire 仍只能靠 grep handoff 一次性 baseline，不能累積 "
            "forward usage data"
        ),
        "consequences_if_off_en": (
            "No W2 Option B informed-retire telemetry; future protected-tier "
            "retire decisions limited to one-shot grep baseline, no forward "
            "usage accumulation."
        ),
        "description": (
            "PreToolUse hook counts Skill tool invocations + Read tool against "
            "skills/<name>/ paths. Output ~/.concinno/state/skill_usage_counter.json "
            "{name: {invoked, read, last_seen}}. Enables data-driven retire "
            "decision after 1-2 weeks of telemetry accumulation."
        ),
        "description_zh": (
            "PreToolUse 鉤子記錄 Skill tool 呼叫 + Read tool 對 skills/<name>/ "
            "路徑的存取，累積到 ~/.concinno/state/skill_usage_counter.json，"
            "1-2 週後可作 informed-retire 決策依據"
        ),
        "params": {},
        "recommended": True,
    },
    # ── AI King 6.0 Wave 1.1 — shell.emergence_hook
    #
    # Per AI King 6.0 Wave 1 WIRE-shell-entry verdict (2026-05-08 §3 Wave 1.1).
    # Wraps concinno-king/skills/emergence.py (735 LoC, T1-T5 5-trigger skill
    # emergence detector) as an outside-fork production caller in the aiking
    # shell loop. Solves 22-dim parity matrix dim 3 (0 outside-fork production
    # caller for skill emergence superset) per MEMORY #4d island-caller
    # requirement. Hook registers via aiking.shell._build_default_registry so
    # every aiking shell session feeds tool-call telemetry into the upstream
    # 5-trigger detector.
    #
    # Default ON — the upstream detector has its own daily caps + cooldown so
    # a runaway loop cannot flood ~/.concinno/skill_drafts/. Operators who do
    # not want emergence telemetry set CONCINNO_SHELL_EMERGENCE_DISABLED=1 or
    # flip this switch off.
    "shell.emergence_hook": {
        "category": "behavioral",
        "enabled": True,
        "ziq_autotunable": False,
        "cosmetic": False,
        "severity_if_off": "minor",
        "consequences_if_off": (
            "aiking shell 不會把 tool-call telemetry 餵給 concinno-king "
            "5-trigger emergence detector，22-dim parity matrix dim 3 "
            "保留 0 outside-fork production caller。手動跑 concinno-king "
            "observe() 仍可用，但 shell session 不再自動觸發。"
        ),
        "consequences_if_off_en": (
            "aiking shell stops feeding tool-call telemetry into the "
            "concinno-king 5-trigger emergence detector. 22-dim parity "
            "matrix dim 3 stays at 0 outside-fork production callers. "
            "Manual concinno-king observe() invocations still work but "
            "shell sessions no longer auto-trigger emergence."
        ),
        "description": (
            "Wave 1.1 wrapper of concinno-king/skills/emergence.py (T1-T5 "
            "5-trigger skill emergence) registered into the aiking shell "
            "hook registry. Appends per-event jsonl to "
            "~/.concinno/state/emergence_log.jsonl + stages drafts to "
            "~/.concinno/skill_drafts/<slug>.md."
        ),
        "description_zh": (
            "Wave 1.1 包裝 concinno-king/skills/emergence.py（T1-T5 五"
            "觸發技能浮現偵測），註冊到 aiking shell 鉤子登錄器。每次"
            "事件追加 jsonl 到 ~/.concinno/state/emergence_log.jsonl，"
            "草稿落到 ~/.concinno/skill_drafts/<slug>.md。"
        ),
        "params": {
            "log_path": "~/.concinno/state/emergence_log.jsonl",
            "draft_dir": "~/.concinno/skill_drafts",
            "min_trigger_count": 3,
        },
        "recommended": True,
    },
    # ── AI King 6.0 Wave 1.2 — shell.curator_hook
    #
    # Per AI King 6.0 Wave 1 WIRE-shell-entry verdict (2026-05-08 §3 Wave 1.2).
    # Wraps concinno_king.governance.curator (934 LoC Hermes-derived stale /
    # archive / reactivate skill curation) as an outside-fork production caller
    # in the aiking shell loop. Solves 22-dim parity matrix dim 4 (0 outside-
    # fork production caller for skill curation superset) per MEMORY #4d
    # island-caller requirement.
    #
    # Cross-references the W2 Option B telemetry counter at
    # ~/.concinno/state/skill_usage_counter.json (per MEMORY 4zd) to apply the
    # informed-retire decision rule: KEEP if invoked+read >= keep_ref_threshold,
    # STALE if last_seen older than archive_days with zero usage,
    # REACTIVATE if recent invoke after stale marker, else BORDERLINE.
    #
    # Advisory only — never deletes / archives skill files (per L0
    # destruction_guard R0-R4). All actions emit jsonl records to
    # ~/.concinno/state/curator_log.jsonl for downstream tooling to act on.
    #
    # Default ON: production caller is the whole point — disabling defeats the
    # FATAL-2 fix. Env opt-out via AIKING_SHELL_CURATOR_HOOK_DISABLED=1 for
    # incident response only. Not ZIQ-autotunable: thresholds are operator-
    # tuned per workspace size, not learned from outcomes.
    "shell.curator_hook": {
        "category": "behavioral",
        "enabled": True,
        "ziq_autotunable": False,
        "cosmetic": False,
        "severity_if_off": "moderate",
        "consequences_if_off": (
            "AI King 6.0 22-dim parity matrix dim 4 退回 0 outside-fork "
            "production caller，skill curation 只能靠 concinno-king CLI 手動跑"
        ),
        "consequences_if_off_en": (
            "AI King 6.0 22-dim parity matrix dim 4 regresses to 0 outside-"
            "fork production caller; skill curation reverts to manual "
            "concinno-king CLI invocation only."
        ),
        "description": (
            "Wave 1.2 — wrap concinno-king curator stale/keep/reactivate "
            "decision in aiking shell. Cross-checks skill_usage_counter "
            "(MEMORY 4zd) to emit advisory jsonl records at "
            "~/.concinno/state/curator_log.jsonl. Never auto-deletes."
        ),
        "description_zh": (
            "Wave 1.2 — 在 aiking shell 包 concinno-king curator 的 stale/"
            "keep/reactivate 決策。跨查 skill_usage_counter（MEMORY 4zd）"
            "輸出建議式 jsonl 到 ~/.concinno/state/curator_log.jsonl，"
            "永不自動刪除。"
        ),
        "params": {
            "log_path": {
                "type": "str",
                "default": "~/.concinno/state/curator_log.jsonl",
                "recommended": "~/.concinno/state/curator_log.jsonl",
            },
            "usage_counter_path": {
                "type": "str",
                "default": "~/.concinno/state/skill_usage_counter.json",
                "recommended": "~/.concinno/state/skill_usage_counter.json",
            },
            "archive_days": {
                "type": "int",
                "default": 30,
                "min": 7,
                "max": 365,
                "recommended": 30,
            },
            "keep_ref_threshold": {
                "type": "int",
                "default": 6,
                "min": 1,
                "max": 100,
                "recommended": 6,
            },
        },
        "recommended": True,
    },
}
