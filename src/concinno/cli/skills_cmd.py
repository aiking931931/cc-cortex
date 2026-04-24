"""``concinno skills {new, list, enable, disable, delete}`` CLI.

Added in 2.30.1 per the 2.30.0 carry-over plan. Closes the UX gap
"用戶加新 Skill要方便到極致" — 30-sec end-to-end from typing the
command to seeing the card appear in the GUI.

Subcommands:
  new <name>       — interactive or flag-driven SKILL.md scaffolder
  list             — enumerate discovered skills across all scopes
  enable <name>    — mark skill enabled in ``~/.concinno/skills.json``
  disable <name>   — mark skill disabled in ``~/.concinno/skills.json``
  delete <name>    — remove the skill directory (prompts unless --force)

The scaffolder produces a ready-to-edit ``SKILL.md`` with correct
frontmatter so ``concinno.gui`` picks it up on the next 3-second
polling tick (Phase A from 2.30.0).
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

__all__ = ["register"]


_SCOPE_DIRS = {
    "user": ("user",),
    "public": ("public",),
    "private": ("private",),
    "project": None,  # project scope → ./.claude/skills/<name>/
    "official": ("official",),  # gated behind CONCINNO_DEV
}


_MINIMAL_TEMPLATE = """---
name: {name}
description: {description}
triggers: [{triggers_inline}]
user-invocable: {user_invocable}
---

# {name}

I {verb} when {condition}.

## Why this skill exists

{description_long}

## When to use

- (trigger scenario 1)
- (trigger scenario 2)

## When NOT to use

- (out-of-scope case)

## Core content

(put the actual SOP / knowledge / checklist here)
"""


_STANDARD_TEMPLATE = """---
name: {name}
description: {description}
triggers: [{triggers_inline}]
user-invocable: {user_invocable}
---

# {name}

I {verb} when {condition}.

## Why this skill exists

{description_long}

## When to use

- (most common trigger)
- (secondary trigger)
- (edge-case trigger)

## When NOT to use

- (out-of-scope case 1)
- (out-of-scope case 2)

## Core SOP

1. Step one.
2. Step two.
3. Step three.

## Worked examples

### Example A

(concrete scenario with input → expected action)

### Example B

(alternative scenario showing nuance)

## Related

- Related rule / skill / doc pointer
"""


_KB_TEMPLATE = """---
name: {name}
description: {description}
triggers: [{triggers_inline}]
user-invocable: {user_invocable}
---

# {name} — knowledge base

I surface {domain} knowledge when {condition}.

## Why this KB exists

{description_long}

## Topic index

| Topic | Summary | Link |
|---|---|---|
| A | ... | `A.md` |
| B | ... | `B.md` |

## Quick-reference

(compact lookup table or decision tree for the most-asked cases)

## Deeper content

