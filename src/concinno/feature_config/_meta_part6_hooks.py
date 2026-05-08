"""Auto-generated partition 6/7 of FEATURE_META — DO NOT hand-edit boundaries.

Source range: feature_config.py legacy lines 4135-4385 (part6_hooks).

To edit a feature here, edit *this file*. To re-split,
run ``python _tmp/split_feature_config.py`` (boundaries
declared in PARTS list there).
"""
from __future__ import annotations

from typing import Any

_FEATURE_META_PART_6: dict[str, dict[str, Any]] = {
    # ── 4.7.0 W5 — notebooklm_hint UserPromptSubmit cosmetic stderr inject
    #
    # Detects ≥N (PDF / URL) sources or research-distill keywords in user
    # prompt → suggests yt-search → video-transcript → notebooklm chain
    # pipeline. Default OFF (opt-in per L0 鐵律 #6 + MEMORY #4s). Cosmetic
    # stderr nudge only — never deny, never block. Hook lives at
    # ~/.claude/hooks/notebooklm_hint.py wired in
    # ~/.claude/settings.json::hooks.UserPromptSubmit.
    #
    # Threshold tunable: min_sources_for_hint (3-10). ZIQ-autotunable by
    # downstream signal: hook fired count vs user actually invoking
    # /notebooklm afterward (FTRL learns when hint correlates with action).
    "notebooklm_hint": {
        "category": "behavioral",
        "enabled": False,  # opt-in per L0 #6 + MEMORY #4s
        "ziq_autotunable": True,
        "cosmetic": False,
        "severity_if_off": "minor",
        "consequences_if_off": (
            "user 給 ≥3 PDF/URL 時主代理可能 manually summarise 而非 chain "
            "yt-search → video-transcript → notebooklm 走 source-grounded 流程"
        ),
        "consequences_if_off_en": (
            "When user provides ≥3 PDF/URL sources, main agent may "
            "manually summarise instead of chaining yt-search → "
            "video-transcript → notebooklm for source-grounded distill."
        ),
        "description": (
            "UserPromptSubmit hint when ≥N sources detected — suggests "
            "yt-search → video-transcript → notebooklm chain pipeline. "
            "Cosmetic stderr inject, never deny."
        ),
        "description_zh": (
            "UserPromptSubmit 鉤子在偵測到 ≥N PDF/URL/research keyword 時 "
            "stderr 提示 chain pipeline。預設 OFF，opt-in 啟用。"
        ),
        "params": {
            "min_sources_for_hint": {
                "type": "int",
                "default": 3,
                "min": 2,
                "max": 10,
                "recommended": 3,
            },
        },
        "recommended": False,
    },
    # ── ai-king 6+ Wave 1.3 — shell.gepa_hook GEPA prompt evolution
    #
    # Wraps concinno-king/evolution/gepa_skill.py (385 LoC,
    # SkillGEPAOptimizer) as the outside-fork production caller for GEPA
    # prompt evolution. Solves 22-dim parity matrix dim 5 — "0 outside-fork
    # production caller for GEPA prompt evolution superset" — per verdict
    # 2026-05-08 §3 Wave 1.
    #
    # Default OFF per L0 鐵律 #6 + MEMORY #4s default-off audit + MEMORY
    # #4c "GEPA evolves prompts — risk surface for community-submitted
    # skills". Operators must opt in explicitly because the upstream
    # optimiser may rewrite SKILL.md text whose origin is not curator-
    # vetted (community-submitted skills). evolve_user_skills_only flag
    # caps the blast radius further (downstream curator reads this param
    # before invoking the optimiser per MEMORY 4zg open Q5).
    "shell.gepa_hook": {
        "category": "behavioral",
        "enabled": False,  # ⛔ DEFAULT OFF per MEMORY #4s + #4c
        "ziq_autotunable": False,
        "cosmetic": False,
        "severity_if_off": "minor",
        "consequences_if_off": (
            "Wave 1.3 GEPA prompt evolution 不啟用，22-dim parity dim 5 "
            "仍 0 outside-fork production caller，concinno-king "
            "SkillGEPAOptimizer 維持 island 狀態無 agent loop entry"
        ),
        "consequences_if_off_en": (
            "Wave 1.3 GEPA prompt evolution disabled; 22-dim parity "
            "dim 5 stays at 0 outside-fork production caller. The "
            "concinno-king SkillGEPAOptimizer remains an island with "
            "no agent loop entry point."
        ),
        "description": (
            "Wave 1.3 — wrap concinno-king GEPA prompt evolution; "
            "opt-in only. Listens to prompt_outcome events on the "
            "agent loop, captures (prompt_hash, outcome_score, "
            "mutation), best-effort feeds the upstream optimizer, "
            "appends jsonl to ~/.concinno/state/gepa_log.jsonl."
        ),
        "description_zh": (
            "Wave 1.3 — 包 concinno-king GEPA prompt 進化器; "
            "opt-in 才啟用。監聽 agent loop 的 prompt_outcome event, "
            "best-effort 餵給上游 optimizer, 寫 jsonl 到 "
            "~/.concinno/state/gepa_log.jsonl 供 OBS 重放。"
        ),
        "params": {
            "log_path": {
                "type": "str",
                "default": "~/.concinno/state/gepa_log.jsonl",
                "recommended": "~/.concinno/state/gepa_log.jsonl",
            },
            "evolve_user_skills_only": {
                "type": "bool",
                "default": True,
                "recommended": True,
            },
        },
        "recommended": False,
    },
    # ── ai-king 6+ Wave 1.5 — shell.invariants_hook three-invariants
    #
    # Wraps concinno-king/agent/invariants.py (703 LoC, separation /
    # fusion / autonomy validator) as the outside-fork production
    # caller for the three-invariants contract. Solves 22-dim parity
    # matrix dim 13 — "0 outside-fork production caller for
    # three-invariants validator superset" — per verdict 2026-05-08 §3
    # Wave 1.
    #
    # Default ON: invariant *observation* is read-only and cheap; the
    # upstream already gates fatal raises behind ``severity == "fatal"``
    # and the hook downgrades all non-fatal results to log-only. The
    # hook is registered LAST in the registry so it validates state
    # after the other 4 hooks (emergence / curator / gepa / ziq) have
    # potentially mutated session telemetry. Operators can disable
    # when running a pure-substrate measurement that should never see
    # hook side effects.
    "shell.invariants_hook": {
        "category": "behavioral",
        "enabled": True,
        "ziq_autotunable": False,
        "cosmetic": False,
        "severity_if_off": "minor",
        "consequences_if_off": (
            "Aiking shell 不再記錄 fusion-design 事件的三不變量驗證結果 "
            "(separation/fusion/autonomy)，下游 OBS 失去 audit trail; "
            "22-dim parity dim 13 仍 0 outside-fork production caller, "
            "concinno-king post_step_check 維持 island 狀態無 agent loop entry"
        ),
        "consequences_if_off_en": (
            "Aiking shell stops recording three-invariants validation "
            "on fusion-design events; OBS loses the audit trail. "
            "22-dim parity dim 13 stays at 0 outside-fork."
        ),
        "description": (
            "Wave 1.5 — wrap concinno-king three-invariants validator "
            "(separation / fusion / autonomy) as an aiking shell hook. "
            "Listens to fusion_design / module_merge / session_end events, "
            "runs upstream post_step_check, appends jsonl rows to "
            "~/.concinno/state/invariants_log.jsonl. Log-only — never "
            "aborts dispatch (fatal upstream raises are downgraded to "
            "FAIL rows so the registry continues)."
        ),
        "description_zh": (
            "Wave 1.5 — 把 concinno-king 三不變量驗證器 "
            "(separation / fusion / autonomy) 包成 aiking shell hook。"
            "監聽 fusion_design / module_merge / session_end 事件，跑 "
            "上游 post_step_check，寫 jsonl 到 "
            "~/.concinno/state/invariants_log.jsonl。Log-only — 永不 "
            "中斷 dispatch（fatal 上游 raise 降級為 FAIL 行讓 registry 續跑）。"
        ),
        "params": {
            "log_path": {
                "type": "str",
                "default": "~/.concinno/state/invariants_log.jsonl",
                "recommended": "~/.concinno/state/invariants_log.jsonl",
            },
        },
        "recommended": True,
    },
    # Wave 1.4 — ZIQ posterior router (SPS × FTRL) wired into the
    # aiking shell as outside-fork production caller. Per CBUA C1
    # ZIQ adjudicator + kb_ziq/theory_core.md the posterior gates
    # which concinno features each request enables. Default ON
    # because alpha_t tier classification is the load-bearing
    # decision adjudicator; ablation / red-team flips off.
    # ``ziq_autotunable=True`` — ZIQ itself is autotunable per
    # MEMORY 4z + L0 鐵律 #6 (autotune true rule, not cosmetic).
    "shell.ziq_hook": {
        "category": "behavioral",
        "enabled": True,
        "ziq_autotunable": True,
        "cosmetic": False,
        "severity_if_off": "minor",
        "consequences_if_off": (
            "Wave 1.4 ZIQ posterior router 不啟用，22-dim parity dim 6 "
            "仍 0 outside-fork production caller，alpha_t tier 回退到 "
            "硬編碼 default，CBUA C1 ZIQ adjudicator 失效"
        ),
        "consequences_if_off_en": (
            "Wave 1.4 ZIQ posterior router disabled; 22-dim parity dim "
            "6 stays at 0 outside-fork production caller. alpha_t tier "
            "classification falls back to hard-coded defaults; CBUA C1 "
            "ZIQ adjudicator is degraded."
        ),
        "description": (
            "Wave 1.4 — wrap concinno.ziq.router.ZIQFeatureRouter "
            "(posterior ∝ SPS × FTRL) as the agent-loop adjudicator. "
            "Reacts to skill_load / tool_dispatch / memory_recall / "
            "routing_decision events; gates on six-conditions per "
            "kb_ziq/theory_core.md; appends jsonl to "
            "~/.concinno/state/ziq_log.jsonl for OBS replay."
        ),
        "description_zh": (
            "Wave 1.4 — 包 concinno.ziq.router.ZIQFeatureRouter "
            "(posterior ∝ SPS × FTRL) 當 agent loop 決策仲裁器。"
            "監聽 skill_load / tool_dispatch / memory_recall / "
            "routing_decision event；按 kb_ziq/theory_core.md 六條件"
            "做適用性 gate；寫 jsonl 到 ~/.concinno/state/ziq_log.jsonl "
            "供 OBS 重放。"
        ),
        "params": {
            "log_path": {
                "type": "str",
                "default": "~/.concinno/state/ziq_log.jsonl",
                "recommended": "~/.concinno/state/ziq_log.jsonl",
            },
            "min_sps_threshold": {
                "type": "float",
                "default": 0.0,
                "min": 0.0,
                "max": 1.0,
                "recommended": 0.0,
                "risk_low": (
                    "Below 0 has no semantic meaning (alpha_t is non-negative)"
                ),
                "risk_high": (
                    "Above 0.5 silently masks low-confidence routing "
                    "decisions — ZIQ becomes overly conservative"
                ),
                "risk_low_zh": "低於 0 無語意（alpha_t 非負）",
                "risk_high_zh": (
                    "高於 0.5 會默默過濾低信心 routing decision，ZIQ 變過保守"
                ),
            },
            "ftrl_alpha": {
                "type": "float",
                "default": 0.1,
                "min": 0.001,
                "max": 1.0,
                "recommended": 0.1,
                "risk_low": (
                    "Below 0.001 makes FTRL learn too slowly to react to drift"
                ),
                "risk_high": (
                    "Above 1.0 destabilises FTRL — single bad outcome flips weights"
                ),
                "risk_low_zh": "低於 0.001 FTRL 學太慢追不上 drift",
                "risk_high_zh": "高於 1.0 FTRL 不穩，單次壞 outcome 就翻 weight",
            },
        },
        "recommended": True,
    },
}
