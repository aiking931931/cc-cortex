"""cc_cortex.mcp_server -- MCP Server for CC Cortex.

@module mcp_server
@responsibility Expose status/metrics/knowledge via JSON-RPC stdio
@dependencies (none — standalone, reads JSON files directly)
@exports run_stdio_server, handle_request, handle_status,
    handle_doctor

Exposes CC Cortex status, metrics, and knowledge via JSON-RPC over stdio.
Zero external dependencies -- hand-rolled MCP protocol implementation.

Resources:
    cc-cortex://session/status    -- Current session state
    cc-cortex://metrics/quality   -- B4 quality scores
    cc-cortex://metrics/tokens    -- Token usage stats
    cc-cortex://knowledge/stats   -- Knowledge base statistics

Tools:
    cc-cortex-status  -- Full module status dashboard
    cc-cortex-doctor  -- Health check

Usage:
    python -m cc_cortex.mcp_server
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

# ── MCP Protocol Constants ────────────────────────────────

MCP_PROTOCOL_VERSION = "2025-03-26"
SERVER_NAME = "cc-cortex"
SERVER_VERSION = "0.3.0"

RESOURCES = [
    {
        "uri": "cc-cortex://session/status",
        "name": "Session Status",
        "description": "Session state: ID, start time, active files, token usage, quality grade",
        "mimeType": "application/json",
    },
    {
        "uri": "cc-cortex://metrics/quality",
        "name": "Quality Metrics",
        "description": "B4 quality scores: completion, accuracy, focus, efficiency, overall grade",
        "mimeType": "application/json",
    },
    {
        "uri": "cc-cortex://metrics/tokens",
        "name": "Token Usage",
        "description": "Token consumption: current usage, budget, percentage, tier level",
        "mimeType": "application/json",
    },
    {
        "uri": "cc-cortex://knowledge/stats",
        "name": "Knowledge Stats",
        "description": (
            "Knowledge base statistics: total entries, recent corrections, staleness ratio"
        ),
        "mimeType": "application/json",
    },
]

TOOLS = [
    {
        "name": "cc-cortex-status",
        "description": (
            "Returns full module status dashboard (equivalent to `cc-cortex status` CLI)"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "cc-cortex-doctor",
        "description": (
            "Run health check on CC Cortex installation (equivalent to `cc-cortex doctor` CLI)"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "cc-cortex-rag-search",
        "description": (
            "Semantic search over knowledge base (skills, rules, handoffs). "
            "Use this to recall cross-session knowledge, find relevant context, "
            "or look up past decisions. Returns top matching chunks with scores."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language search query",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Max results (default 5)",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "cc-cortex-rag-build",
        "description": (
            "Build or rebuild the RAG vector index over knowledge files. "
            "Run this after major skill/knowledge changes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "force": {
                    "type": "boolean",
                    "description": "Force full rebuild (default false)",
                    "default": False,
                },
            },
            "required": [],
        },
    },
    {
        "name": "cc-cortex-recommendations",
        "description": (
            "Proactive session health analysis. Returns actionable recommendations "
            "based on current session state: failure patterns, guard statistics, "
            "token budget, and unresolved issues. Call this periodically or when "
            "feeling stuck to get CCC's perspective."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "cc-cortex-failure-patterns",
        "description": (
            "Analyze tool failure history. Returns recurring failure patterns "
            "with prescriptions. Use this to understand systemic issues and "
            "avoid repeating the same mistakes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tool_filter": {
                    "type": "string",
                    "description": "Filter by tool name (optional)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max patterns to return (default 10)",
                    "default": 10,
                },
            },
            "required": [],
        },
    },
    {
        "name": "cc-cortex-guard-report",
        "description": (
            "Guard pipeline statistics: deny counts, top triggered guards, "
            "health status per guard. Use this to understand what CCC is "
            "catching and whether guards need tuning."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "cc-cortex-analyze-intent",
        "description": (
            "Semantic intent analysis for ambiguous commands. Goes beyond "
            "regex pattern matching by analyzing command structure, context, "
            "and risk indicators. Returns risk assessment and recommendations. "
            "Use this when unsure if a command is safe."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The command or action to analyze",
                },
                "context": {
                    "type": "string",
                    "description": "Additional context (what you're trying to do)",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "cc-cortex-confirm-action",
        "description": (
            "Ask the user for confirmation via MCP elicitation. Use this when "
            "a destructive or high-risk action needs explicit user approval. "
            "Returns the user's decision (accept/decline/dismiss)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "What to ask the user (explain the action and its impact)",
                },
                "risk_level": {
                    "type": "string",
                    "enum": ["medium", "high", "critical"],
                    "description": "Risk level of the action",
                    "default": "high",
                },
            },
            "required": ["message"],
        },
    },
    {
        "name": "cc-cortex-sync-state",
        "description": (
            "Export or import session state for cross-machine synchronization. "
            "Export produces a portable JSON bundle of session state, guard "
            "configs, and knowledge. Import merges remote state into local."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["export", "import"],
                    "description": "Export local state or import remote state",
                },
                "remote_state": {
                    "type": "object",
                    "description": "Remote state bundle (required for import)",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "cc-cortex-daily-reflection",
        "description": (
            "Daily self-reflection: corrections received, guards triggered, "
            "patterns detected, actionable improvements. Call at end of day "
            "or after intensive sessions to consolidate learning."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "cc-cortex-weekly-evolution",
        "description": (
            "Weekly evolution report: guard trigger trends (this week vs last), "
            "correction velocity, rule upgrade candidates. Call weekly to "
            "identify recurring patterns and evolve the guard configuration."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "cc-cortex-screenshot",
        "description": (
            "Take a screenshot for WIREDO visual verification. "
            "Auto-detects method: Playwright headless (preferred) or "
            "windows-mcp Screenshot fallback. Returns file path of screenshot."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "URL to screenshot (default: localhost dev server)",
                    "default": "http://localhost:3000",
                },
                "viewport": {
                    "type": "string",
                    "enum": ["desktop", "mobile", "both"],
                    "description": "Viewport size (default: both)",
                    "default": "both",
                },
            },
            "required": [],
        },
    },
    {
        "name": "cc-cortex-progress",
        "description": (
            "Session progress report: task completion, cost tracking, "
            "error recovery status. Use this for stakeholder communication "
            "or to check how far along the session is."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "cc-cortex-cost",
        "description": (
            "Token cost breakdown: input/output tokens, estimated USD, "
            "budget ceiling, percentage used. Use this to monitor spend."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


# ── Data Readers ──────────────────────────────────────────


def _read_json_file(path: str) -> dict:
    """Read a JSON file, returning empty dict on failure."""
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _cfg():
    """Return the Config singleton, initialised with default hooks dir."""
    from cc_cortex.core.config import get_config

    hooks_dir = os.path.join(os.path.expanduser("~"), ".claude", "hooks")
    return get_config(hooks_dir=hooks_dir)


# ── Resource Data Providers ───────────────────────────────


def read_session_status() -> dict:
    """Read current session status from instance_lock.json and session state."""
    cfg = _cfg()
    lock_path = cfg.path("instance_lock")
    if not lock_path:
        lock_path = os.path.join(
            cfg.workspace,
            ".claude-forge",
            "state",
            "instance_lock.json",
        )

    lock_data = _read_json_file(lock_path)
    sessions = lock_data.get("sessions", {})

    # Find current session (most recently active)
    current = None
    latest_ts = ""
    for _key, session in sessions.items():
        la = session.get("last_active", "")
        if la > latest_ts:
            latest_ts = la
            current = session

    if not current:
        return {
            "session_id": None,
            "start_time": None,
            "active_files": [],
            "active_sessions_count": 0,
            "token_usage": None,
            "quality_grade": None,
        }

    # Read token state if available
    token_state_dir = os.path.join(os.path.expanduser("~"), ".claude", "token_state")
    session_id = current.get("session_id", "")
    short_id = session_id[:8] if session_id else ""
    token_usage = None

    if short_id:
        # Look for token tracking file
        token_file = os.path.join(token_state_dir, f"{short_id}_tokens")
        if os.path.isfile(token_file):
            try:
                with open(token_file, "r") as f:
                    token_usage = int(f.read().strip())
            except Exception:
                pass

    return {
        "session_id": current.get("session_id", ""),
        "start_time": current.get("started", ""),
        "last_active": current.get("last_active", ""),
        "active_files": current.get("files", []),
        "active_files_count": len(current.get("files", [])),
        "holder": current.get("holder", ""),
        "project": current.get("project", ""),
        "task": current.get("task", ""),
        "active_sessions_count": len(sessions),
        "token_usage": token_usage,
        "quality_grade": None,  # Populated from cognitive if available
    }


def read_quality_metrics() -> dict:
    """Read B4 quality scores from cognitive layer."""
    cognitive_dir = os.path.join(
        os.path.expanduser("~"),
        ".claude",
        "cognitive",
    )

    # Read decision journal for quality score
    journal_path = os.path.join(cognitive_dir, "decision_journal.json")
    journal_data = _read_json_file(journal_path)
    entries = journal_data.get("entries", [])

    # Calculate quality dimensions
    scored = [e for e in entries if e.get("outcome")]
    total = len(scored)

    if total == 0:
        return {
            "completion": None,
            "accuracy": None,
            "focus": None,
            "efficiency": None,
            "overall_grade": "N/A",
            "total_decisions": len(entries),
            "scored_decisions": 0,
        }

    weights = {"accepted": 1.0, "ignored": 0.7, "corrected": 0.0, "reverted": 0.0}
    quality = sum(weights.get(e.get("outcome", ""), 0.5) for e in scored) / total

    # Derive dimensional scores from available data
    outcomes = {}
    for e in scored:
        o = e.get("outcome", "unknown")
        outcomes[o] = outcomes.get(o, 0) + 1

    accepted_ratio = outcomes.get("accepted", 0) / total if total else 0
    corrected_ratio = outcomes.get("corrected", 0) / total if total else 0

    # Grade mapping
    if quality >= 0.9:
        grade = "A+"
    elif quality >= 0.8:
        grade = "A"
    elif quality >= 0.7:
        grade = "B+"
    elif quality >= 0.6:
        grade = "B"
    elif quality >= 0.5:
        grade = "C"
    elif quality >= 0.4:
        grade = "D"
    else:
        grade = "F"

    return {
        "completion": round(accepted_ratio, 3),
        "accuracy": round(1.0 - corrected_ratio, 3),
        "focus": round(quality, 3),
        "efficiency": round(quality, 3),
        "overall_grade": grade,
        "overall_score": round(quality, 3),
        "total_decisions": len(entries),
        "scored_decisions": total,
        "outcomes": outcomes,
    }


def read_token_usage() -> dict:
    """Read token usage from token tracking files."""
    cfg = _cfg()
    budget = cfg.threshold("default_token_budget", 40000)

    token_state_dir = os.path.join(os.path.expanduser("~"), ".claude", "token_state")
    current_usage = 0

    # Read the most recent token state file
    if os.path.isdir(token_state_dir):
        try:
            for f in sorted(os.listdir(token_state_dir), reverse=True):
                if f.endswith("_tokens"):
                    fpath = os.path.join(token_state_dir, f)
                    try:
                        with open(fpath, "r") as fh:
                            current_usage = int(fh.read().strip())
                        break
                    except Exception:
                        continue
        except Exception:
            pass

    percentage = round(current_usage / budget * 100, 1) if budget > 0 else 0

    # Determine tier based on token warnings config
    if current_usage >= 180000:
        tier = "emergency"
    elif current_usage >= 140000:
        tier = "critical"
    elif current_usage >= 100000:
        tier = "warn"
    else:
        tier = "info"

    return {
        "current_usage": current_usage,
        "budget": budget,
        "percentage": percentage,
        "tier": tier,
        "tier_thresholds": {
            "info": 0,
            "warn": 100000,
            "critical": 140000,
            "emergency": 180000,
        },
    }


def read_knowledge_stats() -> dict:
    """Read knowledge base statistics from learnings.json."""
    cfg = _cfg()
    learnings_path = cfg.path("learnings")
    if not learnings_path:
        learnings_path = os.path.join(
            cfg.workspace,
            ".claude-forge",
            "memory",
            "evolution",
            "learnings.json",
        )

    data = _read_json_file(learnings_path)
    learnings = data.get("learnings", [])
    total = len(learnings)

    if total == 0:
        return {
            "total_entries": 0,
            "recent_corrections": 0,
            "staleness_ratio": 0.0,
            "promoted_count": 0,
            "high_frequency_count": 0,
        }

    # Count recent corrections (last 7 days)
    now = datetime.now(timezone.utc)
    recent_count = 0
    stale_count = 0
    promoted_count = 0
    high_freq_count = 0

    for item in learnings:
        # Recent
        last_seen = item.get("last_seen", "")
        if last_seen:
            try:
                ts = datetime.fromisoformat(last_seen)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                days_ago = (now - ts).days
                if days_ago <= 7:
                    recent_count += 1
                if days_ago > 90:
                    stale_count += 1
            except (ValueError, TypeError):
                pass

        # Promoted
        if item.get("promoted"):
            promoted_count += 1

        # High frequency
        if item.get("count", 0) >= 3:
            high_freq_count += 1

    staleness_ratio = round(stale_count / total, 3) if total else 0.0

    return {
        "total_entries": total,
        "recent_corrections": recent_count,
        "staleness_ratio": staleness_ratio,
        "stale_count": stale_count,
        "promoted_count": promoted_count,
        "high_frequency_count": high_freq_count,
        "last_updated": data.get("last_updated", ""),
    }


# ── Tool Handlers ─────────────────────────────────────────


def handle_status(arguments: dict | None = None) -> str:
    """Execute cc-cortex status and return formatted output."""
    try:
        from cc_cortex.cli.main import MODULES, _load_module_states
    except ImportError:
        MODULES = {}

        def _load_module_states() -> dict:
            return {}

    lines = ["cc-cortex modules:", ""]
    states = _load_module_states()

    if MODULES:
        for name, info in MODULES.items():
            if info.get("required"):
                icon = "locked"
                label = "always on"
            elif name in states:
                icon = "on" if states[name] else "off"
                label = "enabled" if states[name] else "disabled"
            else:
                icon = "on" if info.get("default") else "off"
                label = "default"
            lines.append(f"  [{icon}] {name:20s} {info['description']:40s} ({label})")
    else:
        lines.append("  (module registry not available)")

    config_path = _cfg().config_file_path
    if config_path and os.path.isfile(config_path):
        lines.append(f"\n  Config: {config_path}")
    else:
        lines.append("\n  Config: not found (run `cc-cortex init`)")

    return "\n".join(lines)


def handle_doctor(arguments: dict | None = None) -> str:
    """Execute cc-cortex doctor and return formatted output."""
    lines = ["cc-cortex doctor", ""]
    issues = 0
    hooks_dir = os.path.join(os.path.expanduser("~"), ".claude", "hooks")
    config_path = _cfg().config_file_path or os.path.join(hooks_dir, "cc_config.json")

    # 1. Config file
    if os.path.isfile(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                json.load(f)
            lines.append("  [ok] cc_config.json -- valid")
        except Exception as exc:
            lines.append(f"  [error] cc_config.json -- invalid JSON: {exc}")
            issues += 1
    else:
        lines.append("  [error] cc_config.json -- not found")
        issues += 1

    # 2. Hook files
    hook_files = [
        "on-session-start.py",
        "on-stop.py",
        "on-pre-tool.py",
        "on-post-tool.py",
        "extract-learnings.py",
    ]
    for hf in hook_files:
        path = os.path.join(hooks_dir, hf)
        if os.path.isfile(path):
            lines.append(f"  [ok] {hf}")
        else:
            lines.append(f"  [missing] {hf}")
            issues += 1

    # 3. Settings hook registration
    settings_path = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
    if os.path.isfile(settings_path):
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                settings = json.load(f)
            hooks_cfg = settings.get("hooks", {})
            for hf, event in [
                ("on-session-start.py", "SessionStart"),
                ("on-stop.py", "Stop"),
                ("on-pre-tool.py", "PreToolUse"),
                ("on-post-tool.py", "PostToolUse"),
            ]:
                found = any(
                    hf in h.get("command", "")
                    for group in hooks_cfg.get(event, [])
                    for h in group.get("hooks", [])
                )
                status = "ok" if found else "not registered"
                lines.append(f"  [{status}] settings.json [{event}] -> {hf}")
                if not found:
                    issues += 1
        except Exception:
            lines.append("  [error] settings.json -- invalid JSON")
            issues += 1
    else:
        lines.append("  [warn] settings.json -- not found")
        issues += 1

    lines.append("")
    if issues == 0:
        lines.append("  All checks passed!")
    else:
        lines.append(f"  {issues} issue(s) found. Run `cc-cortex init` to fix.")

    return "\n".join(lines)


# ── MCP Protocol Handler ─────────────────────────────────


RESOURCE_HANDLERS = {
    "cc-cortex://session/status": read_session_status,
    "cc-cortex://metrics/quality": read_quality_metrics,
    "cc-cortex://metrics/tokens": read_token_usage,
    "cc-cortex://knowledge/stats": read_knowledge_stats,
}

def handle_rag_search(arguments: dict | None = None) -> str:
    """Handle RAG search tool call."""
    args = arguments or {}
    query = args.get("query", "")
    top_k = args.get("top_k", 5)
    if not query:
        return "Error: query is required"
    try:
        from cc_cortex.rag import RAGIndex

        idx = RAGIndex(project_dir=_cfg().workspace)
        results = idx.search(query, top_k=top_k)
        if not results:
            return "No results found. Try building the index first: cc-cortex-rag-build"
        lines = []
        for r in results:
            lines.append(f"[{r['score']:.3f}] {r['file']} — {r['heading']}")
            lines.append(f"  {r['text'][:300]}")
            lines.append("")
        return "\n".join(lines)
    except ImportError:
        return "RAG dependencies not installed. Run: pip install cc-cortex[rag]"
    except Exception as exc:
        return f"RAG search error: {exc}"


def handle_rag_build(arguments: dict | None = None) -> str:
    """Handle RAG index build tool call."""
    args = arguments or {}
    force = args.get("force", False)
    try:
        from cc_cortex.rag import RAGIndex

        idx = RAGIndex(project_dir=_cfg().workspace)
        result = idx.build(force=force)
        return json.dumps(result, indent=2, ensure_ascii=False)
    except ImportError:
        return "RAG dependencies not installed. Run: pip install cc-cortex[rag]"
    except Exception as exc:
        return f"RAG build error: {exc}"


def handle_recommendations(arguments: dict | None = None) -> str:
    """Proactive session health analysis with actionable recommendations."""
    recs: list[str] = []

    # Token health
    tokens = read_token_usage()
    tier = tokens.get("tier", "info")
    pct = tokens.get("percentage", 0)
    if tier in ("critical", "emergency"):
        recs.append(f"🔴 Token {tier}: {pct}% used. Handoff soon.")
    elif tier == "warn":
        recs.append(f"🟡 Token warn: {pct}% used. Be concise.")

    # Failure patterns
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    fail_path = os.path.join(
        project_dir, ".cc_cortex_cache", "tool_failures.jsonl",
    ) if project_dir else ""
    if fail_path and os.path.isfile(fail_path):
        fails: dict[str, int] = {}
        try:
            with open(fail_path, encoding="utf-8") as f:
                for line in f:
                    e = json.loads(line.strip())
                    key = f"{e.get('tool', '?')}:{e.get('category', '?')}"
                    fails[key] = fails.get(key, 0) + 1
        except Exception:
            pass
        hot = [(k, v) for k, v in fails.items() if v >= 3]
        for k, v in sorted(hot, key=lambda x: -x[1])[:3]:
            recs.append(f"⚠ Recurring failure: {k} ({v}x)")

    # Knowledge health
    kb = read_knowledge_stats()
    stale = kb.get("staleness_ratio", 0)
    if stale > 0.3:
        recs.append(
            f"📚 {int(stale*100)}% knowledge stale (>90d). "
            "Run knowledge pruning."
        )

    if not recs:
        return "✅ Session healthy. No recommendations."
    return "📋 Recommendations:\n" + "\n".join(f"  {i+1}. {r}" for i, r in enumerate(recs))


def handle_failure_patterns(arguments: dict | None = None) -> str:
    """Analyze tool failure history for recurring patterns."""
    args = arguments or {}
    tool_filter = args.get("tool_filter", "")
    limit = args.get("limit", 10)

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    fail_path = os.path.join(
        project_dir, ".cc_cortex_cache", "tool_failures.jsonl",
    ) if project_dir else ""

    if not fail_path or not os.path.isfile(fail_path):
        return "No failure history found."

    patterns: dict[str, dict] = {}
    try:
        with open(fail_path, encoding="utf-8") as f:
            for line in f:
                e = json.loads(line.strip())
                tool = e.get("tool", "?")
                if tool_filter and tool != tool_filter:
                    continue
                cat = e.get("category", "other")
                key = f"{tool}:{cat}"
                if key not in patterns:
                    patterns[key] = {
                        "count": 0,
                        "last_error": "",
                        "last_ts": "",
                    }
                patterns[key]["count"] += 1
                patterns[key]["last_error"] = e.get("error_preview", "")[:100]
                patterns[key]["last_ts"] = e.get("ts", "")
    except Exception as exc:
        return f"Error reading failures: {exc}"

    if not patterns:
        return "No failure patterns found."

    sorted_p = sorted(patterns.items(), key=lambda x: -x[1]["count"])
    lines = ["Tool Failure Patterns:", ""]
    for key, data in sorted_p[:limit]:
        lines.append(f"  {key}: {data['count']}x")
        lines.append(f"    Last: {data['last_error']}")
        lines.append(f"    Time: {data['last_ts']}")
        lines.append("")
    return "\n".join(lines)


def handle_guard_report(arguments: dict | None = None) -> str:
    """Guard pipeline statistics from audit logs."""
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    audit_path = os.path.join(
        project_dir, ".cc_cortex_cache", "audit", "guard_denies.jsonl",
    ) if project_dir else ""

    denies: dict[str, int] = {}
    total = 0

    if audit_path and os.path.isfile(audit_path):
        try:
            with open(audit_path, encoding="utf-8") as f:
                for line in f:
                    e = json.loads(line.strip())
                    guard = e.get("guard", "unknown")
                    denies[guard] = denies.get(guard, 0) + 1
                    total += 1
        except Exception:
            pass

    if not denies:
        return "No guard deny events recorded yet."

    lines = [f"Guard Pipeline Report ({total} total denies):", ""]
    for guard, count in sorted(denies.items(), key=lambda x: -x[1]):
        lines.append(f"  {guard}: {count} denies")
    return "\n".join(lines)


def _semantic_risk_score(command: str) -> tuple[str, list[str]]:
    """Semantic intent analysis beyond regex — heuristic NLP."""
    import re

    indicators: list[str] = []
    risk = 0.0

    # Destructive verbs
    destructive = re.findall(
        r'\b(rm|del|delete|drop|truncate|kill|destroy|wipe|purge|reset)\b',
        command, re.I,
    )
    if destructive:
        risk += 0.3 * len(destructive)
        indicators.append(f"Destructive verbs: {', '.join(destructive)}")

    # Scope amplifiers (exclude dots in filenames like .md .py)
    amplifiers = re.findall(
        r'(-rf?|--force|--hard|--all(?!\w)|\*|--recursive)', command, re.I,
    )
    if amplifiers:
        risk += 0.2 * len(amplifiers)
        indicators.append(f"Scope amplifiers: {', '.join(amplifiers)}")

    # Irreversibility markers
    if re.search(r'--force|--hard|--no-verify|--skip', command, re.I):
        risk += 0.2
        indicators.append("Irreversibility bypass flags detected")

    # Network exfiltration
    if re.search(r'curl.*-d|wget.*\|.*sh|nc\s', command, re.I):
        risk += 0.4
        indicators.append("Potential data exfiltration pattern")

    # Privilege escalation
    if re.search(r'sudo|chmod\s+777|chown.*root', command, re.I):
        risk += 0.3
        indicators.append("Privilege escalation attempt")

    # Pipe to shell
    if re.search(r'\|\s*(ba)?sh|\|\s*python', command, re.I):
        risk += 0.3
        indicators.append("Pipe-to-shell execution")

    # Clamp risk
    risk = min(risk, 1.0)

    if risk >= 0.7:
        level = "HIGH"
    elif risk >= 0.4:
        level = "MEDIUM"
    elif risk >= 0.1:
        level = "LOW"
    else:
        level = "SAFE"

    return level, indicators


def handle_analyze_intent(arguments: dict | None = None) -> str:
    """Semantic intent analysis for ambiguous commands."""
    args = arguments or {}
    command = args.get("command", "")
    context = args.get("context", "")

    if not command:
        return "Error: command is required"

    level, indicators = _semantic_risk_score(command)

    lines = [f"Intent Analysis: {command[:80]}", ""]
    lines.append(f"Risk Level: {level}")
    if indicators:
        lines.append("Indicators:")
        for ind in indicators:
            lines.append(f"  • {ind}")
    else:
        lines.append("No risk indicators detected.")

    if context:
        lines.append(f"\nContext: {context}")

    if level in ("HIGH", "MEDIUM"):
        lines.append("\nRecommendation: Verify intent before executing.")
        lines.append("Consider: backup first, use --dry-run, or break into smaller steps.")

    return "\n".join(lines)


def _do_sync_import(remote: dict, project_dir: str) -> str:
    """Merge remote state bundle into local state."""
    merged: list[str] = []

    # 1. Merge failure patterns (additive — remote counts add to local)
    remote_fails = remote.get("failure_patterns", {})
    if remote_fails and project_dir:
        fail_path = os.path.join(
            project_dir, ".cc_cortex_cache", "tool_failures.jsonl",
        )
        os.makedirs(os.path.dirname(fail_path), exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat()
        try:
            with open(fail_path, "a", encoding="utf-8") as f:
                for key, count in remote_fails.items():
                    parts = key.split(":", 1)
                    tool = parts[0] if parts else "unknown"
                    cat = parts[1] if len(parts) > 1 else "other"
                    entry = json.dumps({
                        "ts": ts, "tool": tool, "category": cat,
                        "error_preview": f"[synced from remote x{count}]",
                        "count": count, "source": "sync_import",
                    }, ensure_ascii=False)
                    f.write(entry + "\n")
            merged.append(f"✅ Failure patterns: {len(remote_fails)} merged")
        except Exception as exc:
            merged.append(f"❌ Failure patterns: {exc}")

    # 2. Merge guard config (remote overrides local for matching keys)
    remote_cfg = remote.get("guard_config", {})
    if remote_cfg and project_dir:
        cfg_path = os.path.join(
            project_dir, ".cc_cortex_cache", "cc_config.json",
        )
        local_cfg = _read_json_file(cfg_path) if os.path.isfile(cfg_path) else {}
        for k, v in remote_cfg.items():
            if isinstance(v, dict) and isinstance(local_cfg.get(k), dict):
                local_cfg[k].update(v)
            else:
                local_cfg[k] = v
        try:
            os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(local_cfg, f, indent=2, ensure_ascii=False)
            merged.append(f"✅ Guard config: {len(remote_cfg)} keys merged")
        except Exception as exc:
            merged.append(f"❌ Guard config: {exc}")

    # 3. Log sync event to audit
    if project_dir:
        audit_dir = os.path.join(project_dir, ".cc_cortex_cache", "audit")
        os.makedirs(audit_dir, exist_ok=True)
        try:
            with open(
                os.path.join(audit_dir, "sync_log.jsonl"),
                "a", encoding="utf-8",
            ) as f:
                f.write(json.dumps({
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "action": "import",
                    "remote_keys": list(remote.keys()),
                    "results": merged,
                }, ensure_ascii=False) + "\n")
        except Exception:
            pass

    if not merged:
        return "Nothing to import (remote_state had no mergeable data)."
    return "Sync Import Complete:\n" + "\n".join(f"  {m}" for m in merged)


def handle_sync_state(arguments: dict | None = None) -> str:
    """Export/import session state for cross-machine sync."""
    args = arguments or {}
    action = args.get("action", "export")
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")

    if action == "export":
        bundle: dict[str, Any] = {
            "version": SERVER_VERSION,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session": read_session_status(),
            "tokens": read_token_usage(),
            "knowledge": read_knowledge_stats(),
            "quality": read_quality_metrics(),
        }

        # Include guard config
        cfg_path = os.path.join(project_dir, ".cc_cortex_cache", "cc_config.json")
        if os.path.isfile(cfg_path):
            bundle["guard_config"] = _read_json_file(cfg_path)

        # Include failure summary (not raw logs)
        fail_path = os.path.join(
            project_dir, ".cc_cortex_cache", "tool_failures.jsonl",
        )
        if os.path.isfile(fail_path):
            patterns: dict[str, int] = {}
            try:
                with open(fail_path, encoding="utf-8") as f:
                    for line in f:
                        e = json.loads(line.strip())
                        key = f"{e.get('tool')}:{e.get('category')}"
                        patterns[key] = patterns.get(key, 0) + 1
            except Exception:
                pass
            bundle["failure_patterns"] = patterns

        return json.dumps(bundle, indent=2, ensure_ascii=False)

    if action == "import":
        remote = args.get("remote_state", {})
        if not remote:
            return "Error: remote_state is required for import"
        return _do_sync_import(remote, project_dir)

    return f"Unknown action: {action}. Use 'export' or 'import'."


def handle_confirm_action(*, arguments: dict) -> str:
    """Ask user for confirmation via MCP elicitation."""
    message = arguments.get("message", "")
    if not message:
        return json.dumps({"error": "message is required"})

    risk_level = arguments.get("risk_level", "high")
    risk_emoji = {"medium": "\U0001f7e0", "high": "\U0001f534", "critical": "\U0001f480"}.get(
        risk_level, "\U0001f534"
    )

    prompt = f"{risk_emoji} {risk_level.upper()} RISK\n\n{message}\n\nDo you want to proceed?"

    try:
        result = elicit(
            message=prompt,
            schema={
                "type": "object",
                "properties": {
                    "confirmed": {
                        "type": "boolean",
                        "title": "Confirm action",
                        "description": message,
                    },
                    "reason": {
                        "type": "string",
                        "title": "Reason (optional)",
                        "description": "Why you want to proceed",
                    },
                },
                "required": ["confirmed"],
            },
        )
        return json.dumps(result, ensure_ascii=False)
    except ElicitationError as exc:
        return json.dumps({
            "action": "error",
            "error": str(exc),
            "fallback": "Use #DESTROY_CONFIRMED tag to confirm via text",
        })


def _load_today_learnings(cache_dir: str, date_str: str) -> list[dict]:
    """Load learnings for a specific date from cache."""
    path = os.path.join(cache_dir, "learnings.json")
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [
            item for item in (data if isinstance(data, list) else [])
            if isinstance(item, dict) and item.get("date", "").startswith(date_str)
        ]
    except Exception:
        return []


def _count_audit_by_guard(
    cache_dir: str, date_filter: str = "",
) -> dict[str, int]:
    """Count guard denies from audit log, optionally filtered by date prefix."""
    path = os.path.join(cache_dir, "audit", "guard_denies.jsonl")
    if not os.path.isfile(path):
        return {}
    counts: dict[str, int] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = entry.get("timestamp", "")
                if date_filter and not ts.startswith(date_filter):
                    continue
                guard = entry.get("guard", "unknown")
                counts[guard] = counts.get(guard, 0) + 1
    except Exception:
        pass
    return counts


def _parse_audit_weekly(
    cache_dir: str,
) -> tuple[dict[str, int], dict[str, int]]:
    """Parse audit log into this-week and last-week guard counts."""
    path = os.path.join(cache_dir, "audit", "guard_denies.jsonl")
    if not os.path.isfile(path):
        return {}, {}
    now = datetime.now(timezone.utc)
    this_week: dict[str, int] = {}
    last_week: dict[str, int] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    ts_str = entry.get("timestamp", "")
                    if not ts_str:
                        continue
                    ts_date = datetime.fromisoformat(
                        ts_str.replace("Z", "+00:00")
                    ).date()
                    guard = entry.get("guard", "unknown")
                    days_ago = (now.date() - ts_date).days
                    if days_ago <= 7:
                        this_week[guard] = this_week.get(guard, 0) + 1
                    elif days_ago <= 14:
                        last_week[guard] = last_week.get(guard, 0) + 1
                except (json.JSONDecodeError, ValueError):
                    continue
    except Exception:
        pass
    return this_week, last_week


def handle_daily_reflection(arguments: dict | None = None) -> str:
    """Generate daily self-reflection report.

    Analyzes today's session activity: corrections received, patterns detected,
    guards triggered, knowledge gained. Outputs actionable improvements.
    """
    cache_dir = os.path.join(
        os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()),
        ".cc_cortex_cache",
    )

    report: dict[str, Any] = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "corrections": [],
        "guard_triggers": {},
        "patterns_detected": [],
        "improvements": [],
        "knowledge_gained": 0,
    }

    # 1. Count corrections from learnings
    today_items = _load_today_learnings(cache_dir, report["date"])
    report["corrections"] = [
        {"pattern": item.get("pattern", ""), "correction": item.get("correction", "")}
        for item in today_items[:10]
    ]
    report["knowledge_gained"] = len(today_items)

    # 2. Guard trigger counts from audit log
    report["guard_triggers"] = _count_audit_by_guard(cache_dir, report["date"])

    # 3. Generate improvements
    if report["guard_triggers"]:
        top_guard = max(report["guard_triggers"], key=report["guard_triggers"].get)
        count = report["guard_triggers"][top_guard]
        report["improvements"].append(
            f"Most triggered guard: {top_guard} ({count}x). "
            f"Review if this indicates a recurring pattern."
        )

    if report["knowledge_gained"] == 0:
        report["improvements"].append(
            "No new knowledge captured today. "
            "Consider reviewing corrections and saving patterns."
        )

    if not report["guard_triggers"]:
        report["improvements"].append(
            "No guard triggers today — either clean session or "
            "guards may need tuning."
        )

    total_denies = sum(report["guard_triggers"].values())
    report["summary"] = (
        f"Daily Reflection ({report['date']}): "
        f"{report['knowledge_gained']} corrections captured, "
        f"{total_denies} guard triggers, "
        f"{len(report['improvements'])} improvements suggested."
    )

    return json.dumps(report, indent=2, ensure_ascii=False)


def handle_weekly_evolution(arguments: dict | None = None) -> str:
    """Generate weekly evolution report with trend analysis.

    Compares this week vs last week: guard trigger trends, correction velocity,
    knowledge growth rate. Suggests rule upgrades and guard tuning.
    """
    cache_dir = os.path.join(
        os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd()),
        ".cc_cortex_cache",
    )

    report: dict[str, Any] = {
        "week_ending": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "guard_trend": {},
        "correction_velocity": 0,
        "rule_upgrade_candidates": [],
        "recommendations": [],
    }

    # 1. Weekly guard trends from audit log
    this_week, last_week = _parse_audit_weekly(cache_dir)
    all_guards = set(this_week) | set(last_week)
    for g in sorted(all_guards):
        tw = this_week.get(g, 0)
        lw = last_week.get(g, 0)
        if lw == 0:
            trend = "new" if tw > 0 else "none"
        else:
            change = ((tw - lw) / lw) * 100
            trend = f"{change:+.0f}%"
        report["guard_trend"][g] = {
            "this_week": tw, "last_week": lw, "trend": trend,
        }

    # 2. Correction velocity from learnings (last 7 days)
    now = datetime.now(timezone.utc)
    from datetime import timedelta
    cutoff = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    learnings_path = os.path.join(cache_dir, "learnings.json")
    if os.path.isfile(learnings_path):
        try:
            with open(learnings_path, "r", encoding="utf-8") as f:
                learnings = json.load(f)
            if isinstance(learnings, list):
                report["correction_velocity"] = sum(
                    1 for item in learnings
                    if isinstance(item, dict)
                    and item.get("date", "") >= cutoff
                )
        except Exception:
            pass

    # 3. Rule upgrade candidates (guards triggered ≥3x this week)
    for g, data in report["guard_trend"].items():
        if data["this_week"] >= 3:
            report["rule_upgrade_candidates"].append(
                f"{g} triggered {data['this_week']}x this week — "
                f"consider hardening into a permanent rule."
            )

    # 4. Recommendations
    improving = [
        g for g, d in report["guard_trend"].items()
        if d["last_week"] > 0 and d["this_week"] < d["last_week"]
    ]
    worsening = [
        g for g, d in report["guard_trend"].items()
        if d["this_week"] > d.get("last_week", 0) and d["last_week"] > 0
    ]

    if improving:
        report["recommendations"].append(
            f"Improving: {', '.join(improving)} — fewer triggers this week."
        )
    if worsening:
        report["recommendations"].append(
            f"Worsening: {', '.join(worsening)} — more triggers, needs attention."
        )
    if report["correction_velocity"] == 0:
        report["recommendations"].append(
            "No corrections this week. Either perfect or not learning."
        )

    report["summary"] = (
        f"Weekly Evolution (ending {report['week_ending']}): "
        f"{sum(d['this_week'] for d in report['guard_trend'].values())} "
        f"guard triggers, "
        f"{report['correction_velocity']} corrections, "
        f"{len(report['rule_upgrade_candidates'])} upgrade candidates."
    )

    return json.dumps(report, indent=2, ensure_ascii=False)


def handle_screenshot(arguments: dict | None = None) -> str:
    """WIREDO visual verification via Playwright or windows-mcp."""
    args = arguments or {}
    url = args.get("url", "http://localhost:3000")
    viewport = args.get("viewport", "both")
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", ".")

    # Try Playwright script first
    script = os.path.join(project_dir, "scripts", "tools", "psyche-screenshot.js")
    if os.path.isfile(script):
        return json.dumps({
            "method": "playwright",
            "command": f"node {script} --url {url} --viewport {viewport}",
            "instruction": "Run this Bash command to take screenshots.",
        }, ensure_ascii=False)

    # Fallback: windows-mcp
    return json.dumps({
        "method": "windows-mcp",
        "instruction": (
            "Use mcp__windows-mcp__Screenshot to capture the browser window. "
            f"Navigate to {url} first."
        ),
    }, ensure_ascii=False)


def handle_progress(arguments: dict | None = None) -> str:
    """Session progress report from TaskOrchestrator + CostTracker."""
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", ".")
    cache_dir = os.path.join(project_dir, ".cc_cortex_cache") if project_dir else ""
    session_id = os.environ.get("CC_SESSION_ID", "")

    try:
        from cc_cortex.cost_tracker import CostTracker
        from cc_cortex.progress_reporter import ProgressReporter
        from cc_cortex.task_orchestrator import TaskOrchestrator

        orch = TaskOrchestrator(cache_dir, session_id)
        cost = CostTracker(cache_dir, session_id)
        reporter = ProgressReporter(orch, cost)
        return reporter.generate_report()
    except Exception as e:
        return json.dumps({"error": str(e)[:200]}, ensure_ascii=False)


def handle_cost(arguments: dict | None = None) -> str:
    """Token cost breakdown."""
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", ".")
    cache_dir = os.path.join(project_dir, ".cc_cortex_cache") if project_dir else ""
    session_id = os.environ.get("CC_SESSION_ID", "")

    try:
        from cc_cortex.cost_tracker import CostTracker

        tracker = CostTracker(cache_dir, session_id)
        stats = tracker.stats()
        alert = tracker.alert_message()
        if alert:
            stats["alert"] = alert
        return json.dumps(stats, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)[:200]}, ensure_ascii=False)


TOOL_HANDLERS = {
    "cc-cortex-status": handle_status,
    "cc-cortex-doctor": handle_doctor,
    "cc-cortex-rag-search": handle_rag_search,
    "cc-cortex-rag-build": handle_rag_build,
    "cc-cortex-recommendations": handle_recommendations,
    "cc-cortex-failure-patterns": handle_failure_patterns,
    "cc-cortex-guard-report": handle_guard_report,
    "cc-cortex-analyze-intent": handle_analyze_intent,
    "cc-cortex-confirm-action": handle_confirm_action,
    "cc-cortex-sync-state": handle_sync_state,
    "cc-cortex-daily-reflection": handle_daily_reflection,
    "cc-cortex-weekly-evolution": handle_weekly_evolution,
    "cc-cortex-screenshot": handle_screenshot,
    "cc-cortex-progress": handle_progress,
    "cc-cortex-cost": handle_cost,
}


def make_response(id: Any, result: Any) -> dict:
    """Create a JSON-RPC 2.0 response."""
    return {"jsonrpc": "2.0", "id": id, "result": result}


def make_error(id: Any, code: int, message: str, data: Any = None) -> dict:
    """Create a JSON-RPC 2.0 error response."""
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": id, "error": error}


# JSON-RPC error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def _handle_initialize(req_id: Any, _params: dict) -> dict:
    """Handle MCP initialize request."""
    return make_response(
        req_id,
        {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {
                "resources": {"listChanged": False},
                "tools": {"listChanged": False},
                "elicitation": {},
            },
            "serverInfo": {
                "name": SERVER_NAME,
                "version": SERVER_VERSION,
            },
        },
    )


def _handle_resources_list(req_id: Any, _params: dict) -> dict:
    """Handle resources/list request."""
    mcp = _cfg().raw("mcp", {})
    _default_res = [
        "session/status", "metrics/quality",
        "metrics/tokens", "knowledge/stats",
    ]
    enabled = mcp.get("resources", _default_res)
    filtered = [r for r in RESOURCES if any(e in r["uri"] for e in enabled)]
    return make_response(req_id, {"resources": filtered})


def _handle_resources_read(req_id: Any, params: dict) -> dict:
    """Handle resources/read request."""
    uri = params.get("uri", "")
    handler = RESOURCE_HANDLERS.get(uri)
    if not handler:
        return make_error(req_id, INVALID_PARAMS, f"Unknown resource: {uri}")
    try:
        data = handler()
        return make_response(
            req_id,
            {
                "contents": [
                    {
                        "uri": uri,
                        "mimeType": "application/json",
                        "text": json.dumps(data, indent=2, ensure_ascii=False),
                    }
                ],
            },
        )
    except Exception as exc:
        return make_error(req_id, INTERNAL_ERROR, str(exc))


def _handle_tools_list(req_id: Any, _params: dict) -> dict:
    """Handle tools/list request."""
    mcp = _cfg().raw("mcp", {})
    enabled = mcp.get("tools", ["cc-cortex-status", "cc-cortex-doctor"])
    filtered = [t for t in TOOLS if t["name"] in enabled]
    return make_response(req_id, {"tools": filtered})


def _handle_tools_call(req_id: Any, params: dict) -> dict:
    """Handle tools/call request."""
    tool_name = params.get("name", "")
    arguments = params.get("arguments", {})
    handler = TOOL_HANDLERS.get(tool_name)
    if not handler:
        return make_error(req_id, INVALID_PARAMS, f"Unknown tool: {tool_name}")
    try:
        result = handler(arguments=arguments)
        return make_response(
            req_id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": result if isinstance(result, str) else json.dumps(result),
                    }
                ],
            },
        )
    except Exception as exc:
        return make_error(req_id, INTERNAL_ERROR, str(exc))


def _handle_ping(req_id: Any, _params: dict) -> dict:
    """Handle ping request."""
    return make_response(req_id, {})


# Method dispatch table
_METHOD_HANDLERS: dict[str, Any] = {
    "initialize": _handle_initialize,
    "resources/list": _handle_resources_list,
    "resources/read": _handle_resources_read,
    "tools/list": _handle_tools_list,
    "tools/call": _handle_tools_call,
    "ping": _handle_ping,
}

# Methods that are notifications (no response needed even without id)
_NOTIFICATION_METHODS = frozenset({"notifications/initialized"})


def handle_request(request: dict) -> Optional[dict]:
    """Handle a single JSON-RPC request. Returns response dict or None for notifications."""
    if not isinstance(request, dict):
        return make_error(None, INVALID_REQUEST, "Invalid request")

    req_id = request.get("id")
    method = request.get("method", "")
    params = request.get("params", {})

    # Known notifications -- no response
    if method in _NOTIFICATION_METHODS:
        return None

    # Unknown methods without an id are notifications -- ignore
    if req_id is None and method not in _METHOD_HANDLERS:
        return None

    handler = _METHOD_HANDLERS.get(method)
    if not handler:
        return make_error(req_id, METHOD_NOT_FOUND, f"Unknown method: {method}")

    return handler(req_id, params)


# ── Elicitation ───────────────────────────────────────────


class ElicitationError(Exception):
    """Raised when an elicitation request fails or times out."""


class _Transport:
    """Bidirectional stdio transport for MCP JSON-RPC.

    Supports server→client requests (elicitation) in addition to the
    standard client→server request flow.
    """

    def __init__(self, input_stream: Any, output_stream: Any) -> None:
        self._in = input_stream
        self._out = output_stream
        self._server_req_counter = 0

    def write_message(self, message: dict) -> None:
        """Write a JSON-RPC message to the output stream."""
        data = json.dumps(message, ensure_ascii=False).encode("utf-8")
        self._out.write(data + b"\n")
        self._out.flush()

    def read_message(self) -> Optional[dict]:
        """Read one JSON-RPC message from the input stream. None on EOF."""
        line = self._in.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            return None
        return json.loads(line.decode("utf-8"))

    def send_request(
        self, method: str, params: dict, timeout: float = 30.0,
    ) -> dict:
        """Send a server→client request and block until response.

        Used for elicitation: the server asks the client to collect user
        input, then waits for the client's response.

        Returns the ``result`` dict from the client's JSON-RPC response.
        Raises ``ElicitationError`` on timeout, transport close, or
        client-side error.
        """
        self._server_req_counter += 1
        req_id = f"srv-{self._server_req_counter}"
        self.write_message({
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        })

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = self.read_message()
            if msg is None:
                raise ElicitationError("Transport closed during elicitation")

            # Response to our request?
            if msg.get("id") == req_id:
                if "error" in msg:
                    err = msg["error"]
                    raise ElicitationError(
                        err.get("message", "Client returned error"),
                    )
                return msg.get("result", {})

            # Client notification — swallow silently
            if msg.get("method") in _NOTIFICATION_METHODS:
                continue

            # Unexpected message — skip (don't crash)
            continue

        raise ElicitationError(
            f"Timeout ({timeout}s) waiting for elicitation response",
        )


# Module-level transport reference — set by run_stdio_server()
_active_transport: Optional[_Transport] = None


def elicit(
    message: str,
    schema: Optional[dict] = None,
    timeout: float = 30.0,
) -> dict:
    """Send an elicitation request to the MCP client.

    The client will show a form/dialog to the user and return their input.

    Args:
        message: What to ask the user.
        schema: Optional JSON Schema for structured input fields.
        timeout: Max seconds to wait for user response.

    Returns:
        ``{"action": "accept"|"decline"|"dismiss", "content": {...}}``

    Raises:
        ElicitationError: No active transport, timeout, or client error.
    """
    if _active_transport is None:
        raise ElicitationError(
            "No active transport — elicitation requires running as MCP server",
        )

    params: dict[str, Any] = {"message": message}
    if schema is not None:
        params["requestedSchema"] = schema

    return _active_transport.send_request(
        "elicitation/create", params, timeout=timeout,
    )


def elicit_confirm(message: str, timeout: float = 30.0) -> bool:
    """Convenience: ask user for yes/no confirmation via elicitation.

    Returns True if the user confirmed, False otherwise (decline/dismiss/
    timeout/error).
    """
    try:
        result = elicit(
            message=message,
            schema={
                "type": "object",
                "properties": {
                    "confirmed": {
                        "type": "boolean",
                        "title": "Confirm",
                        "description": "Proceed with this action?",
                    },
                },
                "required": ["confirmed"],
            },
            timeout=timeout,
        )
        if result.get("action") == "accept":
            return bool(result.get("content", {}).get("confirmed", False))
        return False
    except ElicitationError:
        return False


# ── Stdio Transport ──────────────────────────────────────


def run_stdio_server() -> None:
    """Run MCP server over stdio transport (line-delimited JSON-RPC)."""
    global _active_transport  # noqa: PLW0603

    # Ensure binary mode for stdin/stdout on Windows
    if sys.platform == "win32":
        import msvcrt

        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)

    transport = _Transport(sys.stdin.buffer, sys.stdout.buffer)
    _active_transport = transport

    try:
        while True:
            try:
                msg = transport.read_message()
                if msg is None:
                    break  # EOF

                response = handle_request(msg)
                if response is not None:
                    transport.write_message(response)

            except KeyboardInterrupt:
                break
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                transport.write_message(
                    make_error(None, PARSE_ERROR, f"Parse error: {exc}"),
                )
            except Exception:
                # Don't crash the server on unexpected errors
                continue
    finally:
        _active_transport = None


# ── Entry Point ───────────────────────────────────────────


def main() -> None:
    """CLI entry point for MCP server."""
    run_stdio_server()


if __name__ == "__main__":
    main()
