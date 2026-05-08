"""Auto-generated partition 1/7 of FEATURE_META — DO NOT hand-edit boundaries.

Source range: feature_config.py legacy lines 207-1076 (part1_gates).

To edit a feature here, edit *this file*. To re-split,
run ``python _tmp/split_feature_config.py`` (boundaries
declared in PARTS list there).
"""
from __future__ import annotations

from typing import Any

_FEATURE_META_PART_1: dict[str, dict[str, Any]] = {
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
    # ── Wave 13 NET-A2 — A2A 6-axis security flags ──
    #
    # Per MEMORY 4zn (2026-05-08 user directive — open source rule):
    # every axis is user-opt-outable, including the dangerous
    # ``sig_replay``. OFF → warn-don't-deny (axis_flags emits one
    # stderr line + skips enforcement; never raises). The user is an
    # adult and accepts the consequence stated in
    # ``consequences_if_off``. Re-enable: ``cfg.feature(<key>,
    # 'enabled', True)`` or env ``A2A_AXIS_<NAME>=1``.
    #
    # ``ziq_autotunable=False`` for all six — these are explicit
    # safety choices (user > ZIQ per L0 鐵律 #6 priority chain).
    # ``cosmetic=False`` because security state is functional, not
    # presentational.
    "net_alpha.axis.mtls": {
        "category": "hard_gate",
        "severity_if_off": "major",
        "consequences_if_off": "MITM 風險，僅 trusted-network 安全",
        "description": "A2A axis 1 — mTLS peer cert verification",
        "description_zh": "A2A 第 1 軸 — TLS 憑證驗證",
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {
            "enabled": {
                "type": "bool",
                "default": True,
                "recommended": True,
                "risk_off": "MITM risk; only safe on trusted network",
                "risk_off_zh": "MITM 風險，僅 trusted-network 安全",
            },
        },
    },
    "net_alpha.axis.jwt": {
        "category": "hard_gate",
        "severity_if_off": "major",
        "consequences_if_off": "無身分識別，請求視為匿名",
        "description": "A2A axis 2 — JWT identity claim verification",
        "description_zh": "A2A 第 2 軸 — JWT 身分宣告驗證",
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {
            "enabled": {
                "type": "bool",
                "default": True,
                "recommended": True,
                "risk_off": "No identity; calls are anonymous",
                "risk_off_zh": "無身分識別，請求視為匿名",
            },
        },
    },
    "net_alpha.axis.capability": {
        "category": "hard_gate",
        "severity_if_off": "major",
        "consequences_if_off": "任何 method 可叫，無 method-level 授權",
        "description": "A2A axis 3 — method/tool capability allow-list",
        "description_zh": "A2A 第 3 軸 — method/tool 能力允許清單",
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {
            "enabled": {
                "type": "bool",
                "default": True,
                "recommended": True,
                "risk_off": "Any method invocable; no per-method authz",
                "risk_off_zh": "任何 method 可叫，無 method-level 授權",
            },
        },
    },
    "net_alpha.axis.sig_replay": {
        "category": "hard_gate",
        "severity_if_off": "critical",
        "consequences_if_off": "訊息可被偽造+重送，僅 trusted-network/loopback/測試",
        "description": (
            "A2A axis 4 — Ed25519/HMAC signature + nonce replay defense"
        ),
        "description_zh": "A2A 第 4 軸 — Ed25519/HMAC 簽章 + nonce 重送防護",
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {
            "enabled": {
                "type": "bool",
                "default": True,
                "recommended": True,
                "risk_off": (
                    "Messages may be forged + replayed;"
                    " only safe on trusted-network/loopback/testing"
                ),
                "risk_off_zh": "訊息可被偽造+重送，僅 trusted-network/loopback/測試",
            },
        },
    },
    "net_alpha.axis.rate_limit": {
        "category": "hard_gate",
        "severity_if_off": "major",
        "consequences_if_off": "DOS 風險，無速率/配額保護",
        "description": "A2A axis 5 — token bucket + daily quota",
        "description_zh": "A2A 第 5 軸 — token bucket 速率限制 + 每日配額",
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {
            "enabled": {
                "type": "bool",
                "default": True,
                "recommended": True,
                "risk_off": "DOS risk; no rate/quota guard",
                "risk_off_zh": "DOS 風險，無速率/配額保護",
            },
        },
    },
    "net_alpha.axis.audit": {
        "category": "hard_gate",
        "severity_if_off": "major",
        "consequences_if_off": "事後不可追溯，無稽核 jsonl trail",
        "description": "A2A axis 6 — sha256-chained append-only audit jsonl",
        "description_zh": "A2A 第 6 軸 — sha256-chained 唯增稽核 jsonl",
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {
            "enabled": {
                "type": "bool",
                "default": True,
                "recommended": True,
                "risk_off": "No forensics; audit trail not written",
                "risk_off_zh": "事後不可追溯，無稽核 jsonl trail",
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
}
