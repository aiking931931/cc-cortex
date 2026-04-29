"""concinno.feature_config — Feature risk metadata, validation, and safe get/set.

@module feature_config
@responsibility Define risk metadata for all configurable features, validate parameter
    changes with min/max/recommended bounds, and provide safe get/set API
    with risk warnings.
@dependencies (none — self-contained metadata)
@exports list_features, get_feature, set_feature, validate_value, FEATURE_META,
    get_severity_tier

Schema additions (2.36.0a1 — all optional, backward-compatible):

* ``recommended`` (bool, default ``False``) — surfaced as a "Recommended ON"
  badge in the GUI; advisory, never overrides explicit user state.
* ``severity_if_off`` (Literal[``"none","minor","major","critical"]``,
  default ``"none"``) — drives 4-tier confirm UX in the GUI and gates
  whether ``set_feature`` writes to ``~/.concinno/critical_changes.log``.
  Invariant: every ``category == "hard_gate"`` entry MUST declare
  ``severity_if_off >= "major"``. Enforced by
  ``tests/test_feature_meta_schema_v2_36.py``.
* ``consequences_if_off`` (str, ≤120 chars zh-TW; default ``""``) — one-line
  plain-language consequence shown next to the toggle.
* ``consequences_if_off_en`` — English mirror; falls back to
  ``consequences_if_off`` when absent.

Wiring status (2.7.0 — every feature in this table is now live):

  Centralized wiring
  ------------------
  ``concinno.guards.pipeline.Pipeline._feature_enabled`` consults
  ``cfg.feature(name, "enabled")`` for every ``BaseGuard`` at every
  check/on_post_tool/on_stop call. Guard classes whose ``name``
  differs from their feature key declare ``feature_name =`` on the
  class (e.g. ``ReadFirstGuard.feature_name = "read_first_gate"``).

  Hook-level wiring
  -----------------
  Features that gate module functions rather than ``BaseGuard``
  subclasses read ``cfg.feature(..., "enabled")`` at the hook entry
  point. Current hook-level wirings (beyond the pipeline dispatch):

    * ``clarity_gate``       — on_prompt_submit.py
    * ``prompt_guard``       — on_prompt_submit.py (multi-question)
    * ``insight_engine``     — on_prompt_submit.py
    * ``streak_ux``          — on_post_tool.py (_run_streak_ux)
    * ``session_summary``    — on_stop.py (_session_summary)
    * ``delivery_gate``      — on_stop.py (_build_auto_delivery)
    * ``bash_background_gate`` / ``python_c_gate``
                              — pre_tool_guards.py (BashPythonGuard)

  Metadata-only
  -------------
  ``typescript``, ``language_enforce``,
  ``deny_marker``, ``token_display``, ``handoff_format``,
  ``pipeline_mode``, ``handoff_required_guard``, ``identity_guard``,
  ``butterfly_guard``, ``code_guard``, ``boundary_guard``,
  ``agent_cap``, ``design_theory``,
  ``token_gate``, ``structural_guard``, ``ui_verify``,
  ``publish_scan``, ``sentinel_gate``,
  ``consecutive_fail_gate``, ``hijack_gate`` — every one has either
  a ``BaseGuard`` subclass picked up by the pipeline dispatch or a
  direct ``cfg.feature()`` call at its hook entry point. Use
  ``concinno config set <name> enabled false`` and the guard stops
  running at runtime without a code change or session restart.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, Optional, cast

if TYPE_CHECKING:
    from pathlib import Path

# ── Fail-mode taxonomy (4.3.0 — Plan B Step 1) ──────────────────────
#
# A feature whose runtime check fails (or whose policy gate fires) can
# react in one of four escalating ways. Profiles + per-feature user
# overrides + ZIQ FTRL all converge on this same 4-value Literal so the
# downstream :class:`concinno.security.policy_gate.PolicyGate` can
# dispatch without speculation.
#
# silent     — log nothing, take no action (research / shadow mode)
# warn       — stderr warn once per session, still allow the action
# warn+log   — stderr warn + persist to ~/.concinno/audit.jsonl
# hard_deny  — raise / PreToolUse deny (only profile that blocks)
#
# The literal is canonical: the validator below rejects anything else.
# Storing the four values as a frozenset makes ``in`` lookups O(1) and
# saves a tuple-construction on every validate call.

FailMode = Literal["silent", "warn", "warn+log", "hard_deny"]

VALID_FAIL_MODES: frozenset[str] = frozenset({
    "silent", "warn", "warn+log", "hard_deny",
})

# ── 4.0.0 default-off catalogue ───────────────────────────
#
# Per AI King 2026-04-26 directive: every blocking feature except
# ``DestructionGuard`` (R0-R4 hardcoded data-deletion patterns) ships
# default-OFF in 4.0.0. ``pip install concinno`` then yields a permissive
# install — the user opts into individual gates via ``concinno features
# set <name> enabled true`` or the bulk ``concinno features set-profile
# strict`` shortcut.
#
# This frozenset is the *single source of truth* — keeping it in one
# place avoids the 26-edit scatter pattern and makes future audits
# (which features ship default-on?) one ``DEFAULT_OFF_4_0_0`` lookup.
#
# Senior-dev rationale: see
# ``feedback_default_off_gates_for_senior_devs.md`` (MEMORY index)
# and the CHANGELOG ``[4.0.0]`` entry.
#
# **NOT in this set** = ships default-ON. Currently every other
# FEATURE_META entry (UX, behavioural, context, hard_quality
# enforcement, ZIQ infra, etc.) — these are observability /
# coordination / rendering features that don't deny tool calls or
# block agent flow.
DEFAULT_OFF_4_0_0: frozenset[str] = frozenset({
    # hard_gate (18)
    "agent_cap", "bash_background_gate", "boundary_guard",
    "butterfly_guard", "clarity_gate", "consecutive_fail_gate",
    "delivery_gate", "handoff_required_guard", "hijack_gate",
    "identity_guard", "prompt_guard",
    "publish_scan", "publish_scan_guard", "python_c_gate",
    "read_first_gate", "release_authorization", "sentinel_gate",
    "token_gate", "ui_verify",
    # soft_gate (2)
    "handoff_claim_guard",
    "semver_gate",
    # external module (no FEATURE_META entry; honoured via
    # meta_enabled_default fallback chain)
    "premise_gate",
    # opt-in dev tool: burns LLM credits during optimization runs
    "dspy_prompt_optimization",
    # 4.4.0 — Plan B Week 2 stateful runtime guard, default OFF per
    # 4.0.0 default-off-gates SEMVER baseline.
    "circuit_breaker_guard",
    # 4.6.0 — W4 RCE injection guard, default OFF per 4.0.0 SEMVER baseline.
    "rce_injection_guard",
    # 4.6.0 — W4 HTTP-client request-shape policy gate, default OFF.
    "http_client_guard",
    # 4.6.0 — W4 wave-1 SQL injection scanner, default OFF.
    "sql_injection_guard",
})


def meta_enabled_default(name: str) -> bool:
    """Single source of truth for ship-level default-enabled.

    Lookup order:

    1. ``DEFAULT_OFF_4_0_0`` membership → returns ``False`` (the 4.0.0
       senior-dev permissive baseline). Includes ``premise_gate``
       even though it has no FEATURE_META entry — the lookup happens
       before the meta probe so this works.
    2. ``FEATURE_META[name]["enabled"]`` if explicitly declared.
    3. ``True`` (legacy default for entries that pre-date 4.0.0).

    Used by :meth:`concinno.core.config.Config.feature`,
    :func:`list_features`, and :func:`get_feature` so all three read
    paths agree on the same default — eliminates the GUI-vs-runtime
    divergence flagged by the 4.0.0 red/blue review verdict #6.

    Lookup is name-canonical only (no legacy alias resolution); the
    caller of :meth:`Config.feature` already canonicalises the name
    before consulting this helper.
    """
    if name in DEFAULT_OFF_4_0_0:
        return False
    meta = FEATURE_META.get(name)
    if meta is None:
        return True
    return bool(meta.get("enabled", True))


# ── Risk Metadata ─────────────────────────────────────────

FEATURE_META: dict[str, dict[str, Any]] = {
    # ── Hard Gates ──
    "token_gate": {
        "category": "hard_gate",
        "severity_if_off": "major",
        "consequences_if_off": "Token 防護完全失效，agent spawn 無上限可能 ctx 爆量",
        "description": "Block Agent spawn when context tokens exceed threshold",
        "description_zh": "Token 超過閾值時阻擋 Agent spawn",
        "ziq_autotunable": True,  # agent_threshold / critical_threshold are outcome-learnable
        "cosmetic": False,
        "params": {
            "mode": {
                "type": "str",
                "default": "step_back_first",
                "options": ["step_back_first", "hard_deny", "off"],
                "recommended": "step_back_first",
                "risk_off": "Gate completely disabled — no token protection",
                "risk_off_zh": "Gate 完全關閉 — 無 token 保護",
            },
            "agent_threshold": {
                "type": "int",
                "default": 140000,
                "min": 80000,
                "max": 180000,
                "recommended": 140000,
                "risk_low": (
                    "Below 80K blocks agents too early"
                    " — breaks normal multi-agent workflows"
                ),
                "risk_high": "Above 180K risks context overflow — session may corrupt",
                "risk_low_zh": "低於 80K 會過早擋 Agent，正常多代理工作流會壞掉",
                "risk_high_zh": "高於 180K 有上下文溢出風險，session 可能損壞",
            },
            "critical_threshold": {
                "type": "int",
                "default": 160000,
                "min": 120000,
                "max": 195000,
                "recommended": 160000,
                "risk_low": "Below 120K triggers urgent handoff too early",
                "risk_high": "Above 195K is dangerously close to context limit",
                "risk_low_zh": "低於 120K 會過早觸發緊急交接",
                "risk_high_zh": "高於 195K 危險接近上下文極限",
            },
        },
    },
    "read_first_gate": {
        "category": "hard_gate",
        "severity_if_off": "major",
        "consequences_if_off": "未讀全檔就改檔，蝴蝶效應未防",
        "description": "Block Edit/Write on existing files not yet Read this session",
        "description_zh": "阻擋未讀就改的操作（防止盲改）",
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {
            "mode": {
                "type": "str",
                "default": "step_back_first",
                "options": ["step_back_first", "hard_deny", "off"],
                "recommended": "step_back_first",
                "risk_off": "Gate disabled — blind edits allowed",
                "risk_off_zh": "Gate 關閉 — 允許盲改",
            },
            "min_lines": {
                "type": "int",
                "default": 50,
                "min": 10,
                "max": 500,
                "recommended": 50,
                "risk_low": "Below 10 blocks edits on tiny files — too aggressive",
                "risk_high": "Above 500 only catches very large files — misses most blind edits",
                "risk_low_zh": "低於 10 連小檔案都擋，太激進",
                "risk_high_zh": "高於 500 只擋超大檔案，大部分盲改抓不到",
            },
        },
    },
    "agent_cap": {
        "category": "hard_gate",
        "severity_if_off": "major",
        "consequences_if_off": "Agent spawn 無次數限制，遞迴爆炸風險",
        "description": (
            "Block execution-type Agent spawn after N times per session. "
            "Research agents (Explore/Plan/read-only) are uncapped."
        ),
        "description_zh": (
            "同一 session 執行型 Agent 超過 N 次後阻擋。"
            "研究型 Agent（Explore/Plan/唯讀）不限次數。"
        ),
        "ziq_autotunable": True,
        "cosmetic": False,
        "params": {
            "mode": {
                "type": "str",
                "default": "step_back_first",
                "options": ["step_back_first", "hard_deny", "off"],
                "recommended": "step_back_first",
                "risk_off": "Gate disabled — unlimited agent spawns",
                "risk_off_zh": "Gate 關閉 — Agent 無上限",
            },
            "max_spawns": {
                "type": "int",
                "default": 4,
                "min": 2,
                "max": 20,
                "recommended": 4,
                "risk_low": (
                    "Below 2 blocks almost all execution agent use"
                ),
                "risk_high": "Above 20 effectively disables the cap — token waste from agent spam",
                "risk_low_zh": "低於 2 幾乎擋掉所有執行型 Agent",
                "risk_high_zh": "高於 20 等於沒上限，Agent 濫用浪費 token",
            },
        },
    },
    "sentinel_gate": {
        "category": "hard_gate",
        "severity_if_off": "major",
        "consequences_if_off": "同檔反覆改不被擋，循環編輯潛在風險",
        "description": "Block repeated Edit on same file N+ times (with lint exception)",
        "description_zh": "同檔案連續 Edit N+ 次時阻擋（lint 修復例外）",
        "ziq_autotunable": True,
        "cosmetic": False,
        "params": {
            "mode": {
                "type": "str",
                "default": "step_back_first",
                "options": ["step_back_first", "hard_deny", "off"],
                "recommended": "step_back_first",
                "risk_off": "Gate disabled — edit loops undetected",
                "risk_off_zh": "Gate 關閉 — 編輯迴圈不會被偵測",
            },
            "max_repeats": {
                "type": "int",
                "default": 5,
                "min": 3,
                "max": 15,
                "recommended": 5,
                "risk_low": "Below 3 may false-positive on legitimate 2-step refactors",
                "risk_high": (
                    "Above 15 only catches extreme loops"
                    " — most stuck patterns slip through"
                ),
                "risk_low_zh": "低於 3 正常的兩步重構可能被誤擋",
                "risk_high_zh": "高於 15 只擋極端迴圈，大多數卡住模式會漏掉",
            },
            "lint_exception": {
                "type": "bool",
                "default": True,
                "recommended": True,
                "risk_off": "Disabling lint exception will block legitimate fix iterations",
                "risk_off_zh": "關閉 lint 例外會擋掉正常的修復迭代",
            },
        },
    },
    # ── Hard Quality ──
    "code_guard": {
        "category": "hard_quality",
        "description": "Python(ruff) / Rust(cargo) / Go(vet) static analysis",
        "description_zh": "Python/Rust/Go 靜態分析",
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {},
    },
    "structural_guard": {
        "category": "hard_quality",
        "description": "Structural analysis (func length / nesting / TODO debt / file size)",
        "description_zh": "結構分析（函數長度/巢狀深度/TODO 債務/檔案大小）",
        "ziq_autotunable": True,
        "cosmetic": False,
        "params": {
            "max_func_lines": {
                "type": "int", "default": 50, "recommended": 50,
            },
            "max_nesting_depth": {
                "type": "int", "default": 4, "recommended": 4,
            },
            "max_todo_count": {
                "type": "int", "default": 5, "recommended": 5,
            },
            "max_file_lines": {
                "type": "int", "default": 800, "recommended": 800,
            },
        },
    },
    "typescript": {
        "category": "hard_quality",
        "description": "tsc --noEmit type checking with SHA256 cache",
        "description_zh": "TypeScript 型別檢查（有快取）",
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {},
    },
    "linting": {
        "category": "hard_quality",
        "description": "ESLint for JavaScript files",
        "description_zh": "JavaScript ESLint 檢查",
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {},
    },
    "handoff_format": {
        "category": "hard_quality",
        "description": "Validate handoff file structure on write",
        "description_zh": "寫交接檔時驗證結構（lint 級錯誤）",
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {},
    },
    "prompt_guard": {
        "category": "hard_gate",
        "severity_if_off": "major",
        "consequences_if_off": "模糊 prompt + 多問題不被擋，意圖漂移風險",
        "description": "Clarity gate + multi-question detection for UserPromptSubmit",
        "description_zh": "清晰度 gate + 多問題偵測（UserPromptSubmit 階段）",
        "ziq_autotunable": True,
        "cosmetic": False,
        "params": {
            "clarity_threshold": {
                "type": "float",
                "default": 0.4,
                "min": 0.1,
                "max": 0.8,
                "recommended": 0.4,
                "risk_low": "Below 0.1 blocks almost nothing — gate ineffective",
                "risk_high": "Above 0.8 blocks most prompts — too aggressive",
                "risk_low_zh": "低於 0.1 幾乎不擋，gate 無效",
                "risk_high_zh": "高於 0.8 大部分提示都被擋，太激進",
            },
            "multi_q_threshold": {
                "type": "int",
                "default": 2,
                "min": 2,
                "max": 10,
                "recommended": 2,
                "risk_high": "Above 10 only catches extreme multi-topic prompts",
                "risk_high_zh": "高於 10 只擋極端多主題提示",
            },
        },
    },
    "handoff_required_guard": {
        "category": "hard_gate",
        "severity_if_off": "major",
        "consequences_if_off": "工作做完無交接強制，跨 session 遺失",
        "description": "Block session stop when work was done but no handoff file updated",
        "description_zh": "Session 有工作但無交接更新時阻擋停止",
        "routing_policy": "always_on",
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {
            "enabled": {
                "type": "bool",
                "default": True,
                "risk_off": "Sessions can end without handoff — work context is lost",
                "risk_off_zh": "Session 可結束而無交接 — 工作 context 流失",
            },
            "min_files": {
                "type": "int",
                "default": 3,
                "min": 1,
                "max": 20,
                "recommended": 3,
                "risk_low": "Too aggressive — even tiny edits demand a handoff",
                "risk_high": "Too lax — large changes can sneak through",
                "risk_low_zh": "太激進 — 連微小 edit 都要寫交接",
                "risk_high_zh": "太鬆 — 大改動可能跑掉",
            },
            "structural_gate_enabled": {
                "type": "bool",
                "default": True,
                "risk_off": "Frontmatter-only `last_updated:` bumps bypass the "
                            "guard — root cause of 'handoff touched but empty' "
                            "bug (feedback_handoff_guard_too_lenient.md)",
                "risk_off_zh": "關掉第二層後，只改 `last_updated:` frontmatter 也能過關 — "
                               "即 '交接動了但其實沒寫' 漏洞",
            },
            "min_added_lines": {
                "type": "int",
                "default": 10,
                "min": 1,
                "max": 200,
                "recommended": 10,
                "risk_low": "Tiny thresholds accept near-empty updates",
                "risk_high": "Above 50 forces verbose handoff even for small sessions",
                "risk_low_zh": "太低 — 近乎空白的更新也能過",
                "risk_high_zh": "高於 50 — 連小 session 都被逼寫長交接",
            },
            "min_signal_hits": {
                "type": "int",
                "default": 2,
                "min": 1,
                "max": 6,
                "recommended": 2,
                "risk_low": "Only 1 signal accepts prose-only updates without "
                            "status markers",
                "risk_high": "Above 4 demands every handoff carry most signal "
                             "types simultaneously",
                "risk_low_zh": "只要 1 個信號 — 純文字更新也能過，沒狀態標記",
                "risk_high_zh": "高於 4 — 每份交接都得同時帶多種信號",
            },
        },
    },
    "skill_proactive_router": {
        "category": "cognitive",
        "description": (
            "Proactive Skill router — surface '/skill matches your "
            "request' advisories when the user's prompt semantically "
            "maps to a registered Skill. Cheap inverted index + "
            "optional Haiku judge with hard cost cap."
        ),
        "description_zh": (
            "主動 Skill 路由 — 用戶提示語意命中已註冊 Skill 時建議"
            "「/skill 符合你的請求」。Cheap inverted index + 選擇性"
            " Haiku judge，有硬性成本上限。"
        ),
        "ziq_autotunable": True,
        "cosmetic": False,
        "params": {
            "enabled": {
                "type": "bool",
                "default": True,
                "recommended": True,
                "risk_off": (
                    "Disabled — no Skill suggestions; the agent may "
                    "miss applicable Skills the user didn't name"
                ),
                "risk_off_zh": (
                    "關閉 — 無 Skill 建議；代理可能漏掉用戶沒指名的"
                    "適用 Skill"
                ),
            },
        },
    },
    "skill_disclosure": {
        "category": "behavioral",
        "description": (
            "Three-layer Skill progressive disclosure (L1 always-loaded "
            "frontmatter / L2 trigger-loaded ≤50-line summary / L3 "
            "explicit-invoke full bundle). ZIQ-routed: P(skill | query) "
            "∝ SPS(description, query) × FTRL_weight(skill_history). "
            "Off by default per 4.0.0 opt-in policy."
        ),
        "description_zh": (
            "技能三層漸進式揭露（L1 常駐 frontmatter / L2 觸發載 ≤50 行摘要 "
            "/ L3 顯式調用完整 bundle）。ZIQ 路由：P(skill | query) ∝ "
            "SPS(描述, 查詢) × FTRL_weight(技能歷史)。4.0.0 起預設關閉，"
            "opt-in 啟用。"
        ),
        "ziq_autotunable": True,
        "cosmetic": False,
        "params": {
            "enabled": {
                "type": "bool",
                "default": False,
                "recommended": False,
                "risk_off": (
                    "Disabled — disclosure router silent; agent does not "
                    "see L1 candidates surfaced from skills dir"
                ),
                "risk_off_zh": (
                    "關閉 — 揭露路由靜默；代理看不到 skills dir 中"
                    "L1 候選"
                ),
            },
            "top_k_routing": {
                "type": "int",
                "default": 5,
                "min": 1,
                "max": 20,
                "recommended": 5,
                "risk_low": (
                    "At 1 only the single best match is shown — useful "
                    "candidates are silently dropped"
                ),
                "risk_high": (
                    "Above 20 the advisory grows past the noise floor "
                    "and floods the prompt"
                ),
            },
            "l1_cache_ttl_sec": {
                "type": "int",
                "default": 300,
                "min": 60,
                "max": 3600,
                "recommended": 300,
            },
            "min_route_score": {
                "type": "float",
                "default": 0.15,
                "min": 0.0,
                "max": 1.0,
                "recommended": 0.15,
            },
            "ftrl_alpha": {
                "type": "float",
                "default": 0.1,
                "min": 0.001,
                "max": 1.0,
                "recommended": 0.1,
            },
            "ftrl_decay": {
                "type": "float",
                "default": 0.99,
                "min": 0.5,
                "max": 1.0,
                "recommended": 0.99,
            },
        },
    },
    "insight_engine": {
        "category": "cognitive",
        "description": "Proactive knowledge injection when user prompt matches blind-spot rules",
        "description_zh": "用戶提示命中盲區規則時主動注入知識斷言",
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {
            "enabled": {
                "type": "bool",
                "default": True,
                "recommended": True,
                "risk_off": "Disabled — no proactive insights, user may miss shortcuts",
                "risk_off_zh": "關閉 — 無主動洞察，用戶可能錯過捷徑",
            },
            "custom_rules": {
                "type": "list",
                "default": [],
                "description": "User-defined insight rules (list of rule dicts)",
                "description_zh": "自訂 insight 規則（規則 dict 列表）",
            },
        },
    },
    # ── UX ──
    "streak_ux": {
        "category": "ux",
        "description": "Clean edit streak celebrations (🔥x5, ✅ fixed, etc.)",
        "description_zh": "連擊慶祝（🔥x5、✅ 修復 等）",
        "ziq_autotunable": False,
        "cosmetic": True,
        "params": {
            "milestone_interval": {
                "type": "int",
                "default": 5,
                "min": 1,
                "max": 50,
                "recommended": 5,
                "risk_low": "At 1, every single edit triggers celebration — noise",
                "risk_high": "Above 50, celebrations are so rare they lose motivational impact",
                "risk_low_zh": "設 1 每次編輯都慶祝，變噪音",
                "risk_high_zh": "超過 50 太少慶祝，失去激勵效果",
            },
        },
    },
    "session_summary": {
        "category": "ux",
        "description": "Visual session end summary (token/streak/files box)",
        "description_zh": "Session 結束時視覺化摘要框",
        "ziq_autotunable": False,
        "cosmetic": True,
        "params": {},
    },
    "deny_marker": {
        "category": "ux",
        "description": "Red ANSI counter on every deny (✖ 阻擋 #N)",
        "description_zh": "每次阻擋時的紅色計數通知",
        "ziq_autotunable": False,
        "cosmetic": True,
        "params": {},
    },
    "token_display": {
        "category": "ux",
        "description": "Append real token usage to CRITICAL/MILESTONE UX messages",
        "description_zh": "在錯誤/里程碑訊息後附加真實 token 用量",
        "ziq_autotunable": False,
        "cosmetic": True,
        "params": {},
    },
    # ── Boundary Guard (was soft, now hard gate via BoundaryGuard) ──
    "boundary_guard": {
        "category": "hard_gate",
        "severity_if_off": "critical",
        "consequences_if_off": "Hook/library boundary 違反不擋，可能破壞 concinno OSS layer 純粹度",
        "description": "Hook/library boundary violation detection (PreToolUse DENY)",
        "description_zh": "偵測 Hook/庫邊界違規（PreToolUse 硬擋）",
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {},
    },
    "publish_scan": {
        "category": "hard_gate",
        "severity_if_off": "critical",
        "consequences_if_off": (
            "publish 前 secrets/key/personal path 不掃，可能外洩 API key 到 OSS PyPI"
        ),
        "description": "Pre-publish artifact scan for secrets, keys, and personal paths",
        "description_zh": "發布前掃描打包物是否夾帶私鑰/密碼/個人路徑",
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {
            "mode": {
                "type": "str",
                "default": "hard_deny",
                "options": ["step_back_first", "hard_deny", "off"],
                "recommended": "hard_deny",
                "risk_off": "Gate disabled — secrets may leak in published packages",
                "risk_off_zh": "Gate 關閉 — 發布套件可能夾帶密鑰",
            },
        },
    },
    "identity_guard": {
        "category": "hard_gate",
        "severity_if_off": "critical",
        "consequences_if_off": "CLAUDE.md / 規則檔被改不擋，系統身份可被劫持",
        "description": (
            "Block Agent from modifying identity configs "
            "(CLAUDE.md, .claude/rules/, settings.json, hook configs)"
        ),
        "description_zh": (
            "阻擋 Agent 修改身份配置（CLAUDE.md、.claude/rules/、settings.json、hook 設定）"
        ),
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {
            "mode": {
                "type": "str",
                "default": "hard_deny",
                "options": ["step_back_first", "hard_deny", "off"],
                "recommended": "hard_deny",
                "risk_off": "Gate disabled — identity configs unprotected",
                "risk_off_zh": "Gate 關閉 — 身份配置無保護",
            },
        },
    },
    "bash_background_gate": {
        "category": "hard_gate",
        "severity_if_off": "major",
        "consequences_if_off": "長命令不 background 警告失效，可能 hang 主進程",
        "description": "Block long-running Bash commands without run_in_background",
        "description_zh": "阻擋未設 background 的長時間 Bash 指令",
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {
            "mode": {
                "type": "str",
                "default": "step_back_first",
                "options": ["step_back_first", "hard_deny", "off"],
                "recommended": "step_back_first",
                "risk_off": "Gate disabled — session may deadlock on long commands",
                "risk_off_zh": "Gate 關閉 — session 可能因長指令卡死",
            },
        },
    },
    "python_c_gate": {
        "category": "hard_gate",
        "severity_if_off": "major",
        "consequences_if_off": "python -c 多行命令警告失效，noisy 環境風險",
        "description": "Block complex python -c one-liners (>5 lines)",
        "description_zh": "阻擋過長的 python -c 指令（>5 行）",
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {
            "mode": {
                "type": "str",
                "default": "step_back_first",
                "options": ["step_back_first", "hard_deny", "off"],
                "recommended": "step_back_first",
                "risk_off": "Gate disabled — complex one-liners may cause encoding issues",
                "risk_off_zh": "Gate 關閉 — 複雜單行指令可能造成編碼問題",
            },
        },
    },
    "clarity_gate": {
        "category": "hard_gate",
        "severity_if_off": "major",
        "consequences_if_off": "模糊 prompt + 不可逆操作組合警告失效",
        "description": "Block ambiguous prompts combined with irreversible operations",
        "description_zh": "阻擋模糊意圖 + 不可逆操作的組合",
        "ziq_autotunable": True,
        "cosmetic": False,
        "params": {
            "mode": {
                "type": "str",
                "default": "step_back_first",
                "options": ["step_back_first", "hard_deny", "off"],
                "recommended": "step_back_first",
                "risk_off": "Gate disabled — ambiguous destructive prompts pass through",
                "risk_off_zh": "Gate 關閉 — 模糊的破壞性指令不會被攔截",
            },
            "min_clarity": {
                "type": "float",
                "default": 0.4,
                "min": 0.1,
                "max": 0.8,
                "recommended": 0.4,
                "risk_low": "Below 0.1 blocks almost all prompts",
                "risk_high": "Above 0.8 only catches extremely vague prompts",
                "risk_low_zh": "低於 0.1 幾乎擋掉所有提示",
                "risk_high_zh": "高於 0.8 只擋極模糊的提示",
            },
        },
    },
    "hijack_gate": {
        "category": "hard_gate",
        "severity_if_off": "critical",
        "consequences_if_off": "TADS 4 層 circuit breaker 失效，注意力挾持無防禦",
        "description": "TADS four-level circuit breaker based on hijack_score (L0→L2→L3→L4)",
        "description_zh": "TADS 四級斷路器：挾持分數分級 deny（L0→L2→L3→L4）",
        "ziq_autotunable": True,
        "cosmetic": False,
        "params": {
            "mode": {
                "type": "str",
                "default": "hard_deny",
                "options": ["step_back_first", "hard_deny", "off"],
                "recommended": "hard_deny",
                "risk_off": "Gate disabled — hijack loops undetected",
                "risk_off_zh": "Gate 關閉 — 挾持迴圈不會被偵測",
            },
            "l2_threshold": {
                "type": "float",
                "default": 0.3,
                "min": 0.1,
                "max": 0.5,
                "recommended": 0.3,
                "risk_low": "Below 0.1 triggers on normal tool usage — too many false positives",
                "risk_high": "Above 0.5 only catches severe hijack — misses early signs",
                "risk_low_zh": "低於 0.1 正常使用就觸發，假陽性太多",
                "risk_high_zh": "高於 0.5 只抓嚴重挾持，會漏掉早期信號",
            },
            "l3_threshold": {
                "type": "float",
                "default": 0.6,
                "min": 0.4,
                "max": 0.8,
                "recommended": 0.6,
                "risk_low": "Below 0.4 resets context too aggressively",
                "risk_high": "Above 0.8 rarely triggers context reset",
                "risk_low_zh": "低於 0.4 太激進地重置上下文",
                "risk_high_zh": "高於 0.8 幾乎不會觸發上下文重置",
            },
            "l4_threshold": {
                "type": "float",
                "default": 0.8,
                "min": 0.6,
                "max": 0.95,
                "recommended": 0.8,
                "risk_low": "Below 0.6 forces stop too early",
                "risk_high": "Above 0.95 almost never forces stop — model wastes entire context",
                "risk_low_zh": "低於 0.6 太早強制停止",
                "risk_high_zh": "高於 0.95 幾乎不會強制停止，模型浪費整個上下文",
            },
        },
    },
    "ui_verify": {
        "category": "hard_gate",
        "severity_if_off": "major",
        "consequences_if_off": "UI 改後 deploy 不要求截圖，假驗收風險",
        "description": "Lock after deploy with UI changes until screenshot verification",
        "description_zh": "deploy + UI 改動後鎖定，直到截圖驗證完成才釋放",
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {
            "mode": {
                "type": "str",
                "default": "step_back_first",
                "options": ["step_back_first", "hard_deny", "off"],
                "recommended": "step_back_first",
                "risk_off": "Gate disabled — UI changes after deploy go unverified",
                "risk_off_zh": "Gate 關閉 — deploy 後 UI 改動不會被驗證",
            },
        },
    },
    "delivery_gate": {
        "category": "hard_gate",
        "severity_if_off": "major",
        "consequences_if_off": "企業交付驗證失效，未驗 submission 可過",
        "description": (
            "Enterprise delivery verification — "
            "block submission of unverified work"
        ),
        "description_zh": (
            "企業級交付驗證 — 阻擋未驗證的工作提交"
        ),
        "ziq_autotunable": True,
        "cosmetic": False,
        "params": {
            "mode": {
                "type": "str",
                "default": "hard_deny",
                "options": ["step_back_first", "hard_deny", "off"],
                "recommended": "hard_deny",
                "risk_off": "Gate disabled — unverified work may be submitted",
                "risk_off_zh": "Gate 關閉 — 未驗證的工作可能被提交",
            },
            "max_iterations": {
                "type": "int",
                "default": 5,
                "min": 1,
                "max": 20,
                "recommended": 5,
                "risk_low": (
                    "At 1, no retry allowed"
                    " — single failure = permanent block"
                ),
                "risk_high": (
                    "Above 20 wastes tokens on"
                    " clearly unfixable tasks"
                ),
                "risk_low_zh": "設 1 不允許重試，一次失敗就永久擋住",
                "risk_high_zh": (
                    "超過 20 在明顯無法修復的任務上浪費 token"
                ),
            },
        },
    },
    "consecutive_fail_gate": {
        "category": "hard_gate",
        "severity_if_off": "major",
        "consequences_if_off": "N 連續失敗不擋，sentinel ConsecutiveFailGuard 失效",
        "description": "Block after N consecutive tool failures (stuck detection)",
        "description_zh": "連續 N 次工具失敗後阻擋（卡住偵測）",
        "ziq_autotunable": True,
        "cosmetic": False,
        "params": {
            "mode": {
                "type": "str",
                "default": "step_back_first",
                "options": ["step_back_first", "hard_deny", "off"],
                "recommended": "step_back_first",
                "risk_off": "Gate disabled — consecutive failures undetected",
                "risk_off_zh": "Gate 關閉 — 連續失敗不會被偵測",
            },
            "max_fails": {
                "type": "int",
                "default": 3,
                "min": 2,
                "max": 10,
                "recommended": 3,
                "risk_low": "Below 2 blocks after a single retry — too aggressive",
                "risk_high": "Above 10 lets the model waste many calls before intervening",
                "risk_low_zh": "低於 2 只重試一次就擋，太激進",
                "risk_high_zh": "高於 10 讓模型浪費太多次呼叫才介入",
            },
        },
    },
    # ── Design Theory ──
    "design_theory": {
        "category": "hard_quality",
        "description": (
            "Enforce design principles: Vertical Slice traceability on planning files, "
            "Deep Module ratio check on code files"
        ),
        "description_zh": (
            "設計理論強制：規劃檔 Vertical Slice 可追溯性 + 程式碼 Deep Module 比率檢查"
        ),
        "ziq_autotunable": True,
        "cosmetic": False,
        "params": {
            "mode": {
                "type": "str",
                "default": "step_back_first",
                "options": ["step_back_first", "hard_deny", "off"],
                "recommended": "step_back_first",
                "risk_off": "Gate disabled — no design principle enforcement",
                "risk_off_zh": "Gate 關閉 — 不強制設計原則",
            },
            "deep_module_ratio": {
                "type": "float",
                "default": 5.0,
                "min": 2.0,
                "max": 20.0,
                "recommended": 5.0,
                "risk_low": "Below 2.0 flags almost everything as shallow",
                "risk_high": "Above 20.0 only catches extremely shallow modules",
                "risk_low_zh": "低於 2.0 幾乎所有模組都被標記為淺模組",
                "risk_high_zh": "高於 20.0 只抓極淺的模組",
            },
        },
    },
    # ── Butterfly Effect Guard ──
    "butterfly_guard": {
        "category": "hard_gate",
        "severity_if_off": "critical",
        "consequences_if_off": "蝴蝶效應發現問題不擋繼續做，L0 鐵律 #1 失效",
        "description": (
            "Butterfly Effect: discover issue → must fix before continuing. "
            "Tracks issues in a session-scoped ledger, denies non-fix operations"
        ),
        "description_zh": (
            "蝴蝶效應守衛：發現問題必須立即處理，不處理就不能繼續。"
            "Session 級 Issue Ledger 追蹤，非修復操作被 deny"
        ),
        "ziq_autotunable": True,
        "cosmetic": False,
        "params": {
            "mode": {
                "type": "str",
                "default": "step_back_first",
                "options": ["step_back_first", "hard_deny", "off"],
                "recommended": "step_back_first",
                "risk_off": (
                    "Gate disabled — discovered issues can be ignored, "
                    "risking butterfly-effect bugs"
                ),
                "risk_off_zh": (
                    "Gate 關閉 — 發現的問題可被忽略，"
                    "有蝴蝶效應 bug 風險"
                ),
            },
            "max_fix_attempts": {
                "type": "int",
                "default": 3,
                "min": 1,
                "max": 10,
                "recommended": 3,
                "risk_low": "Below 2 forces handoff too quickly",
                "risk_high": "Above 5 may cause long stuck loops",
                "risk_low_zh": "低於 2 太快強制交接",
                "risk_high_zh": "高於 5 可能卡住太久",
            },
        },
    },
    # ── PII Guard (4.3.0 Plan B Step 3) ──
    "pii_guard": {
        "category": "security",
        "severity_if_off": "major",
        "consequences_if_off": (
            "PII (SSN / credit card / API keys / phone / email / IP / "
            "passport) can leak through tool inputs/outputs without "
            "warning. Default ON — opt out via FEATURE_META if needed."
        ),
        "description": (
            "Regex-based PII leak prevention guard. Detects SSN, "
            "credit cards (Luhn-validated), email, phone, IPv4/v6, "
            "API key prefixes (sk-/ghp_/AKIA/etc), passports, and "
            "driver licenses. Inherits PolicyGate fail-mode chain "
            "(silent / warn / warn+log / hard_deny) — default ``warn`` "
            "in ``mainstream``, ``hard_deny`` in ``strict``/``paranoid``."
        ),
        "description_zh": (
            "Regex 偵測 PII 洩漏 — SSN / 信用卡（Luhn 驗證）/ "
            "email / 電話 / IPv4-v6 / API key 前綴 / 護照 / 駕照。"
            "繼承 PolicyGate fail-mode 鏈，預設 mainstream warn / "
            "strict + paranoid hard_deny。"
        ),
        "enabled": True,
        "ziq_autotunable": True,
        "cosmetic": False,
        "recommended": True,
        "params": {
            "min_severity": {
                "type": "str",
                "default": "medium",
                "options": ["low", "medium", "high", "critical"],
                "recommended": "medium",
                "risk_low": (
                    "``low`` keeps every match incl. emails / IPs — "
                    "high noise rate in normal logs"
                ),
                "risk_high": (
                    "``critical`` only catches API keys — SSN / "
                    "credit-card / phone leaks slip through silently"
                ),
                "risk_low_zh": (
                    "``low`` 連 email / IP 全收，正常 log 噪音極高"
                ),
                "risk_high_zh": (
                    "``critical`` 只抓 API key，SSN / 信用卡 / 電話 "
                    "外洩會靜默漏掉"
                ),
            },
            "luhn_strict": {
                "type": "bool",
                "default": True,
                "recommended": True,
                "risk_off": (
                    "Disabling Luhn keeps raw 13-19 digit candidates — "
                    "order numbers / tracking codes spike false positives"
                ),
                "risk_off_zh": (
                    "關閉 Luhn 後 13-19 位數字會全收 — "
                    "訂單號 / 追蹤碼會大量誤判"
                ),
            },
            "redact_chars": {
                "type": "int",
                "default": 4,
                "min": 2,
                "max": 8,
                "recommended": 4,
                "risk_low": (
                    "Below 2 collapses every match to '***' — "
                    "no triage signal"
                ),
                "risk_high": (
                    "Above 8 leaks too much of the original secret "
                    "(``sk-ant-api03-...XYZW`` defeats redaction)"
                ),
                "risk_low_zh": (
                    "低於 2 全部變 '***' — 無從分流分析"
                ),
                "risk_high_zh": (
                    "高於 8 暴露太多原始 secret（redaction 失效）"
                ),
            },
        },
    },
    # ── Deserialize Guard (4.3.0 Plan B Step 4) ──
    "deserialize_guard": {
        "category": "security",
        "enabled": True,
        "ziq_autotunable": True,
        "cosmetic": False,
        "severity_if_off": "major",
        "consequences_if_off": (
            "Unsafe deserialize calls (pickle.load / yaml.load default "
            "Loader / dill / marshal / eval / exec) ship to production "
            "unflagged — single line of agent-generated code can RCE."
        ),
        "description": (
            "AST-based scan for unsafe deserialize calls "
            "(pickle/yaml/dill/marshal/eval/exec). Inherits PolicyGate "
            "fail-mode chain — default ``warn`` in mainstream, "
            "``hard_deny`` in strict/paranoid. Comment escape hatch "
            "``# CONCINNO_DISABLE:deserialize_guard:<reason>`` per call."
        ),
        "description_zh": (
            "AST 偵測不安全反序列化 — "
            "pickle/yaml/dill/marshal/eval/exec。"
            "繼承 PolicyGate fail-mode 鏈，mainstream warn / "
            "strict + paranoid hard_deny。逐行 escape："
            "``# CONCINNO_DISABLE:deserialize_guard:<reason>``。"
        ),
        "recommended": True,
        "severity": "major",
        "params": {
            "allow_pickle_with_protocol": {
                "type": "bool",
                "default": False,
                "recommended": False,
                "risk_off": (
                    "Allowing pickle silences every pickle.load / "
                    "pickle.loads finding — RCE-on-load remains."
                ),
                "risk_off_zh": (
                    "放行 pickle 會靜默所有 pickle.load 偵測 — "
                    "load 即 RCE 的風險不會消失"
                ),
            },
            "yaml_safe_loader_only": {
                "type": "bool",
                "default": True,
                "recommended": True,
                "risk_off": (
                    "Disabling SafeLoader-only flag lets ``yaml.load`` "
                    "with default Loader / FullLoader pass — PyYAML "
                    "FullLoader is NOT safe for untrusted input."
                ),
                "risk_off_zh": (
                    "關閉 SafeLoader-only 後 yaml.load 預設 Loader / "
                    "FullLoader 會被放行 — FullLoader 對不信任輸入並不安全"
                ),
            },
            "flag_reduce_override": {
                "type": "bool",
                "default": True,
                "recommended": True,
                "risk_off": (
                    "Skipping ``__reduce__`` overrides hides classes "
                    "designed to inject code on unpickle."
                ),
                "risk_off_zh": (
                    "略過 ``__reduce__`` override 會漏看為 unpickle "
                    "注入程式碼而設計的類別"
                ),
            },
            "min_severity": {
                "type": "str",
                "default": "medium",
                "options": ["low", "medium", "high", "critical"],
                "recommended": "medium",
                "risk_low": (
                    "``low`` keeps every match incl. literal eval / "
                    "__reduce__ — high noise on legitimate code."
                ),
                "risk_high": (
                    "``critical`` filters out dill / marshal / "
                    "subprocess.shell — high-severity bypass routes."
                ),
                "risk_low_zh": (
                    "``low`` 含字面 eval / __reduce__，合法程式碼噪音極高"
                ),
                "risk_high_zh": (
                    "``critical`` 會過濾 dill / marshal / subprocess.shell "
                    "繞道路徑，覆蓋率不足"
                ),
            },
        },
    },
    # ── RCE Injection Guard (4.6.0 W4) ──
    "rce_injection_guard": {
        "category": "security",
        # Default OFF per 4.0.0 default-off-gates SEMVER baseline —
        # also registered in DEFAULT_OFF_4_0_0 frozenset above.
        "enabled": False,
        # Severity thresholds + literal-eval gating are tunable; ZIQ
        # learns from accept/warn/deny outcomes when the bus is on.
        "ziq_autotunable": True,
        "cosmetic": False,
        "severity_if_off": "major",
        "consequences_if_off": (
            "Dynamic-code construction (f-string→shell, eval/exec on "
            "user input, compile('exec')) reaches disk unflagged; one "
            "agent line becomes an RCE primitive."
        ),
        "description": (
            "AST + regex scan for RCE-injection patterns "
            "(f-string-into-shell, eval/exec on dynamic args, "
            "compile(..., 'exec'), Bash backtick substitution, "
            "Bash unquoted-variable expansion). Inherits PolicyGate "
            "fail-mode chain — default ``warn`` when enabled, "
            "``hard_deny`` in strict/paranoid. Per-line escape: "
            "``# CONCINNO_DISABLE:rce_injection_guard:<reason>``."
        ),
        "description_zh": (
            "AST + regex 偵測 RCE 注入 — "
            "f-string 入 shell / eval-exec 動態參數 / "
            "compile('exec') / Bash 反引號 / Bash 未引用變數。"
            "繼承 PolicyGate fail-mode 鏈，預設啟用後 warn / "
            "strict + paranoid hard_deny。逐行 escape："
            "``# CONCINNO_DISABLE:rce_injection_guard:<reason>``。"
        ),
        "recommended": True,
        "severity": "major",
        "params": {
            "min_severity": {
                "type": "str",
                "default": "medium",
                "options": ["low", "medium", "high", "critical"],
                "recommended": "medium",
                "risk_low": (
                    "``low`` keeps eval-literal / exec-literal — "
                    "noisy on legitimate test code"
                ),
                "risk_high": (
                    "``critical`` filters out bash_unquoted_var / "
                    "format_shell — high-severity Bash RCE shapes leak"
                ),
                "risk_low_zh": (
                    "``low`` 連 eval/exec 字面量也保留，合法測試噪音極高"
                ),
                "risk_high_zh": (
                    "``critical`` 過濾 bash_unquoted_var / format_shell — "
                    "高風險 Bash RCE 形狀會漏掉"
                ),
            },
            "flag_eval_literal": {
                "type": "bool",
                "default": True,
                "recommended": True,
                "risk_off": (
                    "Disabling skips the audit-trail entry for literal "
                    "eval/exec — useful in heavy test setups but reduces "
                    "forensic visibility."
                ),
                "risk_off_zh": (
                    "關閉後 eval/exec 字面量不寫稽核 — "
                    "重測試場景可降噪但失去鑑識可見度"
                ),
            },
        },
    },
    # ── HTTP-Client Request-Shape Guard (4.6.0 W4) ──
    "http_client_guard": {
        "category": "security",
        # Default OFF per 4.0.0 default-off-gates SEMVER baseline —
        # also registered in DEFAULT_OFF_4_0_0 frozenset above.
        "enabled": False,
        # Allowlist / denylist / severity thresholds are tunable but
        # the policy evaluation outcome (accept/warn/deny) emits to
        # the ZIQ bus so FTRL can learn per-domain reputation.
        "ziq_autotunable": True,
        "cosmetic": False,
        "severity_if_off": "major",
        "consequences_if_off": (
            "HTTP-client tool calls (curl/wget/requests/httpx/aiohttp) "
            "ship without request-shape policy: bearer-token leaks, "
            "POST exfil, DELETE on prod hosts ship un-flagged."
        ),
        "description": (
            "Request-semantic policy gate for HTTP-client tool calls. "
            "Domain allow/deny-list, leaked-secret prefix detection in "
            "Authorization/Cookie/X-Api-Key headers, form-encoded POST "
            "exfil shape, and DELETE/PUT against ``*.prod.*`` hosts. "
            "Complementary to ssrf_guard (which validates network "
            "endpoints). Inherits PolicyGate fail-mode chain — default "
            "``warn`` when enabled, ``hard_deny`` in strict/paranoid. "
            "Per-line escape: ``# CONCINNO_DISABLE:http_client:<reason>``."
        ),
        "description_zh": (
            "HTTP-client 請求形狀政策閘 — 域名白/黑名單、洩漏密鑰 "
            "前綴偵測（Authorization / Cookie / X-Api-Key）、表單 "
            "POST exfil 形狀、production-shape host DELETE/PUT。"
            "與 ssrf_guard（驗證網路端點）互補。繼承 PolicyGate "
            "fail-mode 鏈，啟用後 warn / strict+paranoid hard_deny。"
            "逐行 escape：``# CONCINNO_DISABLE:http_client:<reason>``。"
        ),
        "recommended": True,
        "severity": "major",
        "params": {
            "allowlist_path": {
                "type": "str",
                "default": "~/.concinno/http_client_guard.json",
                "recommended": "~/.concinno/http_client_guard.json",
                "risk_off": (
                    "Empty allowlist means unknown_domain findings "
                    "never emit — first-line domain triage is off."
                ),
                "risk_off_zh": (
                    "空白名單會關掉 unknown_domain 偵測 — "
                    "第一道域名分級失效"
                ),
            },
            "secret_severity": {
                "type": "str",
                "default": "high",
                "options": ["low", "medium", "high", "critical"],
                "recommended": "high",
                "risk_low": (
                    "``low`` lets leaked Bearer tokens pass with a "
                    "soft warn — exfil is one network hop away."
                ),
                "risk_high": (
                    "``critical`` blocks every Authorization header "
                    "that matches a prefix — too aggressive on dev "
                    "environments where short-lived test tokens are "
                    "expected."
                ),
                "risk_low_zh": (
                    "``low`` 洩漏 Bearer token 只 warn，"
                    "exfil 一跳就出去"
                ),
                "risk_high_zh": (
                    "``critical`` 攔下所有 Authorization 命中 — "
                    "dev 環境短期 token 會誤擋"
                ),
            },
            "denylist_severity": {
                "type": "str",
                "default": "critical",
                "options": ["low", "medium", "high", "critical"],
                "recommended": "critical",
                "risk_low": (
                    "Lowering the denylist severity defeats the "
                    "explicit operator block — denylist hits should "
                    "always be loud."
                ),
                "risk_low_zh": (
                    "降低 denylist severity 等同放掉操作員明示 "
                    "封鎖 — 命中該大聲"
                ),
            },
        },
    },
    # ── Circuit Breaker Guard (4.4.0 Plan B Week 2) ──
    "circuit_breaker_guard": {
        "category": "security",
        # Default OFF per 4.0.0 default-off-gates SEMVER baseline —
        # also registered in DEFAULT_OFF_4_0_0 frozenset above.
        "enabled": False,
        # Rate / cooldown thresholds are tunable but the actionable
        # outcome (call admitted vs denied) emits to the ZIQ bus so
        # FTRL can learn per-resource cap settings.
        "ziq_autotunable": True,
        "cosmetic": False,
        "severity_if_off": "major",
        "consequences_if_off": (
            "External-dependency call streams (HTTP API / RPC / LLM "
            "provider / subprocess) lose runtime rate-limit + "
            "circuit-breaker protection. Cascading failures and "
            "thundering-herd retries against an already-degraded "
            "dependency are not flagged."
        ),
        "description": (
            "Stateful per-resource rate-limit + Hystrix circuit "
            "breaker (closed / open / half_open). Exponential "
            "backoff doubles cooldown on each re-open up to a 60s "
            "ceiling. Inherits PolicyGate fail-mode chain — default "
            "``warn`` in mainstream, ``hard_deny`` in strict / "
            "paranoid. Per-line escape ``# CONCINNO_DISABLE:"
            "circuit_breaker:<reason>``."
        ),
        "description_zh": (
            "有狀態 per-resource 速率限制 + Hystrix 斷路器 "
            "（closed / open / half_open）。指數退避每次重開 cooldown "
            "翻倍，上限 60 秒。繼承 PolicyGate fail-mode 鏈，"
            "mainstream warn / strict + paranoid hard_deny。逐行 "
            "escape：``# CONCINNO_DISABLE:circuit_breaker:<reason>``。"
        ),
        "recommended": True,
        "severity": "major",
        "params": {
            "max_calls": {
                "type": "int",
                "default": 60,
                "min": 0,
                "max": 100000,
                "recommended": 60,
                "risk_low": (
                    "``0`` disables rate-limit entirely — only the "
                    "circuit breaker stays active. Acceptable for "
                    "internal-only resources."
                ),
                "risk_high": (
                    "Above 1000 the sliding-deque memory cost climbs "
                    "linearly per resource (each entry is ~32B)."
                ),
                "risk_low_zh": (
                    "``0`` 完全停用速率限制，只剩斷路器。內部 "
                    "資源可接受，外部依賴不建議"
                ),
                "risk_high_zh": (
                    "高於 1000 後 sliding deque 每 resource 線性 "
                    "佔記憶體（每筆 ~32B）"
                ),
            },
            "window_s": {
                "type": "float",
                "default": 60.0,
                "min": 0.1,
                "max": 3600.0,
                "recommended": 60.0,
                "risk_low": (
                    "Sub-second windows lose smoothing — bursts of "
                    "successful calls trip rate-limit on their own."
                ),
                "risk_high": (
                    "Windows above 1 hour mask short outages — the "
                    "circuit breaker becomes the only active signal."
                ),
                "risk_low_zh": (
                    "次秒級視窗失去平滑性，正常成功 burst 會自觸 rate-limit"
                ),
                "risk_high_zh": (
                    "視窗大於 1 小時會掩蓋短時故障，只剩斷路器在運作"
                ),
            },
            "failure_threshold": {
                "type": "int",
                "default": 5,
                "min": 1,
                "max": 100,
                "recommended": 5,
                "risk_low": (
                    "``1`` opens on the first failure — extremely "
                    "noisy on flaky dependencies."
                ),
                "risk_high": (
                    "Above 20 the breaker tolerates long outage "
                    "runs before opening, defeating its purpose."
                ),
                "risk_low_zh": (
                    "``1`` 第一次失敗就開斷路器，flaky 依賴極吵"
                ),
                "risk_high_zh": (
                    "高於 20 容忍長時間故障，斷路器形同虛設"
                ),
            },
            "cooldown_s": {
                "type": "float",
                "default": 30.0,
                "min": 0.0,
                "max": 3600.0,
                "recommended": 30.0,
                "risk_low": (
                    "Below 1s the next probe fires before the "
                    "dependency has a chance to recover."
                ),
                "risk_high": (
                    "Above 5 minutes the breaker outlives most "
                    "transient outages, blocking healthy calls."
                ),
                "risk_low_zh": (
                    "低於 1s 探測過早，依賴沒時間恢復"
                ),
                "risk_high_zh": (
                    "高於 5 分鐘超過多數瞬時故障，會擋住已恢復的呼叫"
                ),
            },
            "backoff_max_s": {
                "type": "float",
                "default": 60.0,
                "min": 1.0,
                "max": 3600.0,
                "recommended": 60.0,
                "risk_low": (
                    "Low ceilings (<5s) prevent meaningful backoff "
                    "growth — same as a flat cooldown."
                ),
                "risk_high": (
                    "Above 30 minutes a single bad day pins the "
                    "breaker open for the rest of the session."
                ),
                "risk_low_zh": (
                    "上限太低（<5s）退避無實際成長，等同固定 cooldown"
                ),
                "risk_high_zh": (
                    "高於 30 分鐘，一次大故障可能整個 session "
                    "都斷路"
                ),
            },
        },
    },
    # ── SQL Injection Guard (4.6.0 W4 wave-1) ──
    "sql_injection_guard": {
        "category": "security",
        # Default OFF per L0 6-DoD opt-in baseline (4.0.0 SEMVER).
        "enabled": False,
        "ziq_autotunable": False,
        "cosmetic": False,
        "severity_if_off": "major",
        "consequences_if_off": (
            "Agent code interpolating user input into SQL "
            "(concat/f-string/%%/.format()) ships unflagged — single "
            "edit introduces OWASP A03:2021 Injection."
        ),
        "description": (
            "Regex-based SQL injection scanner. Detects 5 unsafe "
            "construction styles (concat / f-string / %% / .format() / "
            "dynamic identifier) and whitelists 4 safe alternatives "
            "(parametrized DB-API, SQLAlchemy text() bindparams, ORM "
            "filter syntax, psycopg.sql.Identifier). Inherits "
            "PolicyGate fail-mode chain — default ``warn`` in "
            "mainstream, ``hard_deny`` in strict / paranoid. Per-call "
            "escape ``# CONCINNO_DISABLE:<reason>``."
        ),
        "description_zh": (
            "Regex 偵測 SQL injection — 5 種不安全構造（concat / "
            "f-string / %% / .format() / 動態識別子）+ 4 種安全寫法 "
            "白名單（parametrized / SQLAlchemy bindparams / ORM "
            "filter / psycopg.sql.Identifier）。繼承 PolicyGate "
            "fail-mode 鏈。逐行 escape：``# CONCINNO_DISABLE:<reason>``。"
        ),
        "recommended": True,
        "severity": "major",
        "params": {
            "min_severity": {
                "type": "str",
                "default": "medium",
                "options": ["low", "medium", "high", "critical"],
                "recommended": "medium",
                "risk_low": (
                    "``low`` keeps every match incl. dynamic-identifier "
                    "interpolation — sharded systems and metaprogramming "
                    "spike false positives."
                ),
                "risk_high": (
                    "``critical`` keeps only the user-input concat shape "
                    "— f-string / %% / .format() injections slip through."
                ),
                "risk_low_zh": (
                    "``low`` 連動態識別子也算 — 分片系統與元程式設計"
                    "誤判率高"
                ),
                "risk_high_zh": (
                    "``critical`` 只剩 user-input concat — f-string / "
                    "%% / .format() injection 會漏掉"
                ),
            },
            "skip_test_files": {
                "type": "bool",
                "default": True,
                "recommended": True,
                "risk_off": (
                    "Disabling test-fixture skip flags every "
                    "``' OR 1=1 --`` literal in pytest fixtures — "
                    "drowns the audit log in intentional bad-string bait."
                ),
                "risk_off_zh": (
                    "關閉測試檔跳過後，pytest fixture 裡刻意的 "
                    "``' OR 1=1 --`` 字串會被全部標記，audit log "
                    "充滿假警報"
                ),
            },
        },
    },
    # ── Windows RAM Cleanup ──
    "memory_relief": {
        "category": "optional_optimization",
        "severity_if_off": "minor",
        "consequences_if_off": (
            "Long Claude sessions may accumulate standby pollution / "
            "leaked working sets without auto-recovery; user must run "
            "/memrelief manually"
        ),
        "description": (
            "Windows-only RAM cleanup with before/after stats and per-"
            "process trim list. SAFE tier needs no admin; STANDBY/"
            "AGGRESSIVE/DESTRUCTIVE require elevated token. Auto-fires "
            "as wave 4 of process_guard chain when wave 3 leaves RAM "
            "above threshold."
        ),
        "description_zh": (
            "Windows 記憶體清理：每進程 working set trim + standby/"
            "modified list 漸進式釋放，含 before/after 統計與每進程明細。"
            "預設 SAFE 不需 admin；aggressive 等級需要管理員。"
            "自動接在 process_guard wave 3 之後當 wave 4 救援。"
        ),
        # ziq_autotunable=False per red-team H-2: ZIQ outcome signal
        # ("MB freed") is blind to user IO penalty (disk re-read after
        # standby purge). Threshold stays user-tuned to avoid Goodhart.
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {
            "auto_trigger_after_process_guard": {
                "type": "bool",
                "default": True,
                "recommended": True,
                "risk_off": (
                    "process_guard finishes wave 3 without escalating to "
                    "memory_relief; user must trigger cleanup manually"
                ),
                "risk_off_zh": (
                    "process_guard wave 3 跑完不會自動清 standby，"
                    "用戶要手動觸發"
                ),
            },
            "auto_trigger_mode": {
                "type": "str",
                "default": "safe",
                "options": ["safe", "standby", "aggressive"],
                "recommended": "safe",
                "risk_off": (
                    "Setting to 'aggressive' auto-purges standby on every "
                    "process_guard escalation — IO penalty repeats"
                ),
                "risk_off_zh": (
                    "設成 aggressive 每次升級都全清 standby — IO 損失反覆"
                ),
            },
            "top_n_per_process_trim": {
                "type": "int",
                "default": 8,
                "min": 1,
                "max": 50,
                "recommended": 8,
                "risk_low": "Below 3 misses meaningful working-set heavyweights",
                "risk_high": "Above 20 trims the user's foreground app, causing UI lag",
                "risk_low_zh": "低於 3 漏掉真正吃 RAM 的 process",
                "risk_high_zh": "高於 20 會 trim 到用戶活躍的應用，造成 UI 卡頓",
            },
            "min_trim_mb": {
                "type": "int",
                "default": 50,
                "min": 10,
                "max": 1000,
                "recommended": 50,
                "risk_low": "Below 10 trims trivial processes for negligible gain",
                "risk_high": "Above 200 only trims giants, missing accumulated mid-size leaks",
                "risk_low_zh": "低於 10 為微小收益 trim 一堆小 process",
                "risk_high_zh": "高於 200 只 trim 巨無霸，漏掉累積的中型 leak",
            },
            "tray_enabled": {
                "type": "bool",
                "default": False,
                "recommended": False,
                "risk_off": (
                    "Tray icon disabled — user invokes via /memrelief skill "
                    "or python -m concinno.memory_relief instead"
                ),
                "risk_off_zh": (
                    "系統匣 icon 關閉 — 用戶透過 /memrelief 或 "
                    "python -m concinno.memory_relief 觸發"
                ),
            },
        },
    },
    # ── 3.1.3 (2026-04-26): wiring-audit fixes ──
    # All five entries below were added by the audit that discovered the
    # corresponding modules / docs claimed an opt-out toggle that no code
    # actually consulted. They expose the standard 6-source enabled chain
    # so ``concinno features set <name> enabled false`` (or the matching
    # env var documented in each module) really turns the gate off.
    "release_authorization": {
        "category": "hard_gate",
        # 2026-04-27 user directive (>10 corrections, final root-fix):
        # publish authorization is permanently opt-out. Fresh installs
        # MUST get this default-OFF — completes the 4.0.0 SEMVER-MAJOR
        # default-off transition this feature was missed from. See
        # feedback_publish_authorization_permanently_disabled.md.
        "enabled": False,
        "severity_if_off": "critical",
        "consequences_if_off": (
            "publish 不可逆操作（twine upload / cargo publish / git tag push remote）"
            "不再要求 chat 內 'go publish <pkg> <ver>' 字串確認；"
            "等同把全套 release 授權交給 harness 層獨自把關"
        ),
        "description": (
            "Block irreversible publish operations until the user types "
            "'go publish <pkg> <ver>' in chat (STRING_MATCH) or selects "
            "the equivalent AskUserQuestion option (ASKUSER_ANSWER). "
            "Honours release_auth.disabled=True as a global bypass. "
            "4.2.3+: also wires atomic per-package ReleaseLock + PyPI "
            "pre-check via acquire_for_upload() context manager so "
            "concurrent sessions cannot collide on twine upload."
        ),
        "description_zh": (
            "阻擋 twine upload / cargo publish / git tag push remote 等"
            "不可逆 publish 操作，直到用戶在 chat 打出"
            "'go publish <pkg> <ver>' 字串確認。"
            "若 ~/.concinno/release_auth.json::disabled=True 則整層跳過。"
            "4.2.3+ 額外串接 atomic ReleaseLock + PyPI pre-check"
            "（acquire_for_upload context manager），防止多 session"
            "並發 publish 撞 PyPI 400 already-exists race。"
        ),
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {
            # ``lock_ttl_minutes`` is exposed so ``concinno features
            # get release_authorization`` surfaces it. It is **not**
            # ZIQ-autotunable — TTL is operational correctness, not
            # outcome-learnable: too short drops live releases (data
            # loss), too long wedges crashed sessions (availability).
            # Correctness wins over learning. Read at call time by
            # ``concinno.coordination.release_lock._ttl_seconds()`` via
            # env var ``CONCINNO_RELEASE_LOCK_TTL_MIN``.
            "lock_ttl_minutes": {
                "type": "int",
                "default": 30,
                "min": 5,
                "max": 240,
                "recommended": 30,
                "risk_low": (
                    "Below 5 minutes can revoke an in-flight upload from "
                    "a slow network, causing the next acquire to take "
                    "over and produce a double-publish race"
                ),
                "risk_high": (
                    "Above 240 minutes (4 hours) leaves crashed-session "
                    "locks wedged for hours, blocking the recovery session"
                ),
                "risk_low_zh": (
                    "低於 5 分鐘會把慢網路下尚未完成的 upload 視為過期，"
                    "下個 acquire 就接手 → 雙重 publish race"
                ),
                "risk_high_zh": (
                    "高於 240 分鐘（4 小時）會把 crash session 的鎖卡住"
                    "好幾小時，恢復用 session 無法接手"
                ),
            },
        },
    },
    "publish_scan_guard": {
        # Distinct from the existing ``publish_scan`` entry above —
        # ``publish_scan`` is the doc-level toggle for the scanner CLI;
        # ``publish_scan_guard`` is the BaseGuard wiring that the audit
        # discovered was orphaned (now registered in 3.1.3).
        "category": "hard_gate",
        "severity_if_off": "critical",
        "consequences_if_off": (
            "publish 前 dist/ 不掃 secrets/keys/personal-paths，可能外洩到 PyPI"
        ),
        "description": (
            "Pre-twine PreToolUse scan of dist/ artifacts for secrets, "
            "keys, and personal absolute paths. Hard-deny on CRITICAL hits."
        ),
        "description_zh": (
            "twine upload 前 PreToolUse hook 掃 dist/ 是否夾帶密鑰/憑證/"
            "個人絕對路徑；CRITICAL 命中直接 hard deny"
        ),
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {},
    },
    "semver_gate": {
        "category": "soft_gate",
        "severity_if_off": "minor",
        "consequences_if_off": (
            "publish 前不檢查 public API 移除 / 重命名，可能 patch / minor "
            "版本暗藏 breaking change"
        ),
        "description": (
            "Compare current public API against committed snapshot; "
            "deny twine upload when breaking changes detected without "
            "a major version bump. No-op when no api_snapshot.json exists."
        ),
        "description_zh": (
            "publish 前比對當前 public API 與 commit 過的 snapshot；"
            "偵測 breaking change 但版號未升 major 時 deny。"
            "無 api_snapshot.json 時 no-op"
        ),
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {},
    },
    "handoff_claim_guard": {
        "category": "soft_gate",
        "severity_if_off": "minor",
        "consequences_if_off": (
            "Agent 講 '已寫入交接' 但 git 沒交接檔變動的情境不再被擋；"
            "假交接會通過 stop"
        ),
        "description": (
            "Detect 'wrote handoff' / '交接已更新' claim in last assistant "
            "message; verify git diff actually contains a handoff_*.md / "
            "交接_*.md change. Block stop on mismatch (1 block per session)."
        ),
        "description_zh": (
            "偵測 assistant 最後一段是否聲稱'已寫入交接'但 git 中無對應"
            "交接檔變動；不一致時阻擋（每 session 最多 block 1 次）"
        ),
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {},
    },
    # ── Pipeline Mode ──
    "pipeline_mode": {
        "category": "context",
        "description": (
            "Toggle between Dynamic (Guard Pipeline + learning loop) and "
            "Static (pure prompt pipeline, no guards) mode. "
            "Dynamic is a strict superset of Static"
        ),
        "description_zh": (
            "切換動態（Guard Pipeline + 學習循環）和靜態（純 prompt pipeline）模式。"
            "動態是靜態的嚴格超集"
        ),
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {
            "mode": {
                "type": "str",
                "default": "dynamic",
                "options": ["dynamic", "static"],
                "recommended": "dynamic",
                "risk_off": (
                    "Static mode disables all Guard Pipeline protections — "
                    "equivalent to gstack/Pocock level (prompt-only, no learning)"
                ),
                "risk_off_zh": (
                    "靜態模式關閉所有 Guard Pipeline 保護 — "
                    "等同 gstack/Pocock 等級（純 prompt，無學習）"
                ),
            },
        },
    },
    # ── 2.16.0 — Session summary CLI + permission bootstrap ──
    "session_switches": {
        "category": "context",
        "description": (
            "SessionStart summary of non-default switches — ensures the agent "
            "reads user opt-outs before primacy-bias kicks in"
        ),
        "description_zh": (
            "Session 開始時把非預設 switch 值摘要進 agent context，"
            "避免 primacy bias 忽略用戶設定"
        ),
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {
            "top_n": {
                "type": "int",
                "default": 10,
                "min": 1,
                "max": 30,
                "recommended": 10,
                "risk_low": "top_n=0 produces empty output — hook becomes noop",
                "risk_high": "top_n>30 floods agent context",
            },
            "hook_format_compact": {
                "type": "bool",
                "default": True,
                "recommended": True,
                "risk_off": (
                    "Verbose hook output consumes extra tokens in every "
                    "SessionStart — fine for dev, expensive in prod"
                ),
            },
        },
    },
    "configure_permissions": {
        "category": "utility",
        "description": (
            "One-shot allowlist bootstrap — add ~100 safe Bash patterns "
            "(pytest/ruff/git/pip) to ~/.claude/settings.json so the agent "
            "stops being prompted for routine ops"
        ),
        "description_zh": (
            "一次把 ~100 條安全 Bash pattern (pytest/ruff/git/pip) 加進 "
            "~/.claude/settings.json，避免每次日常操作都被 prompt"
        ),
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {
            "publish_opt_in": {
                "type": "bool",
                "default": False,
                "recommended": False,
                "risk_off": (
                    "Default OFF protects against accidental publish "
                    "bypass; enable only if you trust the agent to never "
                    "twine upload unintentionally"
                ),
            },
            "preserve_destructive": {
                "type": "bool",
                "default": True,
                "recommended": True,
                "risk_off": (
                    "Setting this False would let rm -rf / git push --force "
                    "into allow[] — destruction_guard is the last line of "
                    "defense, do not disable"
                ),
            },
        },
    },
    # ── Language Enforcement ──
    "language_enforce": {
        "category": "context",
        "description": (
            "Inject language enforcement on every tool call"
            " — forces thinking + responses in configured language"
        ),
        "description_zh": "每次工具呼叫注入語言強制 — 思考和回答都用設定語言",
        "ziq_autotunable": False,
        "cosmetic": True,
        "params": {
            "language": {
                "type": "str",
                "default": "English",
                "recommended": "English",
                "risk_off": (
                    "Disabling removes language enforcement"
                    " — model may switch languages unpredictably"
                ),
                "risk_off_zh": "關閉後模型可能隨意切換語言",
            },
        },
    },
    # ── GAIA skill behavior toggles (2.21.0) ──
    "gaia_tool_router": {
        "category": "context",
        "description": (
            "Route GAIA questions by the Annotator-Metadata Tools field "
            "(ground-truth tool list) instead of self-regex heuristic"
        ),
        "description_zh": (
            "GAIA 題依 Annotator Metadata Tools 欄位（題目設計者標的 "
            "ground-truth）自動分派 pipeline，而非自己 regex 猜 qtype"
        ),
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {},
    },
    "unified_inprocess": {
        "category": "context",
        "description": (
            "Use a single in-process Llama instance for both text and "
            "vision (KV cache shared, no HTTP :9000 hop)"
        ),
        "description_zh": (
            "單一 Llama in-process instance 共享 text+vision（KV cache "
            "共用，不走 HTTP :9000 hop）"
        ),
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {},
    },
    "gemma4_vision": {
        "category": "context",
        "description": (
            "Enable Gemma 4 native vision handler "
            "(Gemma4VisionChatHandler) in place of Qwen2.5-VL fallback"
        ),
        "description_zh": (
            "啟用 Gemma 4 native vision handler（Gemma4VisionChatHandler）"
            "取代 Qwen2.5-VL fallback"
        ),
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {},
    },
    "binary_extractor": {
        "category": "context",
        "description": (
            "Inline-extract xlsx/csv/tsv attachments into the prompt "
            "(bypasses weak-model tool-use discipline)"
        ),
        "description_zh": (
            "xlsx/csv/tsv 結構化 binary attachment 自動 extract 內容塞進 "
            "prompt（繞過弱 model 的 tool-use 紀律）"
        ),
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {},
    },
    "image_upscale_4x": {
        "category": "context",
        "description": (
            "Auto 4× LANCZOS upscale for small (<800 px) images before "
            "vision inference — music notation / compact tables benefit"
        ),
        "description_zh": (
            "<800px 小圖自動 4× LANCZOS upscale（music notation / "
            "compact tables 精度救援）"
        ),
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {
            "min_side": {
                "type": "int",
                "default": 800,
                "min": 200,
                "max": 2048,
                "recommended": 800,
                "risk_low": (
                    "min_side<200 disables upscaling for most puzzle "
                    "images — noteheads lose detail"
                ),
                "risk_high": (
                    "min_side>2048 upscales large images unnecessarily "
                    "and wastes VRAM on the mmproj encoder"
                ),
            },
            "factor": {
                "type": "int",
                "default": 4,
                "min": 2,
                "max": 8,
                "recommended": 4,
            },
        },
    },
    "gaia_music_image_upscale": {
        "category": "context",
        "description": (
            "Pre-inference 4× LANCZOS upscale gate for music notation "
            "image questions. Small (<800px) staff-notation images "
            "are upscaled before being fed to the local vision "
            "encoder, which underperforms on sub-pixel notehead "
            "detail at native resolution. Pure preprocess — no "
            "prompt injection. Renamed from legacy "
            "`bassclef_wordreverse` (2026-04-26) to remove "
            "task-specific naming; back-compat alias preserved one "
            "minor version."
        ),
        "description_zh": (
            "樂譜題的 pre-inference 4× LANCZOS 圖像放大閘。小於 "
            "800px 的五線譜圖在送入本機視覺編碼器前先放大，避免 "
            "noteheads 在原始解析度下細節丟失。純前處理 — 無 prompt "
            "注入。2026-04-26 從 legacy `bassclef_wordreverse` "
            "改名以移除題型特定命名；保留一個 minor 版本的向後相容 "
            "alias。"
        ),
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {},
    },
    "gaia_polygon_image_upscale": {
        "category": "context",
        "description": (
            "Pre-inference 4× LANCZOS upscale gate for "
            "orthogonal-polygon image questions. Small (<800px) "
            "polygon-with-labels images are upscaled before being "
            "fed to the local vision encoder, which loses small "
            "numeric labels at native resolution. Pure preprocess — "
            "no prompt injection. Renamed from legacy "
            "`polygon_counting_hint` (2026-04-26) to remove "
            "task-specific naming; back-compat alias preserved one "
            "minor version."
        ),
        "description_zh": (
            "直角多邊形題的 pre-inference 4× LANCZOS 圖像放大閘。"
            "小於 800px 的帶標籤多邊形圖在送入本機視覺編碼器前先 "
            "放大，避免小型數字標籤在原始解析度下丟失。純前處理 — "
            "無 prompt 注入。2026-04-26 從 legacy "
            "`polygon_counting_hint` 改名以移除題型特定命名；保留 "
            "一個 minor 版本的向後相容 alias。"
        ),
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {},
    },
    "gaia_music_procedure_anchor": {
        "category": "context",
        "description": (
            "L1 domain-typed anchor: inject music-notation procedure "
            "(clef line/space mnemonics + common time-units) for any "
            "question referencing musical staff / clef / noteheads. "
            "Generic textbook knowledge, no GAIA answer paths."
        ),
        "description_zh": (
            "L1 領域型 anchor：樂譜題注入 clef line/space 通用記譜 "
            "mnemonic + 常見時間單位字（decade/score/century/"
            "millennium）。內容皆為樂理通識，不含 GAIA 答案路徑"
        ),
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {},
    },
    "gaia_polygon_area_procedure_anchor": {
        "category": "context",
        "description": (
            "L1 domain-typed anchor: inject orthogonal-polygon area "
            "procedure (label-vs-decoration / boundary walk / closure "
            "check / decompose / sum / sanity check) for area-of-"
            "polygon questions. Generic geometry, no GAIA answer "
            "paths."
        ),
        "description_zh": (
            "L1 領域型 anchor：直角多邊形面積題注入通用解題程序 "
            "（標籤 vs 裝飾 / 沿邊走 / 閉合檢查 / 分解 / 加總 / "
            "sanity check）。內容皆為幾何通識"
        ),
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {},
    },
    "gaia_web_only_procedure_anchor": {
        "category": "context",
        "description": (
            "L1 domain-typed anchor: inject web-research procedure "
            "(call web_search / multi-hop strategy / Wayback "
            "fallback) for questions with no attachment + "
            "temporal/named-entity cues. Generic research strategy, "
            "no GAIA answer paths."
        ),
        "description_zh": (
            "L1 領域型 anchor：無附件且有時間/命名實體線索的 web "
            "research 題注入通用 web 研究程序（必呼 web_search / "
            "multi-hop / Wayback fallback）。內容皆為通用研究策略"
        ),
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {},
    },
    "gaia_music_sonnet_multipass": {
        "category": "context",
        "description": (
            "Force-route music-notation image questions (bass / treble "
            "clef arithmetic with spelled time-unit puzzles) from local "
            "gemma backend to Anthropic Sonnet vision (multi-pass "
            "majority vote, default N=3). Empirical N=3 sonnet on "
            "8f80e01c bass-clef returns 67% per-call PASS; majority "
            "vote stabilises to deterministic PASS. Generic infra "
            "routing — prompt content sourced from the L1 "
            "_MUSIC_NOTATION_PROCEDURE anchor, no per-task answer paths."
        ),
        "description_zh": (
            "音符圖片題（高音/低音譜號 + 時間單位拼字算術）從本地 "
            "gemma 強制改走 Anthropic Sonnet vision（多輪 majority "
            "vote，預設 N=3）。實測 8f80e01c 單次 sonnet 67% PASS；"
            "多輪投票後穩定 PASS。通用 infra 路由 — prompt 內容沿用 "
            "L1 音符 procedure anchor，不含題型專屬答案路徑"
        ),
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {
            "passes_count": {
                "type": "int",
                "default": 3,
                "min": 1,
                "max": 7,
                "recommended": 3,
                "risk_low": (
                    "passes_count<3 forfeits majority-vote noise "
                    "reduction; single-shot sonnet variance flips "
                    "the answer (~33% of the time on bass-clef)"
                ),
                "risk_high": (
                    "passes_count>5 multiplies API cost without "
                    "marginal accuracy gain on this image class"
                ),
            },
            "model": {
                "type": "str",
                "default": "claude-sonnet-4-6",
                "recommended": "claude-sonnet-4-6",
                "risk_low": (
                    "older sonnet checkpoints have weaker spatial / "
                    "musical-notation reasoning; haiku tier "
                    "insufficient for clef-mnemonic decomposition"
                ),
                "risk_high": (
                    "opus is 5x cost for marginal lift on this image "
                    "class; reserve for true Chaotic radius"
                ),
            },
        },
    },
    "gaia_polygon_sonnet_multipass": {
        "category": "context",
        "description": (
            "Force-route orthogonal-polygon area image questions from "
            "local gemma backend to Anthropic Sonnet vision (multi-pass "
            "majority vote, default N=3). Local Gemma 4 Q4_K_M mmproj "
            "under-counts on polygon decomposition (concave-corner "
            "rectangles missed); Sonnet's native multimodal encoder "
            "preserves edge geometry. Generic infra routing — relies on "
            "the L1 orthogonal-polygon procedure anchor for prompt "
            "content; no per-task answer paths."
        ),
        "description_zh": (
            "直角多邊形面積圖題從本地 gemma 強制改走 Anthropic Sonnet "
            "vision（多輪 majority vote，預設 N=3）。本地 Gemma 4 "
            "Q4_K_M mmproj 對多邊形分解低估邊數（concave-corner "
            "rectangle 漏抓）；Sonnet 原生多模態編碼器較能保留邊緣 "
            "幾何。通用 infra 路由 — prompt 內容沿用 L1 直角多邊形 "
            "程序 anchor，不含題型專屬答案路徑"
        ),
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {
            "passes_count": {
                "type": "int",
                "default": 3,
                "min": 1,
                "max": 7,
                "recommended": 3,
                "risk_low": (
                    "passes_count<3 forfeits majority-vote noise "
                    "reduction; single-shot Sonnet variance can flip "
                    "the answer"
                ),
                "risk_high": (
                    "passes_count>5 multiplies API cost without "
                    "marginal accuracy gain on this image class"
                ),
            },
            "model": {
                "type": "str",
                "default": "claude-sonnet-4-6",
                "recommended": "claude-sonnet-4-6",
                "risk_low": (
                    "older sonnet checkpoints have weaker spatial "
                    "geometry; haiku tier insufficient for "
                    "decomposition counting"
                ),
                "risk_high": (
                    "opus is 5x cost for marginal lift on this image "
                    "class; reserve for true Chaotic radius"
                ),
            },
        },
    },
    "compute_structured_plan": {
        "category": "context",
        "description": (
            "Structured-plan compute tool: agents emit a JSON plan "
            "describing a statistics or arithmetic computation, and "
            "Python executes it deterministically against named data "
            "lists. Complements ``python_exec`` (arbitrary Python "
            "expressions) with a narrower DSL that prevents arithmetic-"
            "in-head drift on multi-step computations. Plan kinds: "
            "``statistics`` (allowed fn: pstdev, stdev, pvariance, "
            "variance, mean, median, mode, geometric_mean, "
            "harmonic_mean, fmean) and ``arithmetic`` (allowed ops: "
            "add, sub, mul, div, neg, abs, pow, sum_list, mean_list, "
            "max_list, min_list). Both support ``round_decimals``. "
            "Generic — exposed via ``concinno.tools.builtin.compute`` "
            "Python API and via ``ComputeTool`` LLM-facing wrapper."
        ),
        "description_zh": (
            "結構化 plan compute tool：agent 出 JSON plan 描述 statistics "
            "或 arithmetic 計算，Python deterministic 執行。比 python_exec "
            "(任意 Python expression) 更窄，DSL 防多步算術 mid-precision "
            "drift。兩種 plan kind：statistics (fn whitelist 11 個 reduction) "
            "和 arithmetic (op whitelist 11 個運算)。都支援 round_decimals。"
            "通用 — Python API 與 ComputeTool LLM tool 兩個入口"
        ),
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {},
    },
    "gaia_quiz_scoring_hybrid": {
        "category": "context",
        "description": (
            "Hybrid Sonnet OCR + Python deterministic correctness + "
            "structured arithmetic_plan compute for image-quiz scoring "
            "questions of the form 'scored as follows: <type-A>: N₁ "
            "points / <type-B>: N₂ points … + M bonus points. "
            "How many points would the student have earned?'. Sonnet is "
            "narrowed to per-problem OCR + classification (operands, "
            "operator, student-answer string, one of 4 type tags). "
            "Python computes correctness via fractions.Fraction "
            "(deterministic equality, sign-aware, equivalent-fraction-"
            "tolerant) — avoids the v1-prototype anti-pattern of "
            "Sonnet visually accepting a wrong student answer without "
            "re-doing the math. The final score sum runs through "
            "concinno.tools.builtin.compute.execute_arithmetic_plan, "
            "dogfooding the structured-compute Skill shipped in "
            "cont'd¹². Generic for any rule-based image quiz "
            "matching the scoring-rule pattern; falls through on parse "
            "failure to the legacy single-vision multipass."
        ),
        "description_zh": (
            "Image-quiz scoring (例「scored as follows: <type-A>:"
            "N₁ points / <type-B>: N₂ points + M bonus」) "
            "的 hybrid pipeline。Sonnet 窯到 per-problem "
            "OCR + classification（operands / operator / "
            "student-answer / 4 type tag）；Python 用 "
            "fractions.Fraction 算正確答案 + 比 "
            "student answer（sign-aware、equivalent-fraction "
            "OK），避免 v1 prototype 裡 Sonnet 視"
            "覺接受 student answer 不重算的 "
            "anti-pattern。最後 sum 走 "
            "concinno.tools.builtin.compute.execute_arithmetic_plan， "
            "dogfood cont'd¹² ship 的 structured-compute "
            "Skill。通用適用任何符"
            "合計分規則的圖像 quiz；"
            "解析失敗走舊 single-vision multipass "
            "fallback"
        ),
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {
            "passes_count": {
                "type": "int",
                "default": 3,
                "min": 1,
                "max": 7,
                "recommended": 3,
                "risk_low": (
                    "passes_count<2 forfeits per-problem majority vote; "
                    "one OCR mistake on a single field (operand digit / "
                    "operator symbol / student-answer character) can flip "
                    "an entire row's correctness verdict and skew the "
                    "final score"
                ),
                "risk_high": (
                    "passes_count>5 multiplies API cost without marginal "
                    "accuracy gain; majority on full row tuples saturates "
                    "around N=3 on observed quiz layouts"
                ),
            },
            "model": {
                "type": "str",
                "default": "claude-sonnet-4-6",
                "recommended": "claude-sonnet-4-6",
                "risk_low": (
                    "older sonnet checkpoints may mis-OCR small fraction "
                    "boxes; haiku tier insufficient for the per-problem "
                    "field schema"
                ),
                "risk_high": (
                    "opus tier is more expensive without measured lift on "
                    "the narrow OCR sub-spec; reserve for true Chaotic "
                    "radius"
                ),
            },
        },
    },
    "gaia_colour_coded_numeric_hybrid": {
        "category": "context",
        "description": (
            "Hybrid OpenCV colour-mask + narrow Sonnet OCR + Sonnet "
            "text-only arithmetic for image questions where the agent "
            "must compute a statistic over numbers tagged by colour "
            "(e.g. 'average of pstdev of red numbers and stdev of "
            "green numbers'). For each colour mentioned in the "
            "question, OpenCV masks the image to that hue band only "
            "(other content blacked out) so OCR is done on a single-"
            "colour-only image — Sonnet's vision is reliable at "
            "single-colour OCR but unreliable at colour discrimination "
            "on dense grids. Per-colour N-pass OCR + per-position "
            "majority vote produces the clean number list, then a "
            "text-only Sonnet call performs the arithmetic specified "
            "by the original question (no vision burden in the "
            "compute step). Generic for any axis-aligned colour-coded "
            "numeric image; soft dependency on opencv-python; absent "
            "→ falls through to the legacy single-vision multipass."
        ),
        "description_zh": (
            "Image colour-coded numeric data (例：「red 數字 pstdev "
            "和 green 數字 stdev 的 average」) 的 hybrid pipeline。"
            "對 question 提到的每個 colour 用 OpenCV mask 對應 hue band "
            "(其他 blacked out)，narrow OCR 在單色圖做（Sonnet 對單色 "
            "OCR 可靠，dense grid 多色 discrimination 不可靠）。N-pass "
            "OCR + per-position majority vote 出乾淨數字 list，再用 "
            "text-only Sonnet call 做問題指定的算術（compute step 沒 "
            "vision 負擔）。通用適用任何 axis-aligned colour-coded "
            "numeric image；soft dep cv2，缺則 fall through 到舊 "
            "single-vision multipass"
        ),
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {
            "passes_count": {
                "type": "int",
                "default": 3,
                "min": 1,
                "max": 7,
                "recommended": 3,
                "risk_low": (
                    "passes_count<2 forfeits per-position majority "
                    "vote; one OCR mistake on a single number can "
                    "skew the entire downstream statistic"
                ),
                "risk_high": (
                    "passes_count>5 multiplies API cost without "
                    "marginal accuracy gain; per-position mode "
                    "saturates around N=3 on observed image classes"
                ),
            },
            "model": {
                "type": "str",
                "default": "claude-sonnet-4-6",
                "recommended": "claude-sonnet-4-6",
                "risk_low": (
                    "older sonnet checkpoints may misread small or "
                    "stylised digits; haiku tier insufficient for "
                    "dense-grid OCR"
                ),
                "risk_high": (
                    "opus tier is more expensive without measured lift "
                    "on the narrow OCR sub-spec; reserve for true "
                    "Chaotic radius"
                ),
            },
        },
    },
    "gaia_polygon_opencv_hybrid": {
        "category": "context",
        "description": (
            "Hybrid OpenCV + narrow Sonnet OCR + Python shoelace solver "
            "for orthogonal polygon area image questions. cv2.findContours "
            "extracts polygon vertices in pixel coords (ground truth the "
            "LLM cannot fabricate); a narrow Anthropic vision call asks "
            "the model to label each edge by index with the visible "
            "numeric value nearest to the edge midpoint (OCR + spatial "
            "matching only — no decomposition, no arithmetic). Python "
            "walks the polygon in unit space using (label, direction) "
            "pairs, verifies closure against the OpenCV-anchored vertex "
            "structure, and computes signed area via shoelace formula. "
            "Generic for any axis-aligned polygon area question with "
            "labelled side lengths — schematic non-uniformity (image "
            "not drawn-to-scale) is tolerated because the algorithm "
            "uses LABEL values for shoelace, not pixel distances. "
            "Soft dependency on opencv-python; absent → fall through "
            "to the structured-JSON multipass below."
        ),
        "description_zh": (
            "Orthogonal polygon area 圖題的 hybrid pipeline。OpenCV "
            "cv2.findContours 抽 polygon 頂點 pixel 座標（LLM 無法偽造的"
            "ground truth）；narrow Anthropic vision call 要 model 對每"
            "edge 配最近 label（純 OCR + spatial matching，不 decomp 不"
            "算術）；Python 在 unit space 走 polygon 用 shoelace 算面積，"
            "closure 用 OpenCV vertex structure 錨定，跳脫 free-form "
            "structured multipass 的「closure-valid != structural-truth」"
            "失敗模式。schematic 圖非按比例也能算對，因為用 LABEL（非 "
            "pixel）做 shoelace。soft dep cv2，缺則 fall through 到下面 "
            "structured multipass。通用適用任何 axis-aligned polygon area "
            "+ numeric label 題型"
        ),
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {
            "passes_count": {
                "type": "int",
                "default": 3,
                "min": 1,
                "max": 7,
                "recommended": 3,
                "risk_low": (
                    "passes_count<2 forfeits the retry-on-closure-fail "
                    "buffer; one wrong OCR (e.g. confusing 1 with 1.5) "
                    "kills the whole pipeline"
                ),
                "risk_high": (
                    "passes_count>5 multiplies API cost without marginal "
                    "lift; closure check filters wrong OCR within 2-3 "
                    "tries on observed image classes"
                ),
            },
            "model": {
                "type": "str",
                "default": "claude-sonnet-4-6",
                "recommended": "claude-sonnet-4-6",
                "risk_low": (
                    "older sonnet checkpoints have weaker JSON "
                    "instruction-following on the narrow per-edge label "
                    "schema"
                ),
                "risk_high": (
                    "opus tier is more expensive without measured lift "
                    "on the narrow OCR sub-spec; reserve for true "
                    "Chaotic radius"
                ),
            },
        },
    },
    "gaia_polygon_structured_multipass": {
        "category": "context",
        "description": (
            "Closure-validated structured-JSON multipass for orthogonal "
            "polygon area image questions. Asks Sonnet/Opus for a strict "
            "JSON object {labels_visible, rectangles[], edge_sums, "
            "computed_area}; Python verifies horizontal_right == "
            "horizontal_left and vertical_down == vertical_up closure, "
            "then re-derives area from sum(width*height). Only passes "
            "whose closure holds AND whose self-claimed area matches the "
            "re-derived area within 0.5 are kept; median-of-valid is "
            "returned. Preferred over the legacy free-form polygon "
            "multipass because schematic geometry diagrams are NOT "
            "drawn-to-scale (pixel-counting fails) and arithmetic-in-"
            "head is the recurring failure sub-spec — closure check + "
            "Python re-sum offload that burden. Falls through to the "
            "legacy free-form multipass on zero valid passes. Generic "
            "for any axis-aligned polygon area question."
        ),
        "description_zh": (
            "Orthogonal polygon area 圖題的結構化 JSON multipass + "
            "closure 驗證。要 Sonnet/Opus 出嚴格 JSON {labels_visible, "
            "rectangles[], edge_sums, computed_area}；Python 驗 "
            "horizontal_right==horizontal_left 與 vertical_down=="
            "vertical_up 閉合 + 用 sum(w*h) 重算面積。closure 通過且 "
            "claimed 面積對齊重算（±0.5）才採；取 valid pass 中位數。"
            "比舊 free-form multipass 強，因 schematic 圖非按比例（像 "
            "素計數會錯），算術在頭內做是反覆 fail 的子規格 — closure "
            "驗證 + Python 重算把這負擔卸下。zero valid 才 fall-through "
            "到舊 multipass。通用適用任何 axis-aligned polygon 面積題"
        ),
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {
            "passes_count": {
                "type": "int",
                "default": 5,
                "min": 1,
                "max": 9,
                "recommended": 5,
                "risk_low": (
                    "passes_count<3 has too few candidates for the "
                    "closure filter to recover a valid majority when "
                    "the model decomposes inconsistently"
                ),
                "risk_high": (
                    "passes_count>7 multiplies API cost without "
                    "marginal accuracy lift; once a closure-valid pass "
                    "lands the area is already deterministic"
                ),
            },
            "model": {
                "type": "str",
                "default": "claude-sonnet-4-6",
                "recommended": "claude-sonnet-4-6",
                "risk_low": (
                    "older sonnet checkpoints have weaker JSON "
                    "instruction-following and may drop schema fields, "
                    "starving the closure filter"
                ),
                "risk_high": (
                    "opus tier is more expensive without a measured lift "
                    "on closure-pass rate for this image class; reserve "
                    "for true Chaotic radius"
                ),
            },
        },
    },
    "gaia_web_only_force_anthropic": {
        "category": "context",
        "description": (
            "Force-route web-only questions (no attachment + temporal "
            "/ named-entity cues) from local gemma backend to "
            "Anthropic Sonnet. Local Gemma reliably hallucinates "
            "answers instead of invoking Action: web_search(...); "
            "Sonnet has native web_search_20250305 tool. Generic "
            "infra routing, no GAIA answer paths."
        ),
        "description_zh": (
            "web-only 題（無附件 + 時間/命名實體線索）從本地 gemma "
            "強制改走 Anthropic Sonnet。本地 Gemma 不會穩定呼 "
            "Action: web_search(...) 而會幻覺答案；Sonnet 有原生 "
            "web_search_20250305 tool。通用 infra 路由，無 GAIA "
            "答案路徑"
        ),
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {},
    },
    "gaia_web_fetch_full": {
        "category": "context",
        "description": (
            "Expose the Playwright-backed web_fetch_full tool to the "
            "GAIA gather loop alongside web_search. web_fetch_full "
            "renders one URL in headless chromium and returns rendered "
            "text + a full-page PNG screenshot, enabling multi-hop "
            "questions whose answer depends on what is visible on the "
            "page (small text in a background image, tombstone, chart "
            "label) rather than the search-engine summary. Generic "
            "infra capability, no GAIA answer paths; falls back to the "
            "web_search-only path when disabled."
        ),
        "description_zh": (
            "在 GAIA gather loop 同時暴露 Playwright 版 web_fetch_full "
            "工具。回傳渲染後的純文字 + 整頁 PNG 截圖，讓 multi-hop "
            "題（答案藏在頁面圖片裡的小字 / 墓碑 / 圖表標籤）不再被 "
            "search summary 吃掉。通用 infra 能力，無 GAIA 答案路徑；"
            "關掉時自動 fallback 回只有 web_search 的路徑"
        ),
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {},
    },
    "gaia_web_fetch_full_multimodal": {
        "category": "context",
        "description": (
            "Attach the web_fetch_full screenshot to the next agent "
            "turn as an Anthropic multimodal image content block when "
            "the active backend tier is Sonnet or Opus. Without this, "
            "the model only sees text mentioning the screenshot path "
            "and degenerates trying to PIL-print the file (verified "
            "regression on 624cbf11 Ben & Jerry's flavor graveyard). "
            "Generic infra capability — only the routing decides; the "
            "screenshot is always captured by web_fetch_full itself."
        ),
        "description_zh": (
            "當 backend tier = Sonnet / Opus 時，把 web_fetch_full 的"
            "截圖以 Anthropic 多模態 image content block 附到下一輪 "
            "agent message。沒開時，模型只看到 screenshot path 提到"
            "的文字觀察就 degenerate（624cbf11 graveyard 已驗證 "
            "regression）。通用 infra 能力 — 只決定路由，截圖本身"
            "由 web_fetch_full 一律抓"
        ),
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {},
    },
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
    "skill_emergence_guard": {
        "category": "behavioral",
        "enabled": False,  # default OFF per 4.0.0 SEMVER-MAJOR opt-in policy
        "ziq_autotunable": True,
        "cosmetic": False,
        "description": (
            "Auto-propose Claude Code Skill drafts from observed tool-call "
            "patterns — repeated workflows, error→success recoveries, and "
            "user-correction signals. Drafts land in "
            "~/.concinno/skill_drafts for the user to accept or reject; "
            "the guard never installs a Skill directly."
        ),
        "description_zh": (
            "從觀察到的工具呼叫模式自動提議 Claude Code Skill 草稿 —— "
            "重複工作流、錯誤→成功修復、用戶糾正訊號。草稿寫到 "
            "~/.concinno/skill_drafts 由使用者接受或拒絕；"
            "本 guard 永遠不直接安裝 Skill。"
        ),
        "params": {
            "max_auto_skills_per_day": {
                "type": "int",
                "default": 5,
                "min": 1,
                "max": 20,
                "recommended": 5,
            },
            "min_pattern_occurrences": {
                "type": "int",
                "default": 3,
                "min": 2,
                "max": 10,
                "recommended": 3,
            },
            "cooldown_hours": {
                "type": "float",
                "default": 2.0,
                "min": 0.5,
                "max": 24.0,
                "recommended": 2.0,
            },
            "draft_retention_days": {
                "type": "int",
                "default": 30,
                "min": 7,
                "max": 90,
                "recommended": 30,
            },
        },
        "recommended": False,
        "severity": "minor",
    },
    # ── 4.5.0 W3 — Token Audit Autopilot ──
    #
    # Per-session token overhead audit (skills / MCP / sub-agents /
    # system floor) with a ZIQ FTRL-driven advisor for stale skills.
    # Default-OFF per 4.0.0 opt-in policy.
    "token_audit_autopilot": {
        "category": "observability",
        "enabled": False,
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
        "enabled": False,
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
}


# ── Back-compat aliases (legacy feature names → canonical) ──────
#
# Each entry is ``<old_name>: <canonical_name>``. When the config layer
# (``concinno.core.config.Config.feature``) sees a user-set value
# under ``<old_name>`` it transparently treats it as a value on
# ``<canonical_name>`` and emits a one-time stderr deprecation warning
# of the form
# ``concinno: feature '<old_name>' renamed to '<canonical_name>' (drops 2026-07)``.
#
# Drop policy: aliases stay for one minor version after introduction
# so existing user configs keep working through one upgrade cycle.
LEGACY_ALIASES: dict[str, str] = {
    # 2026-04-26 — leakage-suspect names removed; behavior unchanged
    # (these always only toggled the LANCZOS upscale gate, no prompt
    # injection lived under these flags).
    "bassclef_wordreverse": "gaia_music_image_upscale",
    "polygon_counting_hint": "gaia_polygon_image_upscale",
}


def resolve_alias(name: str) -> str:
    """Map a legacy feature name to its canonical replacement.

    Returns ``name`` unchanged when no alias exists. Pure lookup —
    deprecation warnings are emitted by the config layer (which has
    the per-session dedup state), not here.
    """
    return LEGACY_ALIASES.get(name, name)


# ── 2.36.0a1 schema-extension constants ────────────────────────

#: Severity tiers, ordered low->high. Index used for invariant comparisons.
_SEVERITY_ORDER: tuple[str, ...] = ("none", "minor", "major", "critical")


def get_severity_tier(name: str) -> str:
    """Return the ``severity_if_off`` tier for ``name``.

    Falls back to ``"none"`` for unknown features or entries that did
    not migrate to the 2.36.0a1 schema. Drives the GUI 4-tier confirm
    UX (none -> direct toggle / minor -> info banner / major -> 2-click
    warn / critical -> typed-feature-name confirm + audit log).
    """
    meta = FEATURE_META.get(name) or {}
    sev = meta.get("severity_if_off", "none")
    return sev if sev in _SEVERITY_ORDER else "none"


def _severity_at_or_above(name: str, threshold: str) -> bool:
    """True iff ``name`` has ``severity_if_off >= threshold``."""
    sev = get_severity_tier(name)
    try:
        return _SEVERITY_ORDER.index(sev) >= _SEVERITY_ORDER.index(threshold)
    except ValueError:
        return False


def _audit_log_path() -> Path:
    """Where high-severity feature mutations are recorded.

    Append-only, line-delimited; one record per mutation. Path is
    stable across versions so external tooling can tail it. Returns
    a :class:`pathlib.Path` (lazily imported to keep the module's
    zero-dep top-level namespace).
    """
    from pathlib import Path

    return Path.home() / ".concinno" / "critical_changes.log"


def _record_critical_change(
    name: str, key: str, value: Any, *, origin: tuple[str, ...],
) -> None:
    """Append a record to the critical-changes audit log.

    Fail-soft — observability only, never blocks ``set_feature`` if the
    audit log can't be written (filesystem read-only / disk full /
    permission denied). Format::

        <ISO-8601 UTC>  <severity>  <feature>.<key> -> <value>  origin=<...>
    """
    import datetime as _dt

    path = _audit_log_path()
    sev = get_severity_tier(name)
    timestamp = _dt.datetime.now(tz=_dt.timezone.utc).isoformat(timespec="seconds")
    origin_str = ":".join(origin) if origin else "manual"
    line = f"{timestamp}  {sev}  {name}.{key} -> {value!r}  origin={origin_str}\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        # Observability must never break set_feature.
        pass


# ── Public API ────────────────────────────────────────────


def _merge_feature_meta(
    sources: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    """Merge one feature's meta across the three layers.

    Precedence by field (per 2.31.0 spec v2 amendment A4):

    * ``description`` / ``category`` / ``cosmetic`` / ``ziq_autotunable``
      / ``description_zh``: highest-precedence source wins (shipped >
      user > plugin).
    * ``enabled``: low-to-high cascade (plugin default -> user override
      -> shipped override). Library integrity wins for the final gate.
    * ``params``: per-param merge. Shipped params are the baseline and
      define the ``type`` / ``default`` / ``min`` / ``max``. User may
      override the effective ``value`` (but not redefine the schema).
      Plugin may introduce new params not in shipped.

    Returns ``(merged_meta, origin_label)``. ``origin_label`` is a
    single source name for the 1-source case, else a
    ``"merged:shipped+user"``-style label.
    """
    shipped = sources.get("official")
    user = sources.get("user")
    plugin = sources.get("plugin")

    merged: dict[str, Any] = {}

    # High-precedence-wins fields.
    for field in ("description", "description_zh", "category",
                  "cosmetic", "ziq_autotunable"):
        for layer in (shipped, user, plugin):
            if layer is not None and field in layer:
                merged[field] = layer[field]
                break

    # enabled cascade: plugin default -> user override -> shipped override.
    enabled = True  # ultimate default
    for layer in (plugin, user, shipped):
        if layer is not None and "enabled" in layer:
            enabled = layer["enabled"]
    merged["enabled"] = enabled

    # params per-param merge.
    shipped_params = dict(shipped.get("params", {})) if shipped else {}
    plugin_params = dict(plugin.get("params", {})) if plugin else {}
    user_params = dict(user.get("params", {})) if user else {}
    merged_params: dict[str, Any] = {}
    all_param_names = set(shipped_params) | set(plugin_params) | set(user_params)
    for pname in sorted(all_param_names):
        if pname in shipped_params:
            # Shipped defines schema; user may override value fields.
            p = dict(shipped_params[pname])
            if pname in user_params:
                for k, v in user_params[pname].items():
                    if k in ("default", "value", "recommended"):
                        p[k] = v
            merged_params[pname] = p
        elif pname in plugin_params:
            p = dict(plugin_params[pname])
            if pname in user_params:
                for k, v in user_params[pname].items():
                    if k in ("default", "value", "recommended"):
                        p[k] = v
            merged_params[pname] = p
        else:
            merged_params[pname] = dict(user_params[pname])
    merged["params"] = merged_params

    # Preserve schema_version on plugin-originated rows for downstream
    # GUI rendering / forward-compat warnings.
    if plugin is not None and "schema_version" in plugin:
        merged["schema_version"] = plugin["schema_version"]

    # Origin label. "official" is the legacy backward-compat name for
    # the shipped layer (pre-2.31.0 used this label). Keep it to avoid
    # breaking consumers that compare origin strings.
    present = [name for name in ("official", "user", "plugin") if name in sources]
    if len(present) == 1:
        origin = present[0]
    else:
        origin = "merged:" + "+".join(present)
    # Plugin origin includes the package name for GUI surfacing.
    if "plugin" in sources and "_plugin_pkg" in sources:
        pkg = sources["_plugin_pkg"]
        if origin == "plugin":
            origin = f"plugin:{pkg}"
        else:
            origin = origin + f":{pkg}"

    return merged, origin


def iter_all_features_with_origin() -> list[tuple[str, dict[str, Any], str]]:
    """Yield every feature known to this process as
    ``(name, meta, origin)`` tuples.

    Three-layer merge per 2.31.0 spec v2 amendment A4:

    * ``"shipped"`` -- entries from :data:`FEATURE_META` (always
      wins on core schema / library-integrity fields)
    * ``"user"`` -- entries from
      ``~/.concinno/user_features.json`` (may override ``enabled``
      and param values; cannot redefine shipped schema)
    * ``"plugin:<pkg>"`` -- entries from installed
      ``concinno-skills-*`` packages via the ``concinno.features``
      entry-points group (lowest precedence; user-features override
      plugin defaults by name collision)

    ``origin`` labels:

    * Single layer: ``"shipped"`` / ``"user"`` / ``"plugin:<pkg>"``
    * Multi-layer merged: ``"merged:shipped+user"`` /
      ``"merged:user+plugin:<pkg>"`` etc.

    When two sources collide on a name
    :func:`concinno.user_features.record_collision` is called so the
    GUI's collision-bar can surface the shadow.

    Originally added in 2.30.1 (shipped+user only); plugin layer
    added in 2.31.0.
    """
    try:
        from concinno.user_features import (
            clear_collision_warnings,
            load_user_features,
            record_collision,
        )
        clear_collision_warnings()
        user_feats = load_user_features()
    except Exception:
        user_feats = {}
        record_collision = None  # type: ignore[assignment]

    # Plugin layer (2.31.0). Import is lazy + failure-tolerant so a
    # broken plugin does not take down feature enumeration.
    plugin_by_name: dict[str, tuple[dict[str, Any], str]] = {}
    try:
        from concinno.plugins import iter_valid_feature_plugins

        for name, meta, pkg in iter_valid_feature_plugins():
            if name in plugin_by_name:
                # Same name from two plugin packages — first-wins,
                # mirror ToolRegistry.load_plugins behaviour.
                if record_collision is not None:
                    record_collision(
                        name,
                        f"plugin collision: also in package {pkg!r}",
                    )
                continue
            plugin_by_name[name] = (meta, pkg)
    except Exception:
        plugin_by_name = {}

    shipped_names = set(FEATURE_META.keys())
    user_names = set(user_feats.keys())
    plugin_names = set(plugin_by_name.keys())
    all_names = shipped_names | user_names | plugin_names

    rows: list[tuple[str, dict[str, Any], str]] = []
    for name in sorted(all_names):
        sources: dict[str, Any] = {}
        if name in shipped_names:
            sources["official"] = FEATURE_META[name]
        if name in user_names:
            sources["user"] = user_feats[name]
        if name in plugin_names:
            plugin_meta, pkg = plugin_by_name[name]
            sources["plugin"] = plugin_meta
            sources["_plugin_pkg"] = pkg

        # Emit collisions (anything more than one real layer).
        real_layers = [k for k in ("official", "user", "plugin") if k in sources]
        if len(real_layers) > 1 and record_collision is not None:
            winner = real_layers[0]  # official > user > plugin by iter order
            shadowed = real_layers[1:]
            for s in shadowed:
                tag = s if s != "plugin" else f"plugin:{sources.get('_plugin_pkg', '?')}"
                record_collision(
                    name,
                    f"{tag} shadowed by {winner} (merged fields preserved)",
                )

        merged_meta, origin = _merge_feature_meta(sources)
        rows.append((name, merged_meta, origin))

    return rows


def list_features(lang: str = "en") -> list[dict[str, Any]]:
    """List all features (shipped + user-registered) with current
    config values."""
    try:
        from concinno.core.config import get_config

        cfg = get_config()
    except Exception:
        cfg = None

    result = []
    for name, meta, origin in iter_all_features_with_origin():
        desc = meta.get(f"description_{lang}", meta["description"])
        current = cfg.feature_all(name) if cfg else {}
        result.append({
            "name": name,
            "category": meta["category"],
            "description": desc,
            "enabled": current.get("enabled", meta_enabled_default(name)),
            "source": origin,
            "params": {
                k: {
                    "value": current.get(k, v.get("default")),
                    "default": v.get("default"),
                    "recommended": v.get("recommended"),
                }
                for k, v in meta.get("params", {}).items()
            },
        })
    return result


def get_feature(name: str, lang: str = "en") -> Optional[dict[str, Any]]:
    """Get feature info with full risk metadata."""
    meta = FEATURE_META.get(name)
    if not meta:
        return None

    try:
        from concinno.core.config import get_config

        cfg = get_config()
        current = cfg.feature_all(name)
    except Exception:
        current = {}

    desc = meta.get(f"description_{lang}", meta["description"])
    params = {}
    for k, v in meta.get("params", {}).items():
        risk_suffix = f"_{lang}" if lang != "en" else ""
        params[k] = {
            "value": current.get(k, v.get("default")),
            **v,
        }
        # Add localized risk text if available
        for risk_key in ("risk_low", "risk_high", "risk_off"):
            localized = v.get(f"{risk_key}{risk_suffix}")
            if localized:
                params[k][risk_key] = localized

    return {
        "name": name,
        "category": meta["category"],
        "description": desc,
        "enabled": current.get("enabled", meta_enabled_default(name)),
        "params": params,
    }


def _validate_numeric(
    name: str, key: str, value: Any, param: dict[str, Any], ptype: str,
) -> list[str]:
    """Validate int or float param. Returns warnings list."""
    expected = int if ptype == "int" else (int, float)
    if not isinstance(value, expected):
        return [f"{name}.{key} must be {ptype}, got {type(value).__name__}"]
    if ptype == "float":
        value = float(value)
    warnings: list[str] = []
    if "min" in param and value < param["min"]:
        warnings.append(
            f"⚠ {name}.{key}={value} below minimum {param['min']}. "
            f"{param.get('risk_low', '')}"
        )
    elif "max" in param and value > param["max"]:
        warnings.append(
            f"⚠ {name}.{key}={value} above maximum {param['max']}. "
            f"{param.get('risk_high', '')}"
        )
    if ptype == "int":
        rec = param.get("recommended")
        if rec is not None and value != rec:
            warnings.append(
                f"ℹ Recommended: {name}.{key}={rec} (you set {value})"
            )
    return warnings


def _validate_str_or_bool(
    name: str, key: str, value: Any, param: dict[str, Any], ptype: str,
) -> list[str]:
    """Validate str or bool param. Returns warnings or errors."""
    if ptype == "str":
        if not isinstance(value, str):
            return [f"{name}.{key} must be str, got {type(value).__name__}"]
        options = param.get("options")
        if options and value not in options:
            return [f"{name}.{key}={value!r} not in {options}"]
    elif ptype == "bool":
        if not isinstance(value, bool):
            return [f"{name}.{key} must be bool, got {type(value).__name__}"]
        if not value and "risk_off" in param:
            return [f"⚠ {param['risk_off']}"]
    return []


def validate_value(name: str, key: str, value: Any) -> list[str]:
    """Validate a value change and return risk warnings (empty = safe)."""
    meta = FEATURE_META.get(name)
    if not meta:
        return [f"Unknown feature: {name}"]

    if key == "enabled":
        if not isinstance(value, bool):
            return [f"enabled must be bool, got {type(value).__name__}"]
        return []

    # GUI-managed sidecar keys (2.23.0+):
    #   ``ziq_opt_out``      — feature-level ZIQ toggle
    #   ``<param>__pinned``  — per-param manual lock (ZIQ skip)
    # Both are bool, neither lives in FEATURE_META params — accept them
    # unconditionally so the GUI can write them without every feature
    # needing a schema update.
    if key == "ziq_opt_out" or key.endswith("__pinned"):
        if not isinstance(value, bool):
            return [f"{key} must be bool, got {type(value).__name__}"]
        return []

    param = meta.get("params", {}).get(key)
    if not param:
        return [f"Unknown param: {name}.{key}"]

    ptype = param.get("type", "int")
    if ptype in ("int", "float"):
        return _validate_numeric(name, key, value, param, ptype)
    return _validate_str_or_bool(name, key, value, param, ptype)


def set_feature(
    name: str,
    key: str,
    value: Any,
    *,
    force: bool = False,
    origin: tuple[str, ...] = ("manual",),
) -> list[str]:
    """Set a feature config value. Returns risk warnings.

    Args:
        name: FEATURE_META key.
        key: Param name (or ``"enabled"``).
        value: New value.
        force: When False and validation produces warnings, change is NOT
            applied. When True, applied regardless of warnings.
        origin: Provenance tuple recorded in the preset-cascade origin
            sidecar (``~/.concinno/preset_origins.json``) — examples:
            ``("manual",)``, ``("preset", "benchmark")``,
            ``("ziq", "autotune", "full")``. Wired for narrower-scope v4
            so ``concinno preset show`` can explain why a value is
            what it is.

    Returns:
        Risk-warning strings (empty on safe change).
    """
    warnings = validate_value(name, key, value)

    if warnings and not force:
        warnings.append("→ Use force=True to apply anyway.")
        return warnings

    try:
        from concinno.core.config import get_config

        cfg = get_config()
        cfg.set_feature(name, key, value)
    except Exception as e:
        return [f"Failed to set: {e}"]

    # Record origin sidecar for preset-cascade inspection. Fail-soft —
    # origin tracking is observability, not gating.
    try:
        from concinno.preset_cascade import _record_origin

        _record_origin(name, key, origin)
    except Exception:  # pragma: no cover — optional sidecar
        pass

    # 2.36.0a1: append to ~/.concinno/critical_changes.log when the
    # feature carries severity_if_off >= "major". Drives the redteam-
    # mandated audit trail for GUI-initiated config mutations of
    # high-impact gates (R#6 acceptance per commander verdict).
    if _severity_at_or_above(name, "major"):
        _record_critical_change(name, key, value, origin=origin)

    return warnings


def list_autotunable() -> list[str]:
    """Return FEATURE_META names that ZIQ may auto-tune (non-cosmetic).

    Used by :class:`concinno.ziq_autotune_loop.ZIQAutoTuneLoop.tick` to
    short-circuit the walk over safety-only / cosmetic entries.
    """
    return sorted(
        name
        for name, meta in FEATURE_META.items()
        if meta.get("ziq_autotunable") and not meta.get("cosmetic", False)
    )


# ── Preset Profiles ──────────────────────────────────────

PROFILES: dict[str, dict[str, Any]] = {
    "minimal": {
        "description": "Lightweight — core guards only, no ARBITER, no skill routing",
        "settings": {
            "guard_count": 7,
            "arbiter": False,
            "skill_routing": False,
            "silent": False,
        },
    },
    "standard": {
        "description": "Full guards, skill routing enabled, no ARBITER overhead",
        "settings": {
            "guard_count": 55,
            "arbiter": False,
            "skill_routing": True,
            "silent": False,
        },
    },
    "paranoid": {
        "description": "Maximum safety — all guards + ARBITER post-check",
        "settings": {
            "guard_count": 55,
            "arbiter": True,
            "skill_routing": True,
            "silent": False,
        },
    },
    "competition": {
        "description": "Competition mode — all guards, ARBITER, eager skill loading, silent",
        "settings": {
            "guard_count": 55,
            "arbiter": True,
            "skill_routing": "eager",
            "silent": True,
        },
    },
    "ziq_adaptive": {
        "description": (
            "ZIQ-routed — features dynamically enabled per-request via "
            "ZIQFeatureRouter (α_t tier + ctx budget)"
        ),
        "settings": {
            "guard_count": 55,
            "arbiter": "ziq_routed",
            "skill_routing": True,
            "silent": False,
            "dynamic_routing": True,
        },
    },
}


# ── Routing Policy Convention ────────────────────────────
#
# Features may declare a ``routing_policy`` key in FEATURE_META to control
# how ZIQFeatureRouter (see ``concinno.ziq_router``) treats them:
#
#   "always_on"     → ignore ZIQ, always enabled (critical safety gates)
#   "always_off"    → ignore ZIQ, always disabled (opt-in only)
#   "ziq_routed"    → enabled based on α_t tier + ctx budget  (DEFAULT)
#   "user_override" → respect the persisted config value verbatim
#
# Absence of the key is equivalent to ``"ziq_routed"``. The router returns
# a RoutingDecision with a ``reasons`` audit trail per feature.

ROUTING_POLICY_VALUES = frozenset({
    "always_on", "always_off", "ziq_routed", "user_override",
})


def get_routing_policy(name: str) -> str:
    """Return the effective routing_policy for a feature (default ziq_routed)."""
    meta = FEATURE_META.get(name)
    if not meta:
        return "ziq_routed"
    policy = meta.get("routing_policy", "ziq_routed")
    return policy if policy in ROUTING_POLICY_VALUES else "ziq_routed"


def list_with_routing(lang: str = "en") -> list[dict[str, Any]]:
    """Like ``list_features`` but also includes effective ``routing_policy``."""
    features = list_features(lang=lang)
    for f in features:
        f["routing_policy"] = get_routing_policy(f["name"])
    return features


def list_profiles() -> dict[str, str]:
    """Return {name: description} for all available profiles."""
    return {k: v["description"] for k, v in PROFILES.items()}


def get_active_profile() -> str:
    """Return the currently active profile name.

    Resolution order (first hit wins):
      1. ``CONCINNO_PROFILE`` environment variable (if it names a
         known profile)
      2. ``active_profile`` key in the live config file (set by
         ``apply_profile``)
      3. Fallback to ``"standard"``

    Fail-soft: any exception is swallowed and ``"standard"`` returned.
    This helper drives the competition-mode advisory silencer in
    ``GuardPipeline`` — it must never raise, because raising inside
    the hook path would break every tool call.
    """
    import os as _os

    try:
        env_name = _os.environ.get("CONCINNO_PROFILE", "").strip()
        if env_name and env_name in PROFILES:
            return env_name
    except Exception:
        pass

    try:
        from concinno.core.config import get_config

        cfg = get_config()
        active = cfg.raw("active_profile", "")
        if isinstance(active, str) and active in PROFILES:
            return active
    except Exception:
        pass

    return "standard"


def apply_profile(name: str) -> list[str]:
    """Apply a preset profile. Returns list of changes made.

    Individual feature toggles can still override after applying a profile.
    Use ``/hook profile <name>`` to switch profiles.
    """
    if name not in PROFILES:
        return [f"Unknown profile: {name}. Available: {', '.join(PROFILES)}"]

    profile = PROFILES[name]
    changes: list[str] = []

    try:
        from concinno.core.config import get_config

        cfg = get_config()
        # Write directly to cc_config.json (not through set_feature validation
        # — "profile" is a meta-concept above individual features)
        cfg.update_file("active_profile", name)
        cfg.update_file("profile_settings", profile["settings"])
        for key, value in profile["settings"].items():
            changes.append(f"  {key} = {value}")
    except Exception as e:
        return [f"Failed to apply profile: {e}"]

    return [f"Applied profile '{name}': {profile['description']}"] + changes


# ── Per-feature toggle profiles (4.2.x, set-profile shortcut) ────
#
# These profiles toggle individual features in ``DEFAULT_OFF_4_0_0``
# en-masse, so an operator restoring strict mode after the 4.0.0
# permissive baseline can do it in one command instead of running
# ``concinno config set features.<name>.enabled true`` 27 times.
#
# Distinct from the legacy :data:`PROFILES` dict above — that one
# stores high-level settings (guard_count / arbiter / skill_routing
# / silent / dynamic_routing) under a meta ``profile_settings`` key,
# while this one writes per-feature ``enabled`` flags through
# :meth:`Config.set_feature` so the existing 6-source resolution
# chain (env > user > project > FEATURE_META) keeps working.
#
# Each profile maps a name to two specs (``enable`` / ``disable``).
# Each spec is either a frozenset of feature names, or the string
# sentinel ``"DEFAULT_OFF_4_0_0"`` which expands to the live frozenset
# at apply time. The ``permissive`` profile *disables* every feature
# in ``DEFAULT_OFF_4_0_0`` so a previously-applied ``strict`` can be
# rolled back in one command.
#
# ZIQ note: profile choice is operator preference, not auto-tunable
# (cosmetic=False, ziq_autotunable=False). ZIQ does not auto-flip
# operator-applied profiles; user明示 wins.

# ── Profile fail-mode override defaults (4.3.0 — Plan B Step 1) ────
#
# Each profile carries a ``fail_mode_overrides: dict[feature_name,
# FailMode]`` that the policy gate consults when a feature reports a
# failure. Resolution chain (later wins):
#
#   1. profile default (this dict)
#   2. user override in ``~/.concinno/<feature>.json::fail_mode``
#   3. env ``CONCINNO_<FEATURE>_FAIL_MODE``
#   4. ZIQ auto-tune (future — registered ``ziq_autotunable=False``
#      for 4.3.0; flips to True once outcome bus signals are wired)
#
# A feature absent from a profile's overrides falls through to the
# profile's "default for everything else" implicit category — encoded
# in the docstring per profile, not the dict, because the four
# profiles disagree on what "everything else" should mean (lite =
# silent, mainstream = warn, strict = warn+log, paranoid = hard_deny).
# :func:`get_fail_mode` materialises that fallback.

# 4.3.0 schema additions are layered on top of the existing 3 profile
# names (strict / permissive / dev) — `permissive` is now an alias to
# `lite` for backward-compat (3-month migration window per CHANGELOG).

FEATURE_TOGGLE_PROFILES: dict[str, dict[str, Any]] = {
    "lite": {
        "description": (
            "Lite default (4.3.0+) — minimal blocking, only "
            "DestructionGuard hard-denies. Other guards default to "
            "silent / warn. Aliased from ``permissive``; intended for "
            "senior-dev daily driver and pre-shipping prototypes."
        ),
        "enable": frozenset(),
        "disable": "DEFAULT_OFF_4_0_0",
        "fail_mode_overrides": {
            "destruction_guard": "hard_deny",
            "butterfly_guard": "warn",
        },
        "fail_mode_default": "silent",
    },
    "mainstream": {
        "description": (
            "Mainstream profile (4.3.0+) — production-ready balance. "
            "Hard-deny on data-loss + secrets, warn+log on quality "
            "gates, warn on the rest."
        ),
        "enable": frozenset(),
        "disable": "DEFAULT_OFF_4_0_0",
        "fail_mode_overrides": {
            "destruction_guard": "hard_deny",
            "pii_guard": "warn",
            "butterfly_guard": "warn+log",
        },
        "fail_mode_default": "warn",
    },
    "strict": {
        "description": (
            "Strict profile (pre-4.0.0 paranoid baseline) — enable "
            "all 27 default-off guards. Most checks warn+log, "
            "destruction / pii / deserialize hard-deny."
        ),
        "enable": "DEFAULT_OFF_4_0_0",  # sentinel — expanded at apply time
        "disable": frozenset(),
        "fail_mode_overrides": {
            "destruction_guard": "hard_deny",
            "pii_guard": "hard_deny",
            "deserialize_guard": "hard_deny",
            "circuit_breaker_guard": "hard_deny",
        },
        "fail_mode_default": "warn+log",
    },
    "paranoid": {
        "description": (
            "Paranoid profile (4.3.0+) — every guard hard-denies "
            "except cosmetic/observability features which stay warn. "
            "Intended for security-sensitive deployments and CI."
        ),
        "enable": "DEFAULT_OFF_4_0_0",
        "disable": frozenset(),
        "fail_mode_overrides": {
            "destruction_guard": "hard_deny",
            "pii_guard": "hard_deny",
            "deserialize_guard": "hard_deny",
            "circuit_breaker_guard": "hard_deny",
            "butterfly_guard": "hard_deny",
        },
        "fail_mode_default": "hard_deny",
    },
    "permissive": {
        "description": (
            "DEPRECATED — alias for ``lite`` since 4.3.0. Will be "
            "removed in 5.0.0. Existing CLI/tests keep working "
            "transparently via :func:`_resolve_profile_alias`."
        ),
        "enable": frozenset(),
        "disable": "DEFAULT_OFF_4_0_0",
        "fail_mode_overrides": {
            "destruction_guard": "hard_deny",
            "butterfly_guard": "warn",
        },
        "fail_mode_default": "silent",
        "alias_of": "lite",
    },
    "dev": {
        "description": (
            "Solo-dev daily driver — enable productivity features "
            "(dspy_prompt_optimization, polling_watcher, "
            "pip_aftermath_hint) only. Leaves DEFAULT_OFF_4_0_0 guards "
            "off. Inherits ``lite`` fail-mode defaults."
        ),
        "enable": frozenset({
            "dspy_prompt_optimization",
            "polling_watcher",
            "pip_aftermath_hint",
        }),
        "disable": frozenset(),
        "fail_mode_overrides": {
            "destruction_guard": "hard_deny",
            "butterfly_guard": "warn",
        },
        "fail_mode_default": "silent",
    },
}


# Build-time validation: every fail_mode value across every profile
# must be a member of :data:`VALID_FAIL_MODES`. Catches typos at
# import time instead of at policy-gate dispatch (the original Plan B
# spec called for runtime validation; module-level catches the
# regression earlier and costs nothing).
def _validate_profile_fail_modes() -> None:
    """Module-import gate — raises ``ValueError`` on any bad fail_mode.

    Examined keys:
      * ``fail_mode_default`` (per-profile fallback)
      * Every value in ``fail_mode_overrides``
    """
    for name, prof in FEATURE_TOGGLE_PROFILES.items():
        default = prof.get("fail_mode_default")
        if default is not None and default not in VALID_FAIL_MODES:
            raise ValueError(
                f"Profile {name!r} has invalid fail_mode_default "
                f"{default!r}. Valid: {sorted(VALID_FAIL_MODES)}"
            )
        overrides = prof.get("fail_mode_overrides") or {}
        if not isinstance(overrides, dict):
            raise ValueError(
                f"Profile {name!r} fail_mode_overrides must be a "
                f"dict, got {type(overrides).__name__}"
            )
        for feat, mode in overrides.items():
            if mode not in VALID_FAIL_MODES:
                raise ValueError(
                    f"Profile {name!r} fail_mode_overrides[{feat!r}] "
                    f"= {mode!r} is invalid. Valid: "
                    f"{sorted(VALID_FAIL_MODES)}"
                )


_validate_profile_fail_modes()


# Profile alias map — single resolver used by both
# :func:`apply_feature_toggle_profile` (Plan B carry-over) and
# :func:`get_fail_mode` (new in 4.3.0).
_PROFILE_ALIASES: dict[str, str] = {
    name: target
    for name, prof in FEATURE_TOGGLE_PROFILES.items()
    if isinstance(target := prof.get("alias_of"), str)
}


def _resolve_profile_alias(name: str) -> str:
    """Map ``permissive`` → ``lite`` (and any future alias). Pass
    through any non-aliased name verbatim, including unknown names —
    the caller's existing "Unknown profile" error path stays
    authoritative.
    """
    return _PROFILE_ALIASES.get(name, name)


def get_fail_mode(
    feature_name: str,
    profile: str = "lite",
    *,
    cfg: Any = None,
) -> FailMode:
    """Return the effective fail-mode for ``feature_name`` under ``profile``.

    Resolution chain (later wins, mirrors :meth:`Config.feature`):

      1. Profile per-feature override (``fail_mode_overrides[feat]``)
      2. Profile catch-all (``fail_mode_default``)
      3. User override on disk (``cfg.feature(feat, "fail_mode")``)
      4. Env var ``CONCINNO_<FEATURE>_FAIL_MODE`` (handled by
         :meth:`Config.feature`'s 6-source chain — no extra work here)

    The ``cfg`` argument is optional — when ``None`` we skip user/env
    overrides and return the pure profile default. This keeps the
    function trivially callable from policy-gate hot paths that do not
    want a singleton lookup on every check.

    Raises:
        ValueError: if ``profile`` does not exist in
            :data:`FEATURE_TOGGLE_PROFILES` (after alias resolution).
            Returning a silent default would mask config bugs.
    """
    canonical = _resolve_profile_alias(profile)
    if canonical not in FEATURE_TOGGLE_PROFILES:
        raise ValueError(
            f"Unknown profile {profile!r}. Available: "
            f"{', '.join(sorted(FEATURE_TOGGLE_PROFILES))}"
        )
    prof = FEATURE_TOGGLE_PROFILES[canonical]

    # User override beats profile when cfg is supplied. Honours the
    # full 6-source chain inside Config.feature, so env vars and
    # per-project cc_config.json work for free.
    if cfg is not None:
        try:
            override = cfg.feature(feature_name, "fail_mode")
        except Exception:
            override = None
        if isinstance(override, str) and override in VALID_FAIL_MODES:
            return _coerce_fail_mode(override)

    overrides: dict[str, str] = prof.get("fail_mode_overrides") or {}
    if feature_name in overrides:
        return _coerce_fail_mode(overrides[feature_name])

    default = prof.get("fail_mode_default", "warn")
    return _coerce_fail_mode(default)


def _coerce_fail_mode(value: str) -> FailMode:
    """Narrow ``str`` → ``FailMode`` after a ``VALID_FAIL_MODES`` check.

    The runtime check has already happened at module import (or at the
    Config.feature override step), so this is purely a typing helper —
    mypy strict requires the cast to bridge ``str`` → ``Literal``.
    """
    if value not in VALID_FAIL_MODES:
        # Defence in depth — should never trigger after the import-time
        # validator, but a corrupted cc_config.json could feed a junk
        # string through Config.feature.
        raise ValueError(
            f"Invalid fail_mode {value!r}. Valid: "
            f"{sorted(VALID_FAIL_MODES)}"
        )
    # mypy needs the explicit cast — Literal narrowing from a frozenset
    # membership check is not currently inferred.
    return cast("FailMode", value)


def list_feature_toggle_profiles() -> dict[str, str]:
    """Return ``{name: description}`` for the per-feature toggle profiles."""
    return {k: v["description"] for k, v in FEATURE_TOGGLE_PROFILES.items()}


def _resolve_profile_features(
    spec: "frozenset[str] | str",
) -> frozenset[str]:
    """Expand the ``"DEFAULT_OFF_4_0_0"`` sentinel to the actual
    frozenset; pass through real frozensets verbatim."""
    if spec == "DEFAULT_OFF_4_0_0":
        return DEFAULT_OFF_4_0_0
    if isinstance(spec, frozenset):
        return spec
    return frozenset()


def apply_feature_toggle_profile(
    name: str,
    cfg: Any = None,
) -> dict[str, Any]:
    """Apply a per-feature toggle profile by name.

    Returns a dict with ``profile`` / ``enabled`` / ``disabled`` /
    ``unchanged`` / ``error`` keys so callers (CLI, GUI) can render
    structured output.

    The function is idempotent — re-running ``apply_feature_toggle_profile
    ("strict")`` after the first invocation yields ``unchanged ==``
    full set, ``enabled == disabled == []``.

    The optional ``cfg`` parameter accepts a pre-built
    :class:`concinno.core.config.Config` instance (used by tests to
    isolate writes to a tmp ``cc_config.json``). When ``None``, the
    process-wide singleton from ``get_config()`` is used.
    """
    canonical = _resolve_profile_alias(name)
    if canonical not in FEATURE_TOGGLE_PROFILES:
        return {
            "profile": name,
            "error": (
                f"Unknown profile: {name!r}. Available: "
                f"{', '.join(sorted(FEATURE_TOGGLE_PROFILES))}"
            ),
            "enabled": [],
            "disabled": [],
            "unchanged": [],
        }

    profile = FEATURE_TOGGLE_PROFILES[canonical]
    to_enable = _resolve_profile_features(profile["enable"])
    to_disable = _resolve_profile_features(profile["disable"])

    enabled: list[str] = []
    disabled: list[str] = []
    unchanged: list[str] = []
    errors: list[str] = []

    if cfg is None:
        try:
            from concinno.core.config import get_config

            cfg = get_config()
        except Exception as exc:  # pragma: no cover — bootstrap is solid
            return {
                "profile": name,
                "error": f"Failed to load config: {exc}",
                "enabled": [],
                "disabled": [],
                "unchanged": [],
            }

    for feat in sorted(to_enable):
        try:
            current = bool(cfg.feature(feat, "enabled"))
        except Exception:
            current = False
        if current:
            unchanged.append(feat)
            continue
        try:
            cfg.set_feature(feat, "enabled", True)
            enabled.append(feat)
        except Exception as exc:
            errors.append(f"{feat}: {exc}")

    for feat in sorted(to_disable):
        try:
            current = bool(cfg.feature(feat, "enabled"))
        except Exception:
            current = True
        if not current:
            unchanged.append(feat)
            continue
        try:
            cfg.set_feature(feat, "enabled", False)
            disabled.append(feat)
        except Exception as exc:
            errors.append(f"{feat}: {exc}")

    result: dict[str, Any] = {
        "profile": name,
        "description": profile["description"],
        "enabled": enabled,
        "disabled": disabled,
        "unchanged": sorted(unchanged),
    }
    if errors:
        result["errors"] = errors
    return result