Link out to separate per-topic files (`A.md`, `B.md`) kept beside
this `SKILL.md` so each topic can grow without bloating the entry
the agent loads every session.
"""


_TEMPLATES = {
    "minimal": _MINIMAL_TEMPLATE,
    "standard": _STANDARD_TEMPLATE,
    "kb": _KB_TEMPLATE,
}


@dataclass
class _ScaffoldInputs:
    name: str
    description: str
    triggers: list[str]
    user_invocable: bool
    scope: str
    body_template: str


def _skills_state_path() -> Path:
    return Path.home() / ".concinno" / "skills.json"


def _read_skills_state() -> dict[str, dict]:
    p = _skills_state_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_skills_state(state: dict[str, dict]) -> None:
    p = _skills_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(p)


def _resolve_scope_dir(scope: str, name: str) -> Path:
    if scope == "project":
        return Path.cwd() / ".claude" / "skills" / name
    segs = _SCOPE_DIRS[scope]
    return Path.home() / ".claude" / "skills" / segs[0] / name


def _skills_roots() -> list[Path]:
    roots = [Path.home() / ".claude" / "skills"]
    cwd_skills = Path.cwd() / ".claude" / "skills"
    if cwd_skills.is_dir():
        roots.append(cwd_skills)
    return roots


def _scope_of(skill_dir: Path) -> str:
    """Best-effort scope inference from directory path."""
    parts = skill_dir.parts
    for scope in ("official", "public", "private", "user"):
        if scope in parts:
            return scope
    if ".claude" in parts and parts.index(".claude") == len(parts) - 3:
        # cwd/.claude/skills/<name>
        if skill_dir.parent.parent.parent != Path.home():
            return "project"
    return "user"


def _prompt(msg: str, default: str = "") -> str:
    if default:
        shown = f"{msg} [{default}]: "
    else:
        shown = f"{msg}: "
    try:
        reply = input(shown).strip()
    except EOFError:
        return default
    return reply or default


def _prompt_bool(msg: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    while True:
        reply = _prompt(f"{msg} [{d}]").lower()
        if not reply:
            return default
        if reply in ("y", "yes", "true"):
            return True
        if reply in ("n", "no", "false"):
            return False
        print("  please answer y or n")


def _interactive_inputs(name: str, args: argparse.Namespace) -> _ScaffoldInputs:
    description = args.description or _prompt(
        "One-line description (shows in GUI + /help)"
    )
    while not description:
        print("  description is required")
        description = _prompt("One-line description")

    if args.triggers:
        triggers = [t.strip() for t in args.triggers.split(",") if t.strip()]
    else:
        raw = _prompt(
            "Triggers (comma-separated keywords, optional)", default=""
        )
        triggers = [t.strip() for t in raw.split(",") if t.strip()]

    if args.user_invocable is not None:
        user_invocable = args.user_invocable
    else:
        user_invocable = _prompt_bool(
            "User-invocable via /<name>?", default=True
        )

    scope = args.scope or _prompt(
        "Scope (user/project/private)", default="user"
    )
    if scope not in _SCOPE_DIRS:
        raise SystemExit(f"unknown scope: {scope}")
    if scope == "official":
        import os as _os
        if not _os.environ.get("CONCINNO_DEV"):
            raise SystemExit(
                "scope='official' requires $CONCINNO_DEV=1 "
                "(end users should not write to the shipped tree)"
            )

    body_template = args.body_template or _prompt(
        "Body template (minimal/standard/kb)", default="minimal"
    )
    if body_template not in _TEMPLATES:
        raise SystemExit(f"unknown body-template: {body_template}")

    return _ScaffoldInputs(
        name=name,
        description=description,
        triggers=triggers,
        user_invocable=user_invocable,
        scope=scope,
        body_template=body_template,
    )


def _render_template(inputs: _ScaffoldInputs) -> str:
    tpl = _TEMPLATES[inputs.body_template]
    triggers_inline = ", ".join(inputs.triggers)
    # First-person verb hint — user edits, but give a reasonable prior
    verb = "act"
    condition = "triggered"
    description_long = (
        inputs.description
        if len(inputs.description) > 40
        else inputs.description + "\n\n(expand this section with the full rationale)"
    )
    return tpl.format(
        name=inputs.name,
        description=inputs.description,
        triggers_inline=triggers_inline,
        user_invocable=str(inputs.user_invocable).lower(),
        verb=verb,
        condition=condition,
        description_long=description_long,
        domain=inputs.name.replace("_", " "),
    )


def _cmd_new(args: argparse.Namespace) -> int:
    name = args.name
    if not name.replace("_", "").replace("-", "").isalnum():
        print(
            f"error: name must be alphanumeric / underscore / hyphen, got {name!r}",
            file=sys.stderr,
        )
        return 2

    if args.no_interactive:
        missing = []
        if not args.description:
            missing.append("--description")
        if missing:
            print(
                f"error: --no-interactive requires: {', '.join(missing)}",
                file=sys.stderr,
            )
            return 2
        inputs = _ScaffoldInputs(
            name=name,
            description=args.description,
            triggers=[t.strip() for t in (args.triggers or "").split(",") if t.strip()],
            user_invocable=(
                True if args.user_invocable is None else args.user_invocable
            ),
            scope=args.scope or "user",
            body_template=args.body_template or "minimal",
        )
    else:
        inputs = _interactive_inputs(name, args)

    target_dir = _resolve_scope_dir(inputs.scope, inputs.name)
    skill_md = target_dir / "SKILL.md"

    if skill_md.is_file() and not args.force:
        print(
            f"error: {skill_md} already exists; pass --force to overwrite",
            file=sys.stderr,
        )
        return 3

    body = _render_template(inputs)

    if args.dry_run:
        print(f"[dry-run] would write {skill_md}:")
        print("---")
        print(body)
        return 0

    target_dir.mkdir(parents=True, exist_ok=True)
    skill_md.write_text(body, encoding="utf-8")

    # Enable by default
    state = _read_skills_state()
    state[inputs.name] = {"enabled": True}
    _write_skills_state(state)

    print(f"Wrote {skill_md}")
    print(
        "The GUI (if running) refreshes within 3 seconds; the new skill "
        "appears in the Skills tab.",
    )
    print(
        f"Next: edit {skill_md} to fill in the body, then invoke with "
        f"/{inputs.name} if user-invocable=true.",
    )
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    state = _read_skills_state()
    rows = []
    for root in _skills_roots():
        if not root.is_dir():
            continue
        for sk_md in sorted(root.rglob("SKILL.md")):
            if any(seg in {".git", "node_modules", "__pycache__"} for seg in sk_md.parts):
                continue
            name = sk_md.parent.name
            scope = _scope_of(sk_md.parent)
            enabled = state.get(name, {}).get("enabled", True)
            rows.append((name, scope, enabled, sk_md))

    if not rows:
        print("(no skills discovered)")
        return 0

    print(f"{'name':<30} {'scope':<10} {'enabled':<8} path")
    for name, scope, enabled, path in rows:
        mark = "yes" if enabled else "no"
        print(f"{name:<30} {scope:<10} {mark:<8} {path}")
    return 0


def _cmd_toggle(name: str, enabled: bool) -> int:
    state = _read_skills_state()
    state.setdefault(name, {})["enabled"] = enabled
    _write_skills_state(state)
    word = "enabled" if enabled else "disabled"
    print(f"Skill {name!r} {word} in {_skills_state_path()}")
    return 0


def _cmd_delete(args: argparse.Namespace) -> int:
    name = args.name
    candidates = []
    for root in _skills_roots():
        if not root.is_dir():
            continue
        for sk_md in root.rglob("SKILL.md"):
            if sk_md.parent.name == name:
                candidates.append(sk_md.parent)
    if not candidates:
        print(f"no skill named {name!r} found", file=sys.stderr)
        return 4
    if len(candidates) > 1 and not args.force:
        print(
            f"found {len(candidates)} skill dirs named {name!r}:",
            file=sys.stderr,
        )
        for c in candidates:
            print(f"  {c}", file=sys.stderr)
        print("pass --force to delete all of them", file=sys.stderr)
        return 5
    for c in candidates:
        shutil.rmtree(c)
        print(f"removed {c}")

    state = _read_skills_state()
    if name in state:
        del state[name]
        _write_skills_state(state)
    return 0


def _dispatch(args: argparse.Namespace) -> None:
    action = args.skills_action
    if action == "new":
        sys.exit(_cmd_new(args))
    if action == "list":
        sys.exit(_cmd_list(args))
    if action == "enable":
        sys.exit(_cmd_toggle(args.name, True))
    if action == "disable":
        sys.exit(_cmd_toggle(args.name, False))
    if action == "delete":
        sys.exit(_cmd_delete(args))
    print("usage: concinno skills {new|list|enable|disable|delete}", file=sys.stderr)
    sys.exit(2)


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``concinno skills ...`` namespace on the top-level
    argparse ``subparsers``."""
    p = subparsers.add_parser(
        "skills",
        help="Discover / scaffold / toggle / delete skills in "
        "~/.claude/skills/",
    )
    sub = p.add_subparsers(dest="skills_action")

    p_new = sub.add_parser("new", help="Scaffold a new SKILL.md file")
    p_new.add_argument("name", help="Skill name (snake_case)")
    p_new.add_argument(
        "--description", default=None,
        help="One-line description (skips interactive prompt if set)",
    )
    p_new.add_argument(
        "--triggers", default=None,
        help="Comma-separated trigger keywords",
    )
    p_new.add_argument(
        "--user-invocable", dest="user_invocable",
        type=lambda v: v.lower() in ("1", "true", "yes", "y"),
        default=None,
        help="Whether users invoke via /<name>  (true|false)",
    )
    p_new.add_argument(
        "--scope",
        choices=sorted(_SCOPE_DIRS.keys()),
        default=None,
        help="Which skill tree to write into (default: user)",
    )
    p_new.add_argument(
        "--body-template", dest="body_template",
        choices=list(_TEMPLATES.keys()), default=None,
        help="Body shape template (default: minimal)",
    )
    p_new.add_argument(
        "--no-interactive", dest="no_interactive",
        action="store_true",
        help="Fail instead of prompting when flags are missing "
        "(for agent automation)",
    )
    p_new.add_argument(
        "--force", action="store_true",
        help="Overwrite an existing SKILL.md at the target path",
    )
    p_new.add_argument(
        "--dry-run", dest="dry_run", action="store_true",
        help="Show the file that would be written without writing",
    )
    p_new.set_defaults(func=_dispatch)

    p_list = sub.add_parser("list", help="List discovered skills")
    p_list.set_defaults(func=_dispatch)

    p_en = sub.add_parser("enable", help="Enable a skill")
    p_en.add_argument("name")
    p_en.set_defaults(func=_dispatch)

    p_dis = sub.add_parser("disable", help="Disable a skill")
    p_dis.add_argument("name")
    p_dis.set_defaults(func=_dispatch)

    p_del = sub.add_parser(
        "delete",
        help="Remove a skill directory (cannot be undone)",
    )
    p_del.add_argument("name")
    p_del.add_argument(
        "--force", action="store_true",
        help="Remove all matching directories when >1 collide by name",
    )
    p_del.set_defaults(func=_dispatch)
