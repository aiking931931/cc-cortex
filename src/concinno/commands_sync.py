"""concinno.commands_sync — emit Concinno slash-commands into the Claude
Code ``~/.claude/commands/`` tree so users who type ``/`` in the terminal
see Concinno actions next to the built-in and skill commands.

@module concinno.commands_sync
@responsibility Bridge between the Concinno control-plane (FEATURE_META,
    skills.json, GUI) and the Claude Code slash-command surface. The
    surface itself is plain Markdown files — CC discovers them by
    scanning ``~/.claude/commands/*.md`` + ``./.claude/commands/*.md``
    at session start, so all we need to ship is the files.

Generated commands live under a ``concinno/`` subdirectory to avoid
colliding with user-authored commands and to keep cleanup easy
(``concinno commands clean`` nukes only that subdir).

Rationale (user directive 2026-04-24):
    "Concinno 是運作在 CC 上 因此要跟 CC 同步 例如 / 輸入 Skill 時要
    跳出那些"

@exports sync_commands, list_installed_commands, COMMANDS
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = [
    "sync_commands",
    "list_installed_commands",
    "clean_commands",
    "COMMANDS",
    "ANCHOR",
]

ANCHOR = "<!-- concinno-slash-command: do-not-edit-by-hand -->"

# Each entry produces a ``concinno/<slug>.md`` file. Keep bodies short
# (CC renders them as the command tooltip).
COMMANDS: list[dict[str, Any]] = [
    {
        "slug": "concinno-gui",
        "title": "Open the Concinno config GUI",
        "body": (
            "Open the Concinno config dashboard at http://127.0.0.1:8400.\n"
            "If not already running, start it first: `concinno gui`.\n"
        ),
        "shell": "concinno gui",
    },
    {
        "slug": "concinno-status",
        "title": "Show Concinno runtime status summary",
        "body": (
            "Run `concinno status` in a terminal to see hook / guard /\n"
            "scheduled-task state. For the interactive view, use\n"
            "`/concinno-gui` instead.\n"
        ),
        "shell": "concinno status",
    },
    {
        "slug": "concinno-features",
        "title": "List Concinno features (text mode)",
        "body": (
            "List every FEATURE_META entry + its current enabled state.\n"
            "For the filterable / toggleable view open `/concinno-gui`.\n"
        ),
        "shell": "concinno session-switches",
    },
    {
        "slug": "concinno-feature-toggle",
        "title": "Toggle a Concinno feature on/off",
        "body": (
            "Flip a feature on or off via the command palette.\n\n"
            "Usage: `concinno preset set <feature>.enabled <true|false>`\n\n"
            "Example: `concinno preset set release_auth.disabled true`\n\n"
            "Interactive alternative: `/concinno-gui`.\n"
        ),
        "shell": None,
    },
    {
        "slug": "concinno-skills",
        "title": "List all Concinno-tracked skills",
        "body": (
            "Skills are Markdown SOPs + MCP bridges under\n"
            "`~/.claude/skills/` and `./.claude/skills/`. Concinno adds\n"
            "a per-skill enable flag in `~/.concinno/skills.json`\n"
            "(advisory in 2.24.x; enforcement hook lands in 2.25.x).\n\n"
            "Use `/concinno-gui` to toggle skills visually.\n"
        ),
        "shell": None,
    },
    {
        "slug": "concinno-handoff-mode",
        "title": "Switch handoff mode (full / phase / save-token)",
        "body": (
            "Cycle the handoff mode. `full` also auto-launches the GUI\n"
            "(see `concinno.full_mode_services`).\n\n"
            "Usage in a terminal:\n"
            "```bash\n"
            "python -c \"from concinno.handoff_engine import "
            "set_handoff_mode; set_handoff_mode('full')\"\n"
            "```\n\n"
            "Valid modes: full, phase, save-token, competition.\n"
        ),
        "shell": None,
    },
]


def _default_dest() -> Path:
    return Path.home() / ".claude" / "commands" / "concinno"


def _render_command(entry: dict[str, Any]) -> str:
    """Produce the Markdown body for one slash-command file.

    The anchor comment lets us detect and safely clean up only files
    we ourselves generated; a user-authored command with the same slug
    but no anchor is left untouched.
    """
    lines = [
        "---",
        f"description: {entry['title']}",
        "---",
        "",
        ANCHOR,
        "",
        f"# {entry['title']}",
        "",
        entry["body"].rstrip(),
    ]
    if entry.get("shell"):
        lines += [
            "",
            "Quick run:",
            "",
            "```bash",
            entry["shell"],
            "```",
        ]
    return "\n".join(lines) + "\n"


def sync_commands(dest: str | Path | None = None) -> dict[str, Any]:
    """Write every ``COMMANDS`` entry into ``dest`` and return a report.

    Idempotent — files with identical content are not rewritten.
    Files present in ``dest`` that are not in ``COMMANDS`` but carry
    the Concinno anchor are removed so a trimmed ``COMMANDS`` list
    keeps the destination tree in sync without orphans.
    """
    target = Path(dest) if dest else _default_dest()
    target.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    unchanged: list[str] = []
    for entry in COMMANDS:
        body = _render_command(entry)
        p = target / f"{entry['slug']}.md"
        if p.is_file() and p.read_text(encoding="utf-8") == body:
            unchanged.append(entry["slug"])
            continue
        p.write_text(body, encoding="utf-8")
        written.append(entry["slug"])
    # Clean up orphans (our anchor only)
    expected = {f"{e['slug']}.md" for e in COMMANDS}
    removed: list[str] = []
    for p in target.glob("*.md"):
        if p.name in expected:
            continue
        try:
            content = p.read_text(encoding="utf-8")
        except Exception:
            continue
        if ANCHOR in content:
            p.unlink()
            removed.append(p.name)
    return {
        "dest": str(target),
        "written": written,
        "unchanged": unchanged,
        "removed_orphans": removed,
    }


def _parse_command_meta(path: Path) -> tuple[str, str]:
    """Return ``(text, description)`` for a single command file."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return "", ""
    desc = ""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end > 0:
            for line in text[3:end].splitlines():
                if line.startswith("description:"):
                    desc = line.split(":", 1)[1].strip().strip('"').strip("'")
                    break
    return text, desc


