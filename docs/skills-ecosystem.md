<!-- markdownlint-disable MD013 MD060 -->

# Concinno Agent Skill Ecosystem

> Reference for the 19+ `concinno-skills-*` PyPI sub-packages that auto-mount
> into Concinno's `ToolRegistry` via `entry_points`.

## 3-second onboarding

```bash
# Core + most useful skills (~500MB, no GPU/vector DB bloat)
pip install concinno \
    concinno-skills-google \
    concinno-skills-chat \
    concinno-skills-office \
    concinno-skills-content \
    concinno-skills-mobile \
    concinno-skills-crm \
    concinno-skills-support \
    concinno-skills-sql \
    concinno-skills-dev \
    concinno-skills-knowledge \
    concinno-skills-commerce \
    concinno-skills-mobile-ext \
    concinno-skills-marketing

# Enable auto-mount
export CONCINNO_LOAD_PLUGINS=1

# Verify tool count
python -c "from concinno.tools.registry import get_default_registry; \
           print(len(get_default_registry().list_deferred()))"
```

For vector-DB / media-gen / cloud storage — see the "Heavy extras" section below.

## Package inventory

| # | Package | Version | Tools | Top-25 slot |
|---|---------|---------|-------|-------------|
| 1 | `concinno` (core) | 2.15.1 | 5 file-ops (Read/Write/Edit/Glob/Grep) + deferred builtins | Web / Code exec |
| 2 | `concinno-skills-google` | 0.2.0 | 6 | Gmail + Drive + Sheets + Calendar + Maps |
| 3 | `concinno-skills-chat` | 0.1.0 | 4 | Slack + Discord + Telegram + Teams |
| 4 | `concinno-skills-office` | 0.1.0 | 5 | Office basic |
| 5 | `concinno-skills-video` | 0.1.0 | 3 | YouTube |
| 6 | `concinno-skills-content` | 0.1.0 | 4 | Content writer / SEO |
| 7 | `concinno-skills-mobile` | 0.1.0 | 3 | Twilio SMS / Voice |
| 8 | `concinno-skills-crm` | 0.1.0 | 4 | HubSpot + Salesforce |
| 9 | `concinno-skills-support` | 0.1.0 | 3 | Zendesk + Intercom |
| 10 | `concinno-skills-sql` | 0.1.0 | 3 | SQL |
| 11 | `concinno-skills-dev` | 0.1.0 | 5 | GitHub + GitLab |
| 12 | `concinno-skills-knowledge` | 0.1.0 | 5 | Notion + Confluence + Obsidian |
| 13 | `concinno-skills-commerce` | 0.1.0 | 5 | Stripe + Shopify + QuickBooks |
| 14 | `concinno-skills-mobile-ext` | 0.1.0 | 3 | Teams + WhatsApp + WeCom |
| 15 | `concinno-skills-vector` | 0.2.0 | 6 | Pinecone + Chroma + Weaviate (with filter DSL) |
| 16 | `concinno-skills-media` | 0.1.0 | 4 | Image gen + Replicate |
| 17 | `concinno-skills-office-advanced` | 0.1.0 | 3 | MCP bridge + PDF advanced |
| 18 | `concinno-skills-marketing` | 0.1.0 | 3 | Mailchimp + Typeform + SendGrid |
| 19 | `concinno-skills-cloud` | 0.1.0 | 3 | AWS S3 + GCP + Azure Blob (optional extras) |
| 20 | `concinno-skills-auth` | 0.1.0 | 3 | Unified credentials + OAuth device-flow + rotation (20+ providers) |
| | **Total** | | **5 core + 81 deferred = 86** | |

## Top-25 market coverage (100%)

| # | Category | Package | Status |
|---|----------|---------|--------|
| 1 | Web / Code exec | `concinno` core (Shell + HtmlToText) | LIVE |
| 2 | Code sandbox | `concinno` core (python_exec 2.14+) | LIVE |
| 3 | Gmail | `concinno-skills-google` | LIVE |
| 4 | Slack | `concinno-skills-chat` | LIVE |
| 5 | Google Drive/Docs/Sheets | `concinno-skills-google` | LIVE |
| 6 | GitHub | `concinno-skills-dev` | LIVE |
| 7 | SQL | `concinno-skills-sql` + core `DuckDbQuery` | LIVE |
| 8 | Office | `concinno-skills-office` + `-office-advanced` | LIVE |
| 9 | CRM | `concinno-skills-crm` | LIVE |
| 10 | Calendar | `concinno-skills-google` `GoogleCalendar` | LIVE |
| 11 | Notion | `concinno-skills-knowledge` | LIVE |
| 12 | Multi-channel chat | `concinno-skills-chat` + `-mobile` + `-mobile-ext` | LIVE |
| 13 | Image gen | `concinno-skills-media` | LIVE |
| 14 | Vector / RAG | `concinno-skills-vector` + core `ZIQRetrieval` | LIVE |
| 15 | Stripe / payments | `concinno-skills-commerce` | LIVE |
| 16 | Email marketing / Forms | `concinno-skills-marketing` (Mailchimp + Typeform + SendGrid) | LIVE 2026-04-23 |
| 17 | Shopify / e-commerce | `concinno-skills-commerce` | LIVE |
| 18 | YouTube | `concinno-skills-video` | LIVE |
| 19 | Cloud / DevOps | `concinno-skills-cloud` (S3 + GCP + Azure, optional extras) | LIVE 2026-04-23 |
| 20 | Content writer / SEO | `concinno-skills-content` | LIVE |
| 21 | Webhook / HTTP | all sub-packages (shared `httpx`) | LIVE |
| 22 | Maps / geo | `concinno-skills-google` `MapsGeocode` | LIVE |
| 23 | Customer support | `concinno-skills-support` | LIVE |
| 24 | Desktop OS | core `concinno` + global `windows` Skill (`~/.claude/skills/windows/`) | LIVE |
| 25 | Mobile phone | `concinno-skills-mobile` (Twilio) + `-mobile-ext` (Teams/WhatsApp/WeCom) | LIVE |

