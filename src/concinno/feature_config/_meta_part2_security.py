"""Auto-generated partition 2/7 of FEATURE_META — DO NOT hand-edit boundaries.

Source range: feature_config.py legacy lines 1077-1684 (part2_security).

To edit a feature here, edit *this file*. To re-split,
run ``python _tmp/split_feature_config.py`` (boundaries
declared in PARTS list there).
"""
from __future__ import annotations

from typing import Any

_FEATURE_META_PART_2: dict[str, dict[str, Any]] = {
    # ── Routebackend Prefix Pairing (Wave D Step C) ──
    "routebackend_prefix_pairing": {
        "category": "soft_gate",
        "severity_if_off": "minor",
        "consequences_if_off": (
            "Model alias swaps in psyche-engine cognition modules will "
            "no longer be checked against the isSancioRouted allowlist. "
            "A backend swap without a matching startsWith() prefix will "
            "silently fall back to the default Anthropic upstream."
        ),
        "description": (
            "PostToolUse warn-only guard. After Edit/Write/NotebookEdit "
            "on psyche-engine/{src,dist}/cognition/*.{ts,js}, scan for "
            "model: '<alias>' literals and confirm each non-claude/gpt "
            "alias's prefix appears in psyche-engine/src/anthropic.ts "
            "isSancioRouted startsWith() chain. Surfaces missing pairs "
            "in additionalContext so the operator wires both files "
            "before docker cp + container deploy."
        ),
        "description_zh": (
            "PostToolUse 警告守衛。Edit/Write/NotebookEdit psyche-engine "
            "cognition 模組後，掃 model: '<alias>' literal，確認每個 "
            "非 claude/gpt alias 的 prefix 都在 anthropic.ts isSancioRouted "
            "startsWith() chain 裡。漏配 pair 在 additionalContext 顯示，"
            "讓 operator deploy 前對齊兩檔，避免悄悄走 default upstream。"
        ),
        "ziq_autotunable": False,
        "cosmetic": False,
        "params": {
            "enabled": {
                "type": "bool",
                "default": True,
                "recommended": True,
                "risk_off": (
                    "Guard disabled — backend alias swaps without router "
                    "updates can ship unchecked"
                ),
                "risk_off_zh": (
                    "Guard 關閉 — backend alias 替換漏配 router 不再檢查"
                ),
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
        # 5.0.0 BREAKING — D-class promoted default-on per 8-axis audit 2026-04-29.
        # Removed from DEFAULT_OFF_4_0_0 frozenset.
        "enabled": True,
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
        # 5.0.0 BREAKING — D-class promoted default-on per 8-axis audit 2026-04-29.
        # Removed from DEFAULT_OFF_4_0_0 frozenset.
        "enabled": True,
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
        # 5.0.0 BREAKING — D-class promoted default-on per 8-axis audit 2026-04-29.
        # Removed from DEFAULT_OFF_4_0_0 frozenset.
        "enabled": True,
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
        # 5.0.0 BREAKING — D-class promoted default-on per 8-axis audit 2026-04-29.
        # Removed from DEFAULT_OFF_4_0_0 frozenset.
        "enabled": True,
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
}
