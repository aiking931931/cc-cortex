"""Auto-generated partition 7/7 of FEATURE_META — DO NOT hand-edit boundaries.

Source range: feature_config.py legacy lines 4386-5067 (part7_universal).

To edit a feature here, edit *this file*. To re-split,
run ``python _tmp/split_feature_config.py`` (boundaries
declared in PARTS list there).
"""
from __future__ import annotations

from typing import Any

_FEATURE_META_PART_7: dict[str, dict[str, Any]] = {
    # ── ai-king 6+ Wave 2 UNI-A — universal_skill_schema (v0.1)
    #
    # Wave 2 UNI-A schema-lock per verdict 2026-05-08 §3 row 2.
    # Ships ``concinno.skills.universal.{schema, validator, registry,
    # dedup}`` plus the ``aiking skill {validate,register,list,dedup}``
    # CLI. v0.1 EXPERIMENTAL — adopters MAY use the schema for private
    # dogfood but MUST NOT publish ``.skill.yaml`` outputs to a public
    # marketplace until v1.0 freeze (4-6 weeks of telemetry inform the
    # field-set decision per 紅 F-1 acceptance).
    #
    # Default ON: validator/registry are inert until invoked, so cost
    # of leaving on is zero; turning off blocks the CLI subcommand and
    # any future converters/marketplace flows that depend on the
    # universal schema.
    "universal_skill_schema": {
        "category": "behavioral",
        "enabled": True,
        "ziq_autotunable": False,
        "cosmetic": False,
        "severity_if_off": "minor",
        "consequences_if_off": (
            "universal schema validator / registry / dedup detector 不啟用，"
            "skill 仍走 substrate-native frontmatter，跨 marketplace 安裝失能 — "
            "Wave 3 UNI-B converter / Wave 8 HUB-CLI install/publish flow 依賴"
            "本 feature，關閉等同凍結整條 universal-schema 軌。"
        ),
        "consequences_if_off_en": (
            "universal schema validator / registry / dedup detector "
            "stay dormant; skills keep running on substrate-native "
            "frontmatter only and cross-marketplace install fails. "
            "Wave 3 UNI-B converters and Wave 8 HUB-CLI install/publish "
            "depend on this feature; disabling freezes the entire "
            "universal-schema track."
        ),
        "description": (
            "Wave 2 UNI-A — universal .skill.yaml v0.1 EXPERIMENTAL "
            "dogfood-only schema + validator + cross-marketplace TOML "
            "registry + typosquat-aware dedup detector. Lets one "
            "skill description run on multiple substrates "
            "(concinno-king / hermes-agent / openclaw / cc-cli / cline / "
            "cursor / codex) via per-substrate converters that land in "
            "Wave 3 UNI-B. Bidirectional default = cc-cli + concinno-king "
            "only; the other 5 substrates ship unidirectional converters "
            "per verdict 2026-05-08 §2 紅 F-4 acceptance."
        ),
        "description_zh": (
            "Wave 2 UNI-A — 通用 .skill.yaml v0.1 實驗版 schema + "
            "validator + 跨市集 TOML registry + typosquat-aware dedup "
            "detector。一份 skill 描述在多個 substrate 跑 "
            "(concinno-king / hermes-agent / openclaw / cc-cli / cline / "
            "cursor / codex)，靠 per-substrate converter（Wave 3 UNI-B "
            "落地）。雙向預設只開 cc-cli + concinno-king，其餘 5 個"
            "substrate 出單向 converter（per verdict 紅 F-4 接受但降級）。"
        ),
        "params": {
            "registry_path": {
                "type": "str",
                "default": "~/.aiking/registry.toml",
                "recommended": "~/.aiking/registry.toml",
            },
            "schema_version": {
                "type": "int",
                "default": 1,
                "min": 1,
                "max": 1,
                "recommended": 1,
                "risk_low": "schema_version=0 is invalid (no v0)",
                "risk_high": (
                    "schema_version>1 is unreleased; outputs cannot be "
                    "loaded by current concinno builds"
                ),
                "risk_low_zh": "schema_version=0 無效（沒有 v0）",
                "risk_high_zh": (
                    "schema_version>1 尚未發布，當前 concinno 無法讀取"
                ),
            },
            "experimental": {
                "type": "bool",
                "default": True,
                "recommended": True,
                "risk_low": (
                    "experimental=False signals callers to depend on the "
                    "v0.1 schema as if it were stable — premature lock-in"
                ),
                "risk_high": (
                    "experimental=True is correct for v0.1; callers MUST "
                    "not publish outputs to a public marketplace yet"
                ),
                "risk_low_zh": (
                    "experimental=False 會讓呼叫端把 v0.1 schema 當"
                    "stable 用，提前 lock-in 風險高"
                ),
                "risk_high_zh": (
                    "experimental=True 為 v0.1 正確設定；呼叫端"
                    "尚不可發布到公開市集"
                ),
            },
        },
        "recommended": True,
    },
    # ── ai-king 6+ Wave 3 UNI-B — 6 substrate converter rows
    #
    # Wave 3 UNI-B per verdict 2026-05-08 §3 row 3. Each converter ships
    # in ``concinno.skills.universal.converters.<substrate>`` and
    # implements ``SubstrateConverterProtocol``. Bidirectional substrates
    # (cc-cli, concinno-king) MUST round-trip byte-identical;
    # unidirectional substrates (hermes-agent, openclaw, cline, codex)
    # raise ``NotImplementedError`` from ``from_native`` per 紅 F-4.
    # Default ON: a converter does no work until invoked, so leaving on
    # has zero cost; turning off prevents that substrate from
    # participating in cross-marketplace install / publish flows.
    "shell.universal_converter.cc_cli": {
        "category": "behavioral",
        "enabled": True,
        "ziq_autotunable": False,
        "cosmetic": False,
        "severity_if_off": "minor",
        "consequences_if_off": (
            "cc-cli 不再參與跨 substrate 安裝與發布；既有 "
            "~/.claude/skills/<name>/SKILL.md 仍可由 cc-cli 直讀。"
        ),
        "consequences_if_off_en": (
            "cc-cli stops participating in cross-substrate install / "
            "publish flows; existing ~/.claude/skills/<name>/SKILL.md "
            "still loads in cc-cli natively."
        ),
        "description": (
            "Wave 3 UNI-B — cc-cli adapter (bidirectional ★). "
            "Round-trip byte-identical between universal .skill.yaml "
            "and ~/.claude/skills/<name>/SKILL.md."
        ),
        "description_zh": (
            "Wave 3 UNI-B — cc-cli 轉接器（雙向 ★）。"
            "通用 .skill.yaml 與 ~/.claude/skills/<name>/SKILL.md "
            "雙向位元相等往返。"
        ),
        "params": {
            "is_bidirectional": {
                "type": "bool",
                "default": True,
                "recommended": True,
            },
        },
        "recommended": True,
    },
    "shell.universal_converter.concinno_king": {
        "category": "behavioral",
        "enabled": True,
        "ziq_autotunable": False,
        "cosmetic": False,
        "severity_if_off": "minor",
        "consequences_if_off": (
            "concinno-king 自家 substrate 失去跨市集參與；本機 "
            "concinno-king skill 仍照常運作。"
        ),
        "consequences_if_off_en": (
            "concinno-king (self-fork substrate) loses cross-marketplace "
            "participation; local concinno-king skills keep working."
        ),
        "description": (
            "Wave 3 UNI-B — concinno-king adapter (bidirectional ★). "
            "Self-fork substrate, Hermes-style SKILL.md frontmatter "
            "with byte-identical round-trip."
        ),
        "description_zh": (
            "Wave 3 UNI-B — concinno-king 轉接器（雙向 ★）。"
            "自家 fork substrate，Hermes-style SKILL.md 開頭設定區，"
            "位元相等往返。"
        ),
        "params": {
            "is_bidirectional": {
                "type": "bool",
                "default": True,
                "recommended": True,
            },
        },
        "recommended": True,
    },
    "shell.universal_converter.hermes_agent": {
        "category": "behavioral",
        "enabled": True,
        "ziq_autotunable": False,
        "cosmetic": False,
        "severity_if_off": "minor",
        "consequences_if_off": (
            "hermes-agent 不再接受 ai-king skill；對方 PyPI plugin 仍照常。"
        ),
        "consequences_if_off_en": (
            "hermes-agent stops accepting ai-king skills; their own "
            "PyPI plugin pipeline is unaffected."
        ),
        "description": (
            "Wave 3 UNI-B — hermes-agent adapter (unidirectional). "
            "Emits plugin.json + skills/<slug>/SKILL.md per Hermes spec."
        ),
        "description_zh": (
            "Wave 3 UNI-B — hermes-agent 轉接器（單向）。"
            "輸出 plugin.json + skills/<slug>/SKILL.md（Hermes 規格）。"
        ),
        "params": {
            "is_bidirectional": {
                "type": "bool",
                "default": False,
                "recommended": False,
            },
        },
        "recommended": True,
    },
    "shell.universal_converter.openclaw": {
        "category": "behavioral",
        "enabled": True,
        "ziq_autotunable": False,
        "cosmetic": False,
        "severity_if_off": "minor",
        "consequences_if_off": (
            "openclaw 不再接受 ai-king skill；對方 fork 內運作不受影響。"
        ),
        "consequences_if_off_en": (
            "openclaw stops accepting ai-king skills; their fork "
            "internal operation is unaffected."
        ),
        "description": (
            "Wave 3 UNI-B — openclaw adapter (unidirectional, "
            "Hermes-style fallback per MEDIUM-confidence schema)."
        ),
        "description_zh": (
            "Wave 3 UNI-B — openclaw 轉接器（單向，"
            "因無正規格式規格採 Hermes-style 回退設計）。"
        ),
        "params": {
            "is_bidirectional": {
                "type": "bool",
                "default": False,
                "recommended": False,
            },
        },
        "recommended": True,
    },
    "shell.universal_converter.cline": {
        "category": "behavioral",
        "enabled": True,
        "ziq_autotunable": False,
        "cosmetic": False,
        "severity_if_off": "minor",
        "consequences_if_off": (
            "cline 不再接受 ai-king skill；shell-pipeline 類 skill "
            "本來就標 incompatible。"
        ),
        "consequences_if_off_en": (
            "cline stops accepting ai-king skills; shell-pipeline "
            "skills are already marked incompatible."
        ),
        "description": (
            "Wave 3 UNI-B — cline adapter (unidirectional, MCP "
            "wire-protocol). Compatible: python_module / mcp_server / "
            "js_module entry; incompatible: shell_script / "
            "claude_skill_md."
        ),
        "description_zh": (
            "Wave 3 UNI-B — cline 轉接器（單向，MCP wire-protocol）。"
            "相容：python_module / mcp_server / js_module 進入點；"
            "不相容：shell_script / claude_skill_md。"
        ),
        "params": {
            "is_bidirectional": {
                "type": "bool",
                "default": False,
                "recommended": False,
            },
        },
        "recommended": True,
    },
    "shell.universal_converter.codex": {
        "category": "behavioral",
        "enabled": True,
        "ziq_autotunable": False,
        "cosmetic": False,
        "severity_if_off": "minor",
        "consequences_if_off": (
            "OpenAI Codex 不再接受 ai-king skill；對方 plugin 流程不受影響。"
        ),
        "consequences_if_off_en": (
            "OpenAI Codex stops accepting ai-king skills; their plugin "
            "pipeline is unaffected."
        ),
        "description": (
            "Wave 3 UNI-B — codex adapter (unidirectional). Emits "
            ".codex-plugin/plugin.json (JSON-only, deterministic via "
            "json.dumps(sort_keys=True))."
        ),
        "description_zh": (
            "Wave 3 UNI-B — codex 轉接器（單向）。"
            "輸出 .codex-plugin/plugin.json（純 JSON，靠 "
            "json.dumps(sort_keys=True) 確定性序列化）。"
        ),
        "params": {
            "is_bidirectional": {
                "type": "bool",
                "default": False,
                "recommended": False,
            },
        },
        "recommended": True,
    },
    # ── ai-king 6+ Wave 4 UNI-C — cc-cli to ai-king skill batch migration
    #
    # Wave 4 UNI-C per verdict 2026-05-08 §3 row 4. Ships
    # ``concinno.skills.universal.cli_migrate`` plus the ``aiking skill
    # migrate-cc-cli`` subcommand. Walks ``~/.claude/skills/<name>/``
    # subdirs containing ``SKILL.md``, runs each through
    # ``CcCliConverter.from_native`` to a ``SkillYaml``, then writes
    # ``~/.aiking/skills/private/<name>/.skill.yaml`` with visibility=private
    # default. Round-trip byte-identical verified per skill before write.
    # Failures collected to ``~/.aiking/skills/_unmigrated.json`` with reason.
    #
    # Default ON: migration is invoked manually via CLI; nothing runs in
    # the background. Disabling hides the subcommand only — adopters can
    # still hand-author ``.skill.yaml`` files in the target directory.
    "shell.universal_migration.cc_cli_to_aiking": {
        "category": "behavioral",
        "enabled": True,
        "ziq_autotunable": False,
        "cosmetic": False,
        "severity_if_off": "minor",
        "consequences_if_off": (
            "`aiking skill migrate-cc-cli` 隱藏；既有 cc-cli skill 仍照常運作，"
            "但無法批量轉成 universal `.skill.yaml` 進跨市集流程。"
        ),
        "consequences_if_off_en": (
            "`aiking skill migrate-cc-cli` becomes hidden; existing "
            "cc-cli skills keep working, but batch-conversion to "
            "universal `.skill.yaml` for cross-marketplace flows is "
            "unavailable."
        ),
        "description": (
            "Wave 4 UNI-C — batch migration of 65 existing cc-cli "
            "(`~/.claude/skills/<name>/SKILL.md`) skills to universal "
            "`.skill.yaml` (`~/.aiking/skills/private/<name>/.skill.yaml`) "
            "via CcCliConverter round-trip with byte-identical verification "
            "per skill + failure collection to `_unmigrated.json`."
        ),
        "description_zh": (
            "Wave 4 UNI-C — 65 個既有 cc-cli "
            "(`~/.claude/skills/<name>/SKILL.md`) skill 批量遷移到通用 "
            "`.skill.yaml` (`~/.aiking/skills/private/<name>/.skill.yaml`)，"
            "靠 CcCliConverter 雙向往返每個 skill 位元相等驗證，失敗"
            "蒐集到 `_unmigrated.json` 含 reason。"
        ),
        "params": {
            "default_owner": {
                "type": "str",
                "default": "ai-king",
                "recommended": "ai-king",
            },
            "default_version": {
                "type": "str",
                "default": "0.1.0",
                "recommended": "0.1.0",
            },
            "skip_if_dst_exists": {
                "type": "bool",
                "default": True,
                "recommended": True,
            },
        },
        "recommended": True,
    },
    # ── ai-king 6+ Wave 9 NET-α — intra-LAN A2A network transport
    #
    # Wave 9 NET-α per verdict 2026-05-08 §3 row 9. Ships
    # ``persona.a2a_network.{transport, discovery, agent_registry}``
    # cross-machine extension of Sancio's A2A. HTTP/2 over LAN +
    # UDP-broadcast peer discovery + TOML-persisted remote agent
    # registry + 6-axis security wired (mTLS / JWT / capability /
    # signature+replay / rate+quota / audit-log) — all six axes invoked
    # on every send and verified by ≥1 production caller test per axis.
    # NET-α scope = intra-LAN; cross-internet (NET-β) deferred to Wave 12.
    #
    # Default OFF: cross-machine transport is opt-in per CLAUDE.md L0
    # A2A-deny-by-default + privacy (advertise_self requires explicit
    # opt-in). Turning on enables peer discovery + remote skill exec.
    "shell.a2a_network_intra_lan": {
        "category": "behavioral",
        "enabled": False,
        "ziq_autotunable": False,
        "cosmetic": False,
        "severity_if_off": "minor",
        "consequences_if_off": (
            "Sancio a2a_network 不啟動；單機運作不受影響。跨機器 skill "
            "執行 + cross-machine result aggregation 失能。"
        ),
        "consequences_if_off_en": (
            "Sancio a2a_network stays dormant; single-machine operation "
            "unaffected. Cross-machine skill execution and result "
            "aggregation are unavailable."
        ),
        "description": (
            "Wave 9 NET-α — intra-LAN HTTP/2 transport + UDP-broadcast "
            "peer discovery + TOML-persisted remote agent registry. "
            "6-axis A2A security (mTLS/JWT/cap/sig+replay/rate/audit) "
            "wired and asserted per send."
        ),
        "description_zh": (
            "Wave 9 NET-α — 同網段 HTTP/2 傳輸層 + UDP 廣播對等"
            "發現 + TOML 持久化遠端代理註冊。6 軸 A2A 安全 "
            "(mTLS/JWT/能力/簽章+重放/速率/稽核) 每次 send 都"
            "接通並斷言。"
        ),
        "params": {
            "advertise_self_optin": {
                "type": "bool",
                "default": False,
                "recommended": False,
            },
            "discovery_timeout_sec": {
                "type": "float",
                "default": 3.0,
                "recommended": 3.0,
            },
            "transport_port": {
                "type": "int",
                "default": 47821,
                "recommended": 47821,
            },
        },
        "recommended": False,
    },
    # ── ai-king 6+ Wave 6 OBS — observability dashboard
    #
    # Wave 6 OBS per verdict 2026-05-08 §3 row 6. Ships
    # ``concinno.observability.dashboard.{jsonl_reader, rollup,
    # html_report}`` plus the ``aiking observability dashboard``
    # subcommand. Reads 5-dim WIRE hook jsonl + skill_usage_counter +
    # wiredo_verify_outcomes + emergence_log → simple HTML report
    # (stdlib only, no Jinja2 / matplotlib hard dep) with ASCII bar
    # charts + XSS-safe interpolation. Graceful all-missing handling.
    #
    # Default ON: dashboard is invoked manually via CLI; nothing runs
    # background. Disabling hides the subcommand only.
    "shell.observability_dashboard": {
        "category": "behavioral",
        "enabled": True,
        "ziq_autotunable": False,
        "cosmetic": False,
        "severity_if_off": "minor",
        "consequences_if_off": (
            "`aiking observability dashboard` 隱藏；既有 token_audit / "
            "skill_usage_counter / wiredo_verify_outcomes 等 jsonl "
            "telemetry 仍照常累積，只是無 dashboard 渲染。"
        ),
        "consequences_if_off_en": (
            "`aiking observability dashboard` becomes hidden; existing "
            "token_audit / skill_usage_counter / wiredo_verify_outcomes "
            "jsonl telemetry keeps accumulating, just no dashboard render."
        ),
        "description": (
            "Wave 6 OBS — observability dashboard reading "
            "`~/.concinno/state/*.jsonl` + `~/.concinno/audit/*.jsonl` + "
            "skill_usage_counter telemetry → HTML report with ASCII bar "
            "charts + XSS-safe interpolation. Stdlib only (matplotlib "
            "graceful skip if not installed)."
        ),
        "description_zh": (
            "Wave 6 OBS — 可觀測性儀表板讀 `~/.concinno/state/*.jsonl` "
            "+ `~/.concinno/audit/*.jsonl` + skill_usage_counter 遙測 "
            "→ HTML 報告含 ASCII 條形圖 + XSS 安全跳脫。純 stdlib"
            "（matplotlib 缺失優雅跳過）。"
        ),
        "params": {
            "default_state_dir": {
                "type": "str",
                "default": "~/.concinno/state/",
                "recommended": "~/.concinno/state/",
            },
            "render_matplotlib_chart": {
                "type": "bool",
                "default": False,
                "recommended": False,
            },
        },
        "recommended": True,
    },
    # ── ai-king 6+ Wave 7 SEC — marketplace security review
    #
    # Wave 7 SEC per verdict 2026-05-08 §3 row 7. Ships
    # ``concinno.skills.universal.security.{canonical_id_verify,
    # skill_signing, clamav_scan, review_queue, pipeline}``. 4-stage
    # SecurityPipeline: canonical_id ownership verify (12 reserved
    # owner blacklist + Levenshtein-1 + confusable-cluster typosquat
    # detection) → ed25519 signature verify (signs
    # `<canonical_id>:<checksum_sha256>` not raw YAML) → ClamAV scan
    # (clamdscan/clamscan with STUB-NOOP graceful fallback) → review
    # queue submit (TOML persistence + atomic-replace + 5/30d rate
    # limit per submitter pubkey).
    #
    # Default OFF: marketplace upload flow is not wired in until Wave
    # 8 HUB-CLI (`aiking publish` invokes pipeline) and Wave 10
    # HUB-WEB (upload form invokes pipeline with strict mode).
    # Library code ships now to unblock Wave 8/10 dispatch.
    "shell.marketplace_security_review": {
        "category": "behavioral",
        "enabled": False,
        "ziq_autotunable": False,
        "cosmetic": False,
        "severity_if_off": "minor",
        "consequences_if_off": (
            "Marketplace upload security pipeline 不啟動；本地 skill "
            "registry 仍照常運作。Wave 8 HUB-CLI / Wave 10 HUB-WEB "
            "公開上架流程沒接通安全審核 = 不應上線生產。"
        ),
        "consequences_if_off_en": (
            "Marketplace upload security pipeline stays dormant; local "
            "skill registry keeps working. Wave 8 HUB-CLI / Wave 10 "
            "HUB-WEB public publish flow has no security gate = MUST "
            "NOT go to production."
        ),
        "description": (
            "Wave 7 SEC — 4-stage SecurityPipeline (canonical_id "
            "ownership verify with reserved-owner blacklist + "
            "Levenshtein typosquat / ed25519 signature verify / "
            "ClamAV stub graceful fallback / review queue with TOML "
            "persistence + 5/30d rate limit per submitter pubkey). "
            "Library code ships, production wire-in lands at Wave 8 + 10."
        ),
        "description_zh": (
            "Wave 7 SEC — 4-stage 安全一條龍流程（canonical_id 擁有"
            "驗證含保留 owner 黑名單 + Levenshtein typosquat 偵測 / "
            "ed25519 簽章驗證 / ClamAV stub 優雅回退 / 審核佇列 TOML "
            "持久化 + 每 submitter pubkey 30 天 5 次速率限制）。"
            "庫端先 ship，production 接線等 Wave 8 + 10。"
        ),
        "params": {
            "clamav_stub_when_missing": {
                "type": "bool",
                "default": True,
                "recommended": True,
            },
            "rate_limit_count_per_window": {
                "type": "int",
                "default": 5,
                "recommended": 5,
            },
            "rate_limit_window_days": {
                "type": "int",
                "default": 30,
                "recommended": 30,
            },
        },
        "recommended": False,
    },
    # ── ai-king 6+ Wave 8 HUB-CLI — local registry skill hub
    #
    # Wave 8 per verdict 2026-05-08 §3 row 8. Three CLI verbs
    # (``aiking hub {search,install,publish}``) over the local
    # ``~/.aiking/registry.toml`` cross-marketplace index. Spec contract
    # at ``_AI_BRAIN/05_Planning/wave_8_hub_api_spec_v0_1.md`` (OpenAPI
    # 3.1 surface). Future Wave 10 HUB-WEB swaps the file backend for
    # an HTTP backend without changing this CLI surface.
    #
    # Default ON for all 3: each verb is invoked manually; turning off
    # only hides the subcommand. Library code (``concinno.skills.
    # universal``) keeps working regardless.
    "shell.hub_cli.search": {
        "category": "behavioral",
        "enabled": True,
        "ziq_autotunable": False,
        "cosmetic": False,
        "severity_if_off": "minor",
        "consequences_if_off": (
            "`aiking hub search` 隱藏；既有 `aiking skill list` 仍可用。"
        ),
        "consequences_if_off_en": (
            "`aiking hub search` becomes hidden; the equivalent "
            "``aiking skill list`` legacy command keeps working."
        ),
        "description": (
            "Wave 8 HUB-S3 — read-only registry search by canonical_id "
            "substring + optional substrate / visibility filter. Outputs "
            "aligned text table or SearchResponse JSON per HUB-API §2."
        ),
        "description_zh": (
            "Wave 8 HUB-S3 — registry 唯讀搜尋，依 canonical_id 子字串"
            "及 substrate/visibility 過濾，輸出 text 表或 SearchResponse "
            "JSON（spec §2）。"
        ),
        "params": {},
        "recommended": True,
    },
    "shell.hub_cli.install": {
        "category": "behavioral",
        "enabled": True,
        "ziq_autotunable": False,
        "cosmetic": False,
        "severity_if_off": "minor",
        "consequences_if_off": (
            "`aiking hub install` 隱藏；典型用戶需自行 cp + 編輯 "
            "registry.toml 完成相同操作。"
        ),
        "consequences_if_off_en": (
            "`aiking hub install` becomes hidden; users have to manually "
            "cp + edit registry.toml to achieve the same result."
        ),
        "description": (
            "Wave 8 HUB-S1 — materialise a Registry entry with set-union "
            "merge of sources, last_synced bump, and typosquat-guard "
            "checksum compare. Exit 9 on checksum mismatch unless --force."
        ),
        "description_zh": (
            "Wave 8 HUB-S1 — 落實 Registry 條目（sources 集合聯集 + "
            "last_synced 更新 + typosquat 守門 checksum 比對），mismatch "
            "且未 --force 時 exit 9。"
        ),
        "params": {},
        "recommended": True,
    },
    "shell.hub_cli.publish": {
        "category": "behavioral",
        "enabled": True,
        "ziq_autotunable": False,
        "cosmetic": False,
        "severity_if_off": "minor",
        "consequences_if_off": (
            "`aiking hub publish` 隱藏；無法走 validate→sign→register "
            "標準流程，只能手動 register（缺 ed25519 簽名）。"
        ),
        "consequences_if_off_en": (
            "`aiking hub publish` becomes hidden; users lose the "
            "validate→sign→register pipeline and have to manually "
            "register entries (without ed25519 signature)."
        ),
        "description": (
            "Wave 8 HUB-S2 — validate .skill.yaml + ed25519 sign + "
            "(public-only) SecurityPipeline gate + Registry insert. "
            "Exit 22 on validation/pipeline fail; 9 on checksum conflict."
        ),
        "description_zh": (
            "Wave 8 HUB-S2 — 驗證 .skill.yaml + ed25519 簽名 + "
            "（public 才走）SecurityPipeline 把關 + Registry 插入。"
            "驗證/pipeline 失敗 exit 22；checksum 衝突 exit 9。"
        ),
        "params": {},
        "recommended": True,
    },
    # ── ai-king 6+ Wave 11 LOAD-Phase2 — substrate compatibility audit
    #
    # Wave 11 per verdict 2026-05-08 §3 row 11. Read-only audit verb
    # (`aiking substrate audit`) over the local registry. Counts per-
    # substrate compatibility cells (native / via_converter / incompatible
    # / unknown) across the 7 supported substrates and surfaces actionable
    # migrate hints for incompatible rows. Premise resolved: substrate
    # switching itself already LIVE via ``aiking config set
    # substrate.source`` + 6-source chain in ``loader._resolved_pref``.
    #
    # Default ON cosmetic — read-only, no mutation, invoked manually.
    "shell.substrate_migrate": {
        "category": "behavioral",
        "enabled": True,
        "ziq_autotunable": False,
        "cosmetic": False,
        "severity_if_off": "minor",
        "consequences_if_off": (
            "`aiking substrate audit` 隱藏；用戶切 substrate 前無法快速"
            "檢視既有 skill 跨 substrate 的 compatibility 健康度。"
        ),
        "consequences_if_off_en": (
            "`aiking substrate audit` becomes hidden; users lose the "
            "quick way to see registry skill compatibility across all "
            "supported substrates before switching."
        ),
        "description": (
            "Wave 11 LOAD-Phase2 — read-only registry-vs-substrate "
            "compatibility audit. Reports per-substrate counts + lists "
            "incompatible / unknown skills with migrate hints."
        ),
        "description_zh": (
            "Wave 11 LOAD-Phase2 — 唯讀 registry 跨 substrate "
            "compatibility 審計。輸出每 substrate 的計數 + 列出 "
            "incompatible / unknown skill 及 migrate 提示。"
        ),
        "params": {},
        "recommended": True,
    },
}