## Heavy extras (opt-in, on-demand)

### `concinno-skills-vector` — Pinecone / Chroma / Weaviate

Pulls `pinecone>=5.0` + `chromadb>=0.5` + `weaviate-client>=4.9` — ~200MB install.
0.2.0 adds MongoDB-style dict filter translation for `WeaviateQuery`:

```python
tool.call(action="query", collection="Articles", vector=[...],
          filter={"category": "ml", "year": {"$gte": 2024}})
```

Supported operators: `$eq`, `$ne`, `$gt`, `$gte`, `$lt`, `$lte`, `$in`, `$and`, `$or`, plus implicit-AND multi-field.

### `concinno-skills-media` — Image gen + Replicate

Pulls model-hosting SDKs — optional installs.

### `concinno-skills-cloud` — AWS / GCP / Azure (optional extras)

Default install is empty (just `concinno` dep). SDKs are opt-in:

```bash
pip install 'concinno-skills-cloud[aws]'     # boto3 only
pip install 'concinno-skills-cloud[gcp]'     # google-cloud-storage only
pip install 'concinno-skills-cloud[azure]'   # azure-storage-blob only
pip install 'concinno-skills-cloud[all]'     # all three (~65MB)
```

Each tool lazy-imports its SDK on first `call()` and returns a clear `missing_driver` error if the extra is not installed.

## Credentials layering

All sub-packages share Concinno's 4-source `CredentialStore` precedence:

1. Explicit `tool.call(<name>_api_key=...)` kwarg
2. Environment variable (`MAILCHIMP_API_KEY`, `SENDGRID_API_KEY`, `AWS_ACCESS_KEY_ID`, etc.)
3. OS keyring (`keyring.get_password("concinno", key_name)`)
4. Config file (`~/.concinno/credentials.json`)

No vendor lock-in to OAuth-only flows — most sub-packages accept either API key (simpler) or OAuth token (safer for multi-tenant).

## Moat vs competitors

| Competitor pattern | Concinno response |
|---|---|
| Community-toolkit manual import (common in prior-art frameworks) | `entry_points` auto-mount once `CONCINNO_LOAD_PLUGINS=1` — 19 package, zero wire-up |
| Vendor-hosted agent SDKs (cloud lock-in) | On-premise Python, works with any LLM |
| MCP (Anthropic protocol) over-the-wire overhead | In-process Python preferred; MCP used only as fallback bridge (`AnthropicOfficeMcpBridge`) |
| Trace-after-event observability | `CBUA guard PreToolUse` hard deny; `BoundaryGuard` source-level enforcement |
| Per-integration OAuth re-implementation | Shared `CredentialStore` across all 19 sub-packages |
| Legacy `pinecone-client` SDK in the wild | `concinno-skills-vector` uses `pinecone>=5.0` (new SDK) |
| GPL-3.0 contagion risk (`python-telegram-bot`, `zenpy`) | `concinno-skills-chat` uses `aiogram` (MIT); `concinno-skills-support` uses httpx REST — no GPL deps |
| Copyleft OSL-3.0 (`python-quickbooks`) | `concinno-skills-commerce` uses `intuit-oauth` + httpx REST |

## Boundary — Concinno vs Sancio

Routing rule: **"Can CC do it today?"**

- **Yes** → lives in Concinno (core or `concinno-skills-*` sub-package). VS Code CC user `pip install` and it works in the agent loop.
- **No** (CC L1-L8 platform limit locked) → lives in Sancio runtime.

Sancio's narrow scope: `PostToolUse` hard deny (CC L6 can only warn), cross-session `state_store`, `subagent_fork` for real-time supervision (CC L1 loses control after spawn). Sancio **consumes** `concinno-skills-*`; it never re-implements integration surface.

## Future additions (wave-5+)

- Cloud DevOps deepening: EC2 / Cloud Run / Azure Functions orchestration
- LLM provider wrappers beyond Anthropic (OpenAI Chat / Gemini / Mistral as thin adapters)
- Robotics / IoT (Home Assistant already in `concinno-skills-mobile-ext`; room for MQTT / Zigbee)

## References

- Concinno core on PyPI: <https://pypi.org/project/concinno/> (2.15.1 LIVE, 2026-04-22)
- Source repo: currently private during beta; see `project.urls` in each sub-package's `pyproject.toml` for the authoritative URL once public.
- Concinno/Sancio boundary: `projects/concinno/CLAUDE.md` §"Boundary — Concinno vs Sancio"
- Pod merge 2.16.0 checklist: `projects/concinno/docs/pod-merge-2.16.0.md`
- Release coordination: `projects/concinno/RELEASE_COORDINATION.md`
