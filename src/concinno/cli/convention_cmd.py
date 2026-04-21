"""concinno convention CLI — check / suggest / init / presets.

Registered via ``register(sub_parsers)`` the same way ``config_cmd`` is
wired into ``cli.main``. Loads :mod:`concinno.convention_engine` lazily
so the CLI startup stays fast.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _engine(workspace: str = ""):
    from concinno.convention_engine import ConventionEngine

    return ConventionEngine(workspace=workspace)


def _walk_files(root: str, limit: int = 2000) -> list[str]:
    """Return relative file paths under *root*, capped at *limit*.

    Skips ``.git`` / ``.concinno_cache`` / ``.venv`` / ``node_modules`` /
    ``__pycache__`` / ``dist`` to keep the scan focused on source files.
    """
    skip = {".git", ".concinno_cache", ".venv", "venv", "node_modules", "__pycache__", "dist"}
    out: list[str] = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in skip]
        for name in files:
            if name.startswith("."):
                continue
            full = os.path.join(base, name)
            rel = os.path.relpath(full, root).replace("\\", "/")
            out.append(rel)
            if len(out) >= limit:
                return out
    return out


def cmd_check(args: argparse.Namespace) -> None:
    """Scan *path* for files that violate naming or placement conventions."""
    root = args.path or os.environ.get("CLAUDE_PROJECT_DIR", "") or os.getcwd()
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        print(f"❌ Not a directory: {root}", file=sys.stderr)
        sys.exit(1)

    engine = _engine(workspace=root)
    project = args.project or os.path.basename(root)

    violations: list[tuple[str, str, str]] = []
    scanned = _walk_files(root, limit=args.limit)

    for rel in scanned:
        filename = os.path.basename(rel)
        naming = engine.check_naming(filename)
        if not naming.passed:
            violations.append((rel, "naming", naming.suggestion))
            continue
        placement = engine.check_placement(rel, project=project)
        if not placement.passed:
            violations.append((rel, "placement", placement.suggestion))

    print(f"Scanned {len(scanned)} file(s) under {root}")
    print(f"Project: {project}")
    print()
    if not violations:
        print("✅ No convention violations.")
        return

    print(f"⚠ {len(violations)} convention violation(s):")
    print()
    for rel, kind, suggestion in violations[:50]:
        print(f"  [{kind}] {rel}")
        print(f"    → {suggestion}")
    if len(violations) > 50:
        extra = len(violations) - 50
        print(f"  ... and {extra} more (re-run with --limit=bigger for the full list)")

    if args.strict:
        sys.exit(1)


def cmd_suggest(args: argparse.Namespace) -> None:
    """Suggest the correct directory for *filename*."""
    engine = _engine()
    project = args.project or "default"
    path = engine.suggest_placement(args.filename, project=project)
    if path == args.filename:
        print(f"No rule matched for {args.filename!r} — no suggestion.")
        return
    print(path)


def cmd_init(args: argparse.Namespace) -> None:
    """Seed a workspace with a convention config."""
    root = args.workspace or os.environ.get("CLAUDE_PROJECT_DIR", "") or os.getcwd()
    root = Path(root).resolve()
    target_dir = root / ".concinno"
    target = target_dir / "conventions.json"
    if target.exists() and not args.force:
        print(f"⚠ Already exists: {target} (use --force to overwrite)", file=sys.stderr)
        sys.exit(1)

    from concinno.convention_presets import preset_path

    source = preset_path(args.preset)
    if not source:
        from concinno.convention_presets import list_presets

        print(
            f"❌ Unknown preset {args.preset!r}. "
            f"Available: {', '.join(list_presets())}",
            file=sys.stderr,
        )
        sys.exit(1)

    target_dir.mkdir(parents=True, exist_ok=True)
    with open(source, encoding="utf-8") as f:
        data = json.load(f)
    with open(target, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"✅ Wrote {target}")
    print(f"   Preset: {args.preset}")


def cmd_presets(_args: argparse.Namespace) -> None:
    """List available convention presets."""
    from concinno.convention_presets import list_presets, preset_path

    names = list_presets()
    if not names:
        print("No presets shipped. (This is a bug — please report.)")
        return

    print(f"Available convention presets ({len(names)}):")
    print()
    for name in names:
        path = preset_path(name)
        desc = ""
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            desc = (data.get("_meta") or {}).get("description", "")
        except (OSError, ValueError):
            pass
        if desc:
            print(f"  {name:10s}  {desc}")
        else:
            print(f"  {name}")


def register(sub: argparse._SubParsersAction) -> None:
    """Attach the ``convention`` subcommand group to the main CLI parser."""
    p = sub.add_parser("convention", help="Workspace naming / placement conventions")
    conv_sub = p.add_subparsers(dest="convention_command")

    p_check = conv_sub.add_parser(
        "check", help="Scan a directory for convention violations",
    )
    p_check.add_argument(
        "path", nargs="?", default="",
        help="Directory to scan (default: CWD / CLAUDE_PROJECT_DIR)",
    )
    p_check.add_argument(
        "--project", default="",
        help="Project name for path interpolation (default: dir name)",
    )
    p_check.add_argument(
        "--limit", type=int, default=2000, help="Max files to scan (default: 2000)",
    )
    p_check.add_argument(
        "--strict", action="store_true",
        help="Exit non-zero when violations found (for CI)",
    )
    p_check.set_defaults(func=cmd_check)

    p_suggest = conv_sub.add_parser(
        "suggest", help="Suggest the correct directory for a filename",
    )
    p_suggest.add_argument("filename", help="Filename to place")
    p_suggest.add_argument("--project", default="", help="Project name for path interpolation")
    p_suggest.set_defaults(func=cmd_suggest)

    p_init = conv_sub.add_parser(
        "init", help="Seed <workspace>/.concinno/conventions.json from a preset",
    )
    p_init.add_argument("--workspace", default="", help="Workspace path (default: CWD)")
    p_init.add_argument("--preset", default="minimal", help="Preset name (default: minimal)")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing conventions.json")
    p_init.set_defaults(func=cmd_init)

    p_presets = conv_sub.add_parser(
        "presets", help="List available convention presets",
    )
    p_presets.set_defaults(func=cmd_presets)

    def _default(_args: argparse.Namespace) -> None:
        p.print_help()

    p.set_defaults(func=_default)
