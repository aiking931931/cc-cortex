"""concinno.insight_engine — Proactive Insight Engine.

@module insight_engine
@responsibility Detect user blind spots and inject actionable knowledge assertions
    into prompts. NOT a soft warning (negative ROI) — assertive knowledge injection.
@dependencies concinno.i18n, concinno.core.config, concinno.core.state_store
@exports check_insight, InsightRule, load_insight_rules

Analyzes user prompts against a configurable rule set of keyword→assertion mappings.
When a prompt matches keywords AND the user's context suggests a blind spot,
injects a concise knowledge assertion via additionalContext.

Architecture:
  - Rules: keyword sets + context keywords + assertion text
  - Matching: prompt contains ≥1 keyword AND ≥0 context keywords
  - Dedup: each rule fires at most once per session (StateStore)
  - Toggle: cc_config.json → features.insight_engine.enabled (default true)
  - Custom rules: cc_config.json → features.insight_engine.custom_rules[]

Design principle:
  - This is knowledge injection, not warning. The user gains new information.
  - Max 1 insight per prompt (highest confidence match wins).
  - /commands and very short prompts are skipped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from concinno.i18n import msg as i18n_msg

# ── Data ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class InsightRule:
    """A single keyword→assertion mapping rule."""

    rule_id: str
    keywords: tuple[str, ...]
    context: tuple[str, ...] = ()
    assertion_key: str = ""
    assertion_fallback: str = ""
    confidence: float = 0.8
    tags: tuple[str, ...] = field(default_factory=tuple)


# ── Built-in rules (split into data to keep functions short) ──

_BUILTIN: tuple[dict, ...] = (
    {
        "rule_id": "cli_free_on_max",
        "keywords": ("API", "api key", "per-token", "token cost", "付費", "收費"),
        "context": ("Claude", "claude", "subscription", "Max", "訂閱"),
        "assertion_key": "insight.cli_free_on_max",
        "assertion_fallback": (
            "Claude Code CLI (claude -p) is free with Max/Team subscription. "
            "No API key needed for development and testing."
        ),
        "confidence": 0.9,
    },
    {
        "rule_id": "cli_non_interactive",
        "keywords": ("非互動", "non-interactive", "batch", "自動化", "automation"),
        "context": ("CLI", "claude", "script", "排程", "schedule"),
        "assertion_key": "insight.cli_non_interactive",
        "assertion_fallback": (
            "claude -p 'prompt' runs non-interactively with full tool access. "
            "Supports --output-format json for programmatic use."
        ),
        "confidence": 0.8,
    },
    {
        "rule_id": "hook_bypass_permissions",
        "keywords": ("permission", "權限", "每次都問", "keeps asking", "bypass"),
        "context": ("hook", "extension", "VS Code", "vscode"),
        "assertion_key": "insight.hook_bypass_permissions",
        "assertion_fallback": (
            "VS Code extension has known permission bugs (#29159, #20536). "
            "Workaround: use integrated terminal with "
            "claude --dangerously-skip-permissions for auto-accept."
        ),
        "confidence": 0.85,
    },
    {
        "rule_id": "mcp_server_alternative",
        "keywords": ("MCP", "mcp server", "tool", "工具"),
        "context": ("custom", "自訂", "extend", "擴充"),
        "assertion_key": "insight.mcp_server",
        "assertion_fallback": (
            "Custom tools can be added via MCP servers in "
            ".claude/settings.json → mcpServers. "
            "No need to modify Claude Code source."
        ),
        "confidence": 0.75,
    },
    {
        "rule_id": "hook_types",
        "keywords": ("hook", "Hook", "攔截", "guard"),
        "context": ("when", "何時", "trigger", "觸發", "type", "類型"),
        "assertion_key": "insight.hook_types",
        "assertion_fallback": (
            "Claude Code hooks: PreToolUse (before tool), PostToolUse (after), "
            "UserPromptSubmit (before prompt), Stop (session end), "
            "SubagentSpawn (before subagent). "
            "Configure in .claude/settings.json → hooks."
        ),
        "confidence": 0.7,
    },
    {
        "rule_id": "context_window_management",
        "keywords": ("context", "token", "壓縮", "compressed", "遺忘", "forgot"),
        "context": ("window", "limit", "上限", "memory", "記憶"),
        "assertion_key": "insight.context_management",
        "assertion_fallback": (
            "Context compression is automatic. Use CLAUDE.md for persistent "
            "instructions (always loaded). Use /memory for cross-session recall. "
            "Write handoffs before hitting limits."
        ),
        "confidence": 0.75,
    },
    {
        "rule_id": "subagent_parallel",
        "keywords": ("agent", "Agent", "子代理", "subagent", "parallel"),
        "context": ("slow", "慢", "speed", "快", "performance", "效率"),
        "assertion_key": "insight.subagent_parallel",
        "assertion_fallback": (
            "Multiple Agent tool calls in a single message run in parallel. "
            "Use this for independent research/search tasks to save time."
        ),
        "confidence": 0.7,
    },
)


def _builtin_rules() -> list[InsightRule]:
    """Convert built-in rule dicts to InsightRule objects."""
    return [InsightRule(**r) for r in _BUILTIN]


# ── Custom rules ─────────────────────────────────────────────


def _load_custom_rules() -> list[InsightRule]:
    """Load user-defined insight rules from config."""
    try:
        from concinno.core.config import get_config
        cfg = get_config()
        raw_rules = cfg.feature("insight_engine", "custom_rules")
        if not isinstance(raw_rules, list):
            return []
        return [
            InsightRule(
                rule_id=r["rule_id"],
                keywords=tuple(r.get("keywords", ())),
                context=tuple(r.get("context", ())),
                assertion_key=r.get("assertion_key", ""),
                assertion_fallback=r.get("assertion_fallback", ""),
                confidence=float(r.get("confidence", 0.7)),
            )
            for r in raw_rules
            if isinstance(r, dict) and "rule_id" in r
        ]
    except Exception:
        return []


def load_insight_rules() -> list[InsightRule]:
    """Load all insight rules (built-in + custom)."""
    return _builtin_rules() + _load_custom_rules()


# ── Matching ─────────────────────────────────────────────────


def _match_rule(prompt_lower: str, rule: InsightRule) -> bool:
    """Check if a prompt matches a rule's keyword + context requirements."""
    if not any(kw.lower() in prompt_lower for kw in rule.keywords):
        return False
    if rule.context and not any(
        ctx.lower() in prompt_lower for ctx in rule.context
    ):
        return False
    return True


