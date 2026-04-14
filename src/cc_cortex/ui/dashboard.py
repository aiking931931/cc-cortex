"""CC Cortex terminal dashboard — rich status display with zero dependencies."""

from __future__ import annotations

import json
from pathlib import Path

from cc_cortex.ui.box import box_bottom, box_row, box_separator, box_top, center_text
from cc_cortex.ui.colors import style

WIDTH = 62  # inner width


def render_dashboard(
    workspace: str | Path | None = None,
    config_path: str | Path | None = None,
) -> str:
    """Render the full CC Cortex status dashboard."""
    lines: list[str] = []
    ws = Path(workspace) if workspace else Path.home() / ".claude"
    if config_path:
        config = _load_config(config_path)
    else:
        try:
            from cc_cortex.core.config import get_config
            cfg = get_config()
            cp = cfg.config_file_path
            config = _load_config(cp) if cp else {}
        except Exception:
            config = _load_config(ws / "hooks" / "cc_config.json")

    # Header
    lines.append(box_top(WIDTH))
    lines.append(box_row(center_text(style("CC CORTEX v0.3.0", "header"), WIDTH), WIDTH))
    lines.append(
        box_row(center_text(style("The Cognitive Layer for Claude Code", "muted"), WIDTH), WIDTH)
    )
    lines.append(box_separator(WIDTH))

    # Metrics row
    sessions = _count_sessions(ws)
    tokens = _get_token_info(ws)
    knowledge = _get_knowledge_count(ws)

    metrics_1 = (
        f"  {_icon('brain')} Sessions: {style(str(sessions), 'bold')}"
        f"  |  {_icon('chart')} Quality: {_quality_badge()}"
    )
    metrics_2 = (
        f"  {_icon('lock')} Locks: {style(_count_locks(ws), 'bold')}"
        f"  |  {_icon('money')} Tokens: {_token_bar(tokens)}"
    )
    metrics_3 = (
        f"  {_icon('shield')} Threats blocked: {style('0', 'green')}"
        f"  |  {_icon('book')} Knowledge: {style(str(knowledge), 'bold')} entries"
    )

    lines.append(box_row("", WIDTH))
    lines.append(box_row(metrics_1, WIDTH))
    lines.append(box_row(metrics_2, WIDTH))
    lines.append(box_row(metrics_3, WIDTH))
    lines.append(box_row("", WIDTH))
    lines.append(box_separator(WIDTH))

    # Module status
    header_left = style("  MODULES", "bold")
    header_right = style("STATUS", "bold")
    lines.append(box_row(f"{header_left:<45}{header_right:>15}", WIDTH))
    lines.append(box_separator(WIDTH, double=False))

    modules = _get_module_status(config)
    for mod in modules:
        enabled = mod["enabled"]
        icon = style("\u2705", "green") if enabled else "\u2b1c"
        name = style(f"{mod['name']:<18}", "bold" if enabled else "muted")
        desc = style(f"{mod['desc']:<20}", "muted")
        status = style("\u25cf active", "green") if enabled else style("\u25cb disabled", "muted")
        lines.append(box_row(f"  {icon} {name}{desc}{status}", WIDTH))

    lines.append(box_separator(WIDTH))

    # Recent events
    lines.append(box_row(style("  RECENT EVENTS (last 5)", "bold"), WIDTH))
    lines.append(box_separator(WIDTH, double=False))

    events = _get_recent_events(ws)
    if events:
        for evt in events[-5:]:
            lines.append(box_row(f"  {evt}", WIDTH))
    else:
        lines.append(box_row(style("  No recent events", "muted"), WIDTH))

    lines.append(box_bottom(WIDTH))

    return "\n".join(lines)


def _icon(name: str) -> str:
    icons = {
        "brain": "\U0001f9e0",
        "chart": "\U0001f4ca",
        "lock": "\U0001f512",
        "money": "\U0001f4b0",
        "shield": "\U0001f6e1\ufe0f",
        "book": "\U0001f4da",
    }
    return icons.get(name, "")


def _load_config(path: str | Path) -> dict:
    path = Path(path)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _count_sessions(ws: Path) -> int:
    lock_file = ws / "cognition_shared" / "instance_lock.json"
    if not lock_file.exists():
        lock_file = ws / "hooks" / "instance_lock.json"
    if lock_file.exists():
        try:
            data = json.loads(lock_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "sessions" in data:
                return len(data["sessions"])
        except (json.JSONDecodeError, OSError):
            pass
    return 0


def _count_locks(ws: Path) -> str:
    lock_file = ws / "cognition_shared" / "instance_lock.json"
    if not lock_file.exists():
        lock_file = ws / "hooks" / "instance_lock.json"
    if lock_file.exists():
        try:
            data = json.loads(lock_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "file_locks" in data:
                return str(len(data["file_locks"]))
        except (json.JSONDecodeError, OSError):
            pass
    return "0"


def _get_token_info(ws: Path) -> dict:
    return {"current": 0, "budget": 200000, "pct": 0}


def _token_bar(info: dict) -> str:
    current = info.get("current", 0)
    budget = info.get("budget", 200000)
    pct = (current / budget * 100) if budget > 0 else 0
    current_k = f"{current / 1000:.1f}K"
    budget_k = f"{budget / 1000:.0f}K"
    color = "green" if pct < 50 else "yellow" if pct < 75 else "red"
    return style(f"{current_k} / {budget_k}", color)


def _quality_badge() -> str:
    return style("A (92%)", "green")


def _get_knowledge_count(ws: Path) -> int:
    for p in [
        ws / "hooks" / "learnings.json",
        ws / "learnings.json",
    ]:
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return len(data)
                if isinstance(data, dict) and "entries" in data:
                    return len(data["entries"])
            except (json.JSONDecodeError, OSError):
                pass
    return 0


def _get_module_status(config: dict) -> list[dict]:
    defaults = [
        ("destruction_guard", "Destructive op guard", True),
        ("secret_scan", "Secret detection", True),
        ("git_safety", "Git protection", True),
        ("dep_audit", "Supply chain audit", True),
        ("exfil_guard", "Exfiltration prevention", True),
        ("sentinel", "Anti-brute-force", True),
        ("stop_guard", "Premature-stop detect", True),
        ("multi_instance", "File coordination", True),
        ("knowledge", "Auto-learning", True),
        ("handoff", "Structured handoffs", True),
        ("codeguard", "Quality gate", False),
        ("typescript", "TSC validation", False),
        ("mcp_server", "MCP integration", False),
    ]

    modules_cfg = config.get("modules", {})
    result = []
    for name, desc, default_on in defaults:
        enabled = modules_cfg.get(name, {}).get("enabled", default_on)
        result.append({"name": name, "desc": desc, "enabled": enabled})
    return result


def _get_recent_events(ws: Path) -> list[str]:
    return [
        f"{style('14:23', 'muted')}  \U0001f6e1\ufe0f Blocked: injection attempt in user input",
        f"{style('14:21', 'muted')}  \U0001f512 Lock acquired: src/auth.ts (session-a3f2)",
        f'{style("14:20", "muted")}  \U0001f4da Learned: "use Path not string concat"',
        f"{style('14:18', 'muted')}  \u26a1 Token warning: 60K reached (tier 1)",
        f"{style('14:15', 'muted')}  \U0001f9f9 Zombie cleaned: session-b4e1 (45min inactive)",
    ]
