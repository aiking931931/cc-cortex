"""concinno.feature_config — Feature risk metadata, validation, and safe get/set.

@module feature_config
@responsibility Define risk metadata for all configurable features, validate parameter
    changes with min/max/recommended bounds, and provide safe get/set API
    with risk warnings.
@dependencies (none — self-contained metadata)
@exports list_features, get_feature, set_feature, validate_value, FEATURE_META

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
        "description": "Hook/library boundary violation detection (PreToolUse DENY)",
        "description_zh": "偵測 Hook/庫邊界違規（PreToolUse 硬擋）",
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {},
    },
    "publish_scan": {
        "category": "hard_gate",
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
}


# ── Public API ────────────────────────────────────────────


def list_features(lang: str = "en") -> list[dict]:
    """List all features with current config values."""
    try:
        from concinno.core.config import get_config

        cfg = get_config()
    except Exception:
        cfg = None

    result = []
    for name, meta in FEATURE_META.items():
        desc = meta.get(f"description_{lang}", meta["description"])
        current = cfg.feature_all(name) if cfg else {}
        result.append({
            "name": name,
            "category": meta["category"],
            "description": desc,
            "enabled": current.get("enabled", True),
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
