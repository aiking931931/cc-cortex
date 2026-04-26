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
  ``typescript``, ``whitepaper_guard``, ``language_enforce``,
  ``deny_marker``, ``token_display``, ``handoff_format``,
  ``pipeline_mode``, ``handoff_required_guard``, ``identity_guard``,
  ``butterfly_guard``, ``code_guard``, ``boundary_guard``,
  ``agent_cap``, ``cognitive_anchor``, ``design_theory``,
  ``token_gate``, ``structural_guard``, ``ui_verify``,
  ``publish_scan``, ``proposal_guard``, ``sentinel_gate``,
  ``consecutive_fail_gate``, ``hijack_gate`` — every one has either
  a ``BaseGuard`` subclass picked up by the pipeline dispatch or a
  direct ``cfg.feature()`` call at its hook entry point. Use
  ``concinno config set <name> enabled false`` and the guard stops
  running at runtime without a code change or session restart.
"""

from __future__ import annotations

from typing import Any, Optional

# ── Risk Metadata ─────────────────────────────────────────

FEATURE_META: dict[str, dict] = {
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
    "whitepaper_guard": {
        "category": "hard_gate",
        "severity_if_off": "major",
        "consequences_if_off": "白皮書 IP 關鍵字外洩到外部路徑風險",
        "description": "Block whitepaper IP keywords from leaking to external paths",
        "description_zh": "阻擋白皮書核心 IP 關鍵字外流到外部路徑",
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {
            "mode": {
                "type": "str",
                "default": "hard_deny",
                "options": ["step_back_first", "hard_deny", "off"],
                "recommended": "hard_deny",
                "risk_off": "Gate disabled — whitepaper content may leak",
                "risk_off_zh": "Gate 關閉 — 白皮書內容可能外流",
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
    "proposal_guard": {
        "category": "hard_gate",
        "severity_if_off": "major",
        "consequences_if_off": "Planning 新 proposal 無副作用分析警告失效",
        "description": "Block new proposals in planning files without side-effect analysis",
        "description_zh": "規劃檔新提案缺少副作用分析時阻擋（動序 Poka-Yoke）",
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {
            "mode": {
                "type": "str",
                "default": "step_back_first",
                "options": ["step_back_first", "hard_deny", "off"],
                "recommended": "step_back_first",
                "risk_off": "Gate disabled — proposals without side-effect analysis pass through",
                "risk_off_zh": "Gate 關閉 — 無副作用分析的提案不會被攔截",
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
    # ── Cognitive Anchor (red-team injection) ──
    "cognitive_anchor": {
        "category": "context",
        "description": (
            "Inject solid-state language red-team prompts before high-risk"
            " operations (architecture edits, large deletions, new modules, deploys)"
        ),
        "description_zh": (
            "高風險操作前注入固態語言紅隊提示"
            "（架構修改、大量刪除、新模組、部署）"
        ),
        "ziq_autotunable": True,
        "cosmetic": False,
        "params": {
            "deletion_threshold": {
                "type": "int",
                "default": 50,
                "min": 20,
                "max": 200,
                "recommended": 50,
                "risk_low": "Below 20 triggers on minor edits — too noisy",
                "risk_high": "Above 200 misses significant deletions",
                "risk_low_zh": "低於 20 連小改動都觸發，太吵",
                "risk_high_zh": "高於 200 會漏掉重大刪除",
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


def _audit_log_path():
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
        pkg = sources["_plugin_pkg"]  # type: ignore[assignment]
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


def list_features(lang: str = "en") -> list[dict]:
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
            "enabled": current.get("enabled", meta.get("enabled", True)),
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


def get_feature(name: str, lang: str = "en") -> Optional[dict]:
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
        "enabled": current.get("enabled", True),
        "params": params,
    }


def _validate_numeric(
    name: str, key: str, value: Any, param: dict, ptype: str,
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
    name: str, key: str, value: Any, param: dict, ptype: str,
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


def list_with_routing(lang: str = "en") -> list[dict]:
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
