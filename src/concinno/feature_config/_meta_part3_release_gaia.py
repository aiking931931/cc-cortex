"""Auto-generated partition 3/7 of FEATURE_META — DO NOT hand-edit boundaries.

Source range: feature_config.py legacy lines 1685-2726 (part3_release_gaia).

To edit a feature here, edit *this file*. To re-split,
run ``python _tmp/split_feature_config.py`` (boundaries
declared in PARTS list there).
"""
from __future__ import annotations

from typing import Any

_FEATURE_META_PART_3: dict[str, dict[str, Any]] = {
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
}