def list_installed_commands(*, also_scan_roots: bool = True) -> list[dict[str, Any]]:
    """Return every discoverable Claude Code slash command, flagging
    which ones Concinno emitted (``managed=True``)."""
    roots = [
        Path.home() / ".claude" / "commands",
        Path.cwd() / ".claude" / "commands",
    ] if also_scan_roots else [_default_dest()]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*.md")):
            rel = p.relative_to(root)
            slug = rel.with_suffix("").as_posix()
            if slug in seen:
                continue
            seen.add(slug)
            text, desc = _parse_command_meta(p)
            out.append({
                "slug": slug,
                "path": str(p),
                "root": str(root),
                "managed": ANCHOR in text,
                "description": desc,
            })
    return out


def clean_commands(dest: str | Path | None = None) -> dict[str, Any]:
    """Remove every managed (anchored) file under ``dest``."""
    target = Path(dest) if dest else _default_dest()
    removed: list[str] = []
    if not target.is_dir():
        return {"dest": str(target), "removed": removed}
    for p in target.glob("*.md"):
        try:
            if ANCHOR in p.read_text(encoding="utf-8"):
                p.unlink()
                removed.append(p.name)
        except Exception:
            pass
    return {"dest": str(target), "removed": removed}


def main(argv: list[str] | None = None) -> int:
    """CLI for ``concinno commands {sync,list,clean}``."""
    import sys

    args = argv if argv is not None else sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print("Usage: concinno commands {sync|list|clean} [path]")
        return 0
    cmd = args[0]
    path = args[1] if len(args) > 1 else None
    if cmd == "sync":
        rep = sync_commands(path)
        print(json.dumps(rep, indent=2))
        return 0
    if cmd == "list":
        rows = list_installed_commands()
        for r in rows:
            tag = "[concinno]" if r["managed"] else "          "
            print(f"{tag} {r['slug']:40}  {r['description']}")
        return 0
    if cmd == "clean":
        rep = clean_commands(path)
        print(json.dumps(rep, indent=2))
        return 0
    print(f"unknown subcommand: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