def _find_best_match(
    prompt_lower: str,
    rules: list[InsightRule],
    fired: set[str],
) -> Optional[InsightRule]:
    """Find highest-confidence unfired matching rule."""
    matches = [r for r in rules if _match_rule(prompt_lower, r)]
    matches.sort(key=lambda r: r.confidence, reverse=True)
    for rule in matches:
        if rule.rule_id not in fired:
            return rule
    return None


# ── Session dedup ────────────────────────────────────────────


def _get_fired_rules(cache_dir: str, session_id: str) -> set[str]:
    """Read already-fired rule IDs for this session."""
    try:
        from concinno.core.state_store import StateStore
        store = StateStore(cache_dir)
        state = store.read("insight_engine", session_id, default={})
        fired = state.get("fired_rules", [])
        return set(fired) if isinstance(fired, list) else set()
    except Exception:
        return set()


def _record_fired(cache_dir: str, session_id: str, rule_id: str) -> None:
    """Record a rule as fired for session dedup."""
    try:
        from concinno.core.state_store import StateStore
        store = StateStore(cache_dir)
        state = store.read("insight_engine", session_id, default={})
        fired = state.get("fired_rules", [])
        if not isinstance(fired, list):
            fired = []
        fired.append(rule_id)
        state["fired_rules"] = fired
        store.write("insight_engine", session_id, state)
    except Exception:
        pass


# ── Resolve assertion text ───────────────────────────────────


def _resolve_assertion(rule: InsightRule) -> str:
    """Get assertion text from i18n key or fallback."""
    if rule.assertion_key:
        text = i18n_msg(rule.assertion_key)
        if text != rule.assertion_key:
            return text
    return rule.assertion_fallback


# ── Public API ───────────────────────────────────────────────


def _is_enabled() -> bool:
    """Check if insight_engine feature is enabled in config."""
    try:
        from concinno.core.config import get_config
        return get_config().feature("insight_engine", "enabled") is not False
    except Exception:
        return True


def check_insight(
    prompt: str,
    *,
    cache_dir: str = "",
    session_id: str = "",
) -> Optional[str]:
    """Check user prompt for blind spots and return knowledge assertion.

    Returns assertion string for additionalContext, or None.
    """
    if not prompt or len(prompt) < 10:
        return None
    if prompt.strip().startswith("/"):
        return None
    if not _is_enabled():
        return None

    fired = set()
    if cache_dir and session_id:
        fired = _get_fired_rules(cache_dir, session_id)

    rule = _find_best_match(prompt.lower(), load_insight_rules(), fired)
    if not rule:
        return None

    assertion = _resolve_assertion(rule)
    if not assertion:
        return None

    if cache_dir and session_id:
        _record_fired(cache_dir, session_id, rule.rule_id)

    return f"💡 {assertion}"
