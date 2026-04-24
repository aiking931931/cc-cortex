"""concinno CLI — init, enable, disable, status, doctor, uninstall."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys
from pathlib import Path

MODULES = {
    "core": {
        "description": "Token guardian + session notifications",
        "default": True,
        "required": True,
    },
    "knowledge": {"description": "Auto-learning loop + knowledge base", "default": True},
    "multi_instance": {"description": "File locking + zombie GC", "default": True},
    "sentinel": {"description": "Anti-brute-force detection", "default": True},
    "evolution": {
        "description": "Three-reflections + tidy balance + token guard",
        "default": False,
    },
    "typescript": {"description": "Instant tsc validation on save", "default": False},
    "codeguard": {
        "description": "Multi-language code quality guard (ruff/cargo/govet)",
        "default": False,
    },
    "secret_scan": {
        "description": "Hardcoded secret detection (API keys/tokens/passwords)",
        "default": True,
    },
    "git_safety": {
        "description": "Dangerous git operation detection (force push/reset --hard)",
        "default": True,
    },
    "dep_audit": {
        "description": "Dependency typosquatting + scope spoofing + blocklist (pip/npm/uv)",
        "default": True,
    },
    "publish_scan": {
        "description": "Pre-publish artifact scanner (secrets/keys/personal paths in dist)",
        "default": True,
    },
    "exfil_guard": {
        "description": "Data exfiltration prevention (sensitive file uploads)",
        "default": True,
    },
    "identity_guard": {
        "description": "Block Agent from modifying identity configs (CLAUDE.md/rules/settings)",
        "default": True,
    },
    "destruction_guard": {
        "description": "Risk-based destructive op interception + auto-backup (R0-R4)",
        "default": True,
    },
    "stop_guard": {
        "description": "Premature session stop detection + warning",
        "default": True,
    },
    "prompt_guard": {
        "description": "Clarity gate + multi-question detection for UserPromptSubmit",
        "default": True,
    },
    "autopilot": {
        "description": "Automated maintenance scheduler (reflect/scavenger/research)",
        "default": False,
    },
    "cognitive": {
        "description": "Cross-session learning, adaptive thresholds, decision tracking",
        "default": True,
    },
    "delivery": {
        "description": "Enterprise delivery gate — verified results, not guesses",
        "default": True,
    },
    "proposal_guard": {
        "description": "Block proposals without side-effect analysis (Poka-Yoke)",
        "default": True,
    },
    "ui_verify": {
        "description": "Lock after deploy+UI changes until screenshot verification",
        "default": True,
    },
    "structural_guard": {
        "description": "Structural analysis — func length / nesting / TODO debt / file size",
        "default": True,
    },
    "design_theory": {
        "description": "Design theory enforcement — Vertical Slice + Deep Module + HITL/AFK",
        "default": True,
    },
    "pipeline": {
        "description": "Think→Review→QA→Ship pipeline state management",
        "default": True,
    },
}

HOOKS_DIR = Path.home() / ".claude" / "hooks"
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

# (source filename in src/concinno/hooks/, installed filename in ~/.claude/hooks/)
HOOK_FILES = [
    ("on_session_start.py", "on-session-start.py"),
    ("on_stop.py", "on-stop.py"),
    ("on_pre_tool.py", "on-pre-tool.py"),
    ("on_post_tool.py", "on-post-tool.py"),
    ("ask_user_toast.py", "ask-user-toast.py"),
]

# (installed filename, Claude Code hook event, matcher) — settings.json.
# ``matcher`` defaults to "" (all tools). AskUserQuestion has a
# dedicated matcher so we don't toast on every tool call.
HOOK_EVENTS = [
    ("on-session-start.py", "SessionStart", ""),
    ("on-stop.py", "Stop", ""),
    ("on-pre-tool.py", "PreToolUse", ""),
    ("on-post-tool.py", "PostToolUse", ""),
    ("ask-user-toast.py", "PreToolUse", "AskUserQuestion"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hook_command(hook_path: Path) -> str:
    """Build the shell command to invoke a hook script."""
    if platform.system() == "Windows":
        return f'pythonw "{hook_path}"'
    return f'python3 "{hook_path}"'


def _get_cfg():
    """Get the singleton Config, initialised with the default hooks dir."""
    from concinno.core.config import get_config

    return get_config(hooks_dir=str(HOOKS_DIR))


def _cfg_path() -> Path:
    """Resolved path to cc_config.json."""
    cp = _get_cfg().config_file_path
    return Path(cp) if cp else HOOKS_DIR / "cc_config.json"


def _load_module_states() -> dict:
    """Load enabled/disabled states via Config singleton."""
    return _get_cfg().raw("modules", {})


def _save_module_states(states: dict) -> None:
    """Save module states via Config.update_file (merge with existing)."""
    _get_cfg().update_file("modules", states)


def _load_settings() -> dict:
    """Load ~/.claude/settings.json (returns empty dict if missing/invalid)."""
    if SETTINGS_PATH.exists():
        try:
            return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_settings(settings: dict) -> None:
    """Write ~/.claude/settings.json, preserving existing structure."""
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")


def _register_hooks_in_settings() -> int:
    """Register concinno hooks in settings.json. Returns count registered."""
    settings = _load_settings()
    hooks = settings.setdefault("hooks", {})
    registered = 0

    for entry in HOOK_EVENTS:
        # Accept both legacy 2-tuples (filename, event) and the
        # 2.7+ 3-tuples (filename, event, matcher) so downstream
        # forks that import this list keep working.
        if len(entry) == 2:
            hook_file, event = entry
            matcher = ""
        else:
            hook_file, event, matcher = entry
        hook_path = HOOKS_DIR / hook_file
        if not hook_path.exists():
            continue

        command = _hook_command(hook_path)
        event_groups = hooks.setdefault(event, [])

        # Skip if already registered (look for hook filename in existing commands)
        already = any(
            hook_file in h.get("command", "")
            for group in event_groups
            for h in group.get("hooks", [])
        )
        if already:
            continue

        event_groups.append(
            {
                "matcher": matcher,
                "hooks": [{"type": "command", "command": command}],
            }
        )
        registered += 1

    _save_settings(settings)
    return registered


def _unregister_hooks_from_settings() -> int:
    """Remove concinno hooks from settings.json. Returns count removed."""
    if not SETTINGS_PATH.exists():
        return 0

    settings = _load_settings()
    hooks = settings.get("hooks", {})
    if not hooks:
        return 0

    hook_filenames = {entry[0] for entry in HOOK_EVENTS}
    removed = 0

    for event in list(hooks.keys()):
        new_groups = []
        for group in hooks[event]:
            new_hooks = []
            for h in group.get("hooks", []):
                cmd = h.get("command", "")
                if any(hf in cmd for hf in hook_filenames):
                    removed += 1
                else:
                    new_hooks.append(h)
            if new_hooks:
                group["hooks"] = new_hooks
                new_groups.append(group)
        if new_groups:
            hooks[event] = new_groups
        else:
            del hooks[event]

    settings["hooks"] = hooks
    _save_settings(settings)
    return removed


def _install_rules(workspace: Path, *, force: bool = False) -> None:
    """Install cognitive rules (L0 + L1) into .claude/rules/."""
    src_rules = Path(__file__).parent.parent / "templates" / "rules"
    if not src_rules.exists():
        return
    dst_base = workspace / ".claude" / "rules"
    dst_base.mkdir(parents=True, exist_ok=True)
    (dst_base / "L1").mkdir(parents=True, exist_ok=True)

    count = 0
    for src_file in sorted(src_rules.rglob("*.md")):
        rel = src_file.relative_to(src_rules)
        dst = dst_base / rel
        if dst.exists() and not force:
            print(f"  ⚠ rules/{rel} exists (use --force to overwrite)")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dst)
        print(f"  ✅ rules/{rel}")
        count += 1
    if count:
        print(f"  ✅ {count} rule file(s) installed")
    else:
        print("  ⚠ rules already installed (use --force to overwrite)")


def _install_claude_reference(workspace: Path) -> None:
    """Install CLAUDE_REFERENCE.md to workspace (never overwrite CLAUDE.md)."""
    ref_src = Path(__file__).parent.parent / "templates" / "CLAUDE_REFERENCE.md"
    if not ref_src.exists():
        return
    ref_dst = workspace / "CLAUDE_REFERENCE.md"
    claude_md = workspace / "CLAUDE.md"
    if ref_dst.exists():
        print("  ⚠ CLAUDE_REFERENCE.md exists (kept as-is)")
    elif claude_md.exists():
        shutil.copy2(ref_src, ref_dst)
        print("  ✅ CLAUDE_REFERENCE.md (reference — your CLAUDE.md preserved)")
    else:
        shutil.copy2(ref_src, ref_dst)
        print("  ✅ CLAUDE_REFERENCE.md (rename to CLAUDE.md to activate)")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> None:
    """Initialize concinno in the current project."""
    print("⚡ concinno init")
    print()

    # Ensure hooks directory
    HOOKS_DIR.mkdir(parents=True, exist_ok=True)

    # Determine workspace
    workspace = Path.cwd()
    print(f"  Workspace: {workspace}")

    # Copy hook files from hooks/ template directory
    src_hooks = Path(__file__).parent.parent / "hooks"

    installed = 0
    for src_rel, dst_name in HOOK_FILES:
        src = src_hooks / src_rel
        dst = HOOKS_DIR / dst_name
        if src.exists():
            if dst.exists() and not args.force:
                print(f"  ⚠ {dst_name} exists (use --force to overwrite)")
                continue
            shutil.copy2(src, dst)
            print(f"  ✅ {dst_name}")
            installed += 1
        else:
            print(f"  ⏭ {dst_name} (source not found, skipping)")

    # Generate config
    cfg_path = _cfg_path()
    if not cfg_path.exists() or args.force:
        config = {
            "workspace": str(workspace),
            "thresholds": {
                "zombie_seconds": 1800,
                "sentinel_repeat": 3,
                "sentinel_paralysis": 7,
                "cleanup_md_lines": 8,
                "cleanup_code_lines": 40,
                "tsc_timeout": 15,
            },
            "notification": {
                "sound_file": "notify.mp3",
                "toast_title": "Claude Code",
            },
        }
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
        print("  ✅ cc_config.json")
    else:
        print("  ⚠ cc_config.json exists (use --force to overwrite)")

    # Register hooks in settings.json
    reg_count = _register_hooks_in_settings()
    if reg_count:
        print(f"  ✅ settings.json — {reg_count} hook(s) registered")
    else:
        print("  ⚠ settings.json — hooks already registered (or no hook files)")

    # Install CLAUDE.md reference (never overwrite — additive only)
    _install_claude_reference(workspace)

    # Install cognitive rules (L0 + L1)
    _install_rules(workspace, force=args.force)

    print()
    print(f"  Installed {installed} hook(s).")
    print("  Run `concinno doctor` to verify installation.")
    print()


def cmd_enable(args: argparse.Namespace) -> None:
    """Enable a module."""
    module = args.module
    if module not in MODULES:
        print(f"Unknown module: {module}")
        print(f"Available: {', '.join(MODULES.keys())}")
        sys.exit(1)
    states = _load_module_states()
    states[module] = True
    _save_module_states(states)
    print(f"✅ Enabled: {module} — {MODULES[module]['description']}")


def cmd_disable(args: argparse.Namespace) -> None:
    """Disable a module."""
    module = args.module
    if module not in MODULES:
        print(f"Unknown module: {module}")
        sys.exit(1)
    if MODULES[module].get("required"):
        print(f"❌ Cannot disable required module: {module}")
        sys.exit(1)
    states = _load_module_states()
    states[module] = False
    _save_module_states(states)
    print(f"⬜ Disabled: {module}")


def cmd_status(_args: argparse.Namespace) -> None:
    """Show module status (reads actual states from config)."""
    print("concinno modules:")
    print()
    states = _load_module_states()
    for name, info in MODULES.items():
        if info.get("required"):
            icon = "🔒"
            label = "always on"
        elif name in states:
            icon = "✅" if states[name] else "⬜"
            label = "enabled" if states[name] else "disabled"
        else:
            icon = "✅" if info.get("default") else "⬜"
            label = "default"
        print(f"  {icon} {name:20s} {info['description']:40s} ({label})")
    print()
    cfg_status = _cfg_path()
    if cfg_status.exists():
        print(f"  Config: {cfg_status}")
    else:
        print("  Config: not found (run `concinno init`)")


def cmd_doctor(_args: argparse.Namespace) -> None:
    """Health check for concinno installation."""
    print("🩺 concinno doctor")
    print()
    issues = 0

    # 1. Config file
    cfg_file = _cfg_path()
    if cfg_file.exists():
        try:
            json.loads(cfg_file.read_text(encoding="utf-8"))
            print("  ✅ cc_config.json — valid")
        except Exception as exc:
            print(f"  ❌ cc_config.json — invalid JSON: {exc}")
            issues += 1
    else:
        print("  ❌ cc_config.json — not found")
        issues += 1

    # 2. Hook files
    for _, dst_name in HOOK_FILES:
        path = HOOKS_DIR / dst_name
        if path.exists():
            print(f"  ✅ {dst_name}")
        else:
            print(f"  ❌ {dst_name} — missing")
            issues += 1

    # 3. settings.json hook registration
    if SETTINGS_PATH.exists():
        try:
            settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            hooks_cfg = settings.get("hooks", {})
            for hook_file, event in HOOK_EVENTS:
                found = any(
                    hook_file in h.get("command", "")
                    for group in hooks_cfg.get(event, [])
                    for h in group.get("hooks", [])
                )
                if found:
                    print(f"  ✅ settings.json [{event}] → {hook_file}")
                else:
                    print(f"  ⚠ settings.json [{event}] → {hook_file} not registered")
                    issues += 1
        except Exception:
            print("  ❌ settings.json — invalid JSON")
            issues += 1
    else:
        print("  ⚠ settings.json — not found (hooks not registered)")
        issues += 1

    # 4. Cognitive rules
    workspace = Path.cwd()
    rules_dir = workspace / ".claude" / "rules"
    expected_rules = ["00-L0.md", "L1/cbua.md", "L1/wiredo.md", "L1/handoff.md", "L1/autonomous.md"]
    for rel in expected_rules:
        rule_path = rules_dir / rel
        if rule_path.exists():
            print(f"  ✅ rules/{rel}")
        else:
            print(f"  ❌ rules/{rel} — missing")
            issues += 1

    print()
    if issues == 0:
        print("  All checks passed! ✅")
    else:
        print(f"  {issues} issue(s) found. Run `concinno init` to fix.")


# ---------------------------------------------------------------------------
# Autopilot commands
# ---------------------------------------------------------------------------


def cmd_autopilot_init(args: argparse.Namespace) -> None:
    """Full autopilot setup: config + prompts + launcher + scheduler entries."""
    from ..autopilot import install_all

    workspace = args.workspace or str(Path.cwd())
    print(install_all(workspace=workspace, force=args.force))


def cmd_autopilot_list(_args: argparse.Namespace) -> None:
    """List all autopilot tasks."""
    from ..autopilot import list_tasks

    print(list_tasks())


def cmd_autopilot_status(_args: argparse.Namespace) -> None:
    """Show comprehensive autopilot status."""
    from ..autopilot import autopilot_status

    print(autopilot_status())


def cmd_autopilot_enable(args: argparse.Namespace) -> None:
    """Enable an autopilot task."""
    from ..autopilot import enable_task

    print(enable_task(args.name))


def cmd_autopilot_disable(args: argparse.Namespace) -> None:
    """Disable an autopilot task."""
    from ..autopilot import disable_task

    print(disable_task(args.name))


def cmd_autopilot_set(args: argparse.Namespace) -> None:
    """Set an autopilot task parameter."""
    from ..autopilot import set_task_param

    print(set_task_param(args.name, args.key, args.value))


def cmd_autopilot_run(args: argparse.Namespace) -> None:
    """Immediately run an autopilot task."""
    from ..autopilot import run_task_now

    print(run_task_now(args.name))


def cmd_autopilot_uninstall(_args: argparse.Namespace) -> None:
    """Remove all autopilot scheduler entries."""
    from ..autopilot import uninstall_all

    print(uninstall_all())


def cmd_backup_list(_args: argparse.Namespace) -> None:
    """List all destruction guard backups."""
    from ..destruction_guard import list_backups

    print(list_backups())


def cmd_backup_cleanup(args: argparse.Namespace) -> None:
    """Clean expired backups."""
    from ..destruction_guard import cleanup_backups

    days = args.days if hasattr(args, "days") and args.days else None
    print(cleanup_backups(keep_days=days))


def cmd_backup_restore(args: argparse.Namespace) -> None:
    """Restore a specific backup."""
    from ..destruction_guard import restore_backup

    print(restore_backup(args.backup_id))


def cmd_backup_pin(args: argparse.Namespace) -> None:
    """Pin a backup (never auto-delete)."""
    from ..destruction_guard import set_pin

    print(set_pin(args.backup_id, True))


def cmd_backup_unpin(args: argparse.Namespace) -> None:
    """Unpin a backup."""
    from ..destruction_guard import set_pin

    print(set_pin(args.backup_id, False))


def cmd_audit(args: argparse.Namespace) -> None:
    """Run attention audit scans."""
    try:
        from .._recycled.attention_audit import (
            audit_budget,
            auto_upgrade,
            format_budget_report,
            format_strike_report,
            scan_release_candidates,
            scan_strikes,
        )
    except ImportError:
        print("⚠ attention_audit module is recycled and not available.")
        print("  Restore from _recycled/ if needed.")
        return

    mode = args.audit_mode or "all"
    workspace = args.workspace or os.environ.get("CLAUDE_PROJECT_DIR", str(Path.cwd()))

    corrections = os.path.join(workspace, args.corrections) if args.corrections else ""
    budget = os.path.join(workspace, args.budget) if args.budget else ""

    if mode in ("all", "strike") and corrections:
        result = scan_strikes(corrections)
        print(format_strike_report(result))
        print()

    if mode in ("all", "release") and budget:
        candidates = scan_release_candidates(budget)
        if candidates:
            print(f"Release candidates: {len(candidates)}")
            for c in candidates:
                print(f"  → {c.get('name', '?')} (stage={c.get('stage')})")
        else:
            print("No release candidates.")
        print()

    if mode in ("all", "budget") and budget:
        result = audit_budget(budget)
        print(format_budget_report(result))
        print()

    if mode == "upgrade" and budget:
        result = auto_upgrade(budget, dry_run=args.dry)
        print(f"Promoted: {len(result['promoted'])} | Released: {len(result['released'])}")
        if result["written"]:
            print("Budget file updated.")
        elif args.dry:
            print("[DRY RUN] No changes written.")


def cmd_pipeline_status(_args: argparse.Namespace) -> None:
    """Show pipeline state summary."""
    from ..pipeline_state import load_state, pipeline_summary

    state = load_state()
    if not state:
        print("No active pipeline. Start with /think.")
        return

    print(f"Pipeline: {pipeline_summary()}")
    feature = state.get("feature", "")
    if feature:
        print(f"Feature:  {feature}")
    phase = state.get("current_phase", "")
    if phase:
        print(f"Phase:    {phase}")
    nxt = state.get("next_suggested", "")
    if nxt:
        print(f"Next:     {nxt}")
    ts = state.get("timestamp", "")
    if ts:
        print(f"Updated:  {ts}")


def cmd_pipeline_clear(_args: argparse.Namespace) -> None:
    """Clear pipeline state (start fresh)."""
    from ..pipeline_state import clear_state

    if clear_state():
        print("Pipeline state cleared.")
    else:
        print("No pipeline state to clear.")


def cmd_pipeline_design(args: argparse.Namespace) -> None:
    """Generate parallel design prompts for sub-agent exploration."""
    from ..design_theory import DESIGN_CONSTRAINTS, generate_parallel_prompts

    requirements = args.requirements
    constraint_set = args.constraint_set

    if constraint_set not in DESIGN_CONSTRAINTS:
        print(f"Unknown constraint set: {constraint_set}")
        print(f"Available: {', '.join(DESIGN_CONSTRAINTS.keys())}")
        return

    prompts = generate_parallel_prompts(requirements, constraint_set)
    for i, prompt in enumerate(prompts):
        variant = chr(65 + i)
        print(f"{'─' * 60}")
        print(f"Variant {variant}:")
        print(f"{'─' * 60}")
        print(prompt)
        print()


def cmd_validate(args: argparse.Namespace) -> None:
    """Validate handoff files."""
    from ..handoff_validator import format_report, validate_dir

    handoff_dir = args.path or os.environ.get("CLAUDE_PROJECT_DIR", str(Path.cwd()))
    pattern = args.pattern or ""
    results = validate_dir(handoff_dir, pattern=pattern, fix=args.fix)

    if not results:
        print(f"No handoff files found matching {pattern} in {handoff_dir}")
        return

    print(format_report(results))


def cmd_plugins_list(_args: argparse.Namespace) -> None:
    """List all guards (built-in + plugins)."""
    from ..guards.registry import create_default_pipeline
    from ..plugin_loader import discover_plugins

    pipe = create_default_pipeline()
    builtin_names = {g.name for g in pipe._guards}

    print(f"Built-in guards ({len(pipe._guards)}):")
    for g in pipe._guards:
        print(f"  {g.category.name:10s} {g.name}")

    metas = discover_plugins(
        use_entrypoints=True, existing_names=builtin_names,
    )
    valid = [m for m in metas if m.valid]
    invalid = [m for m in metas if not m.valid]

    if valid:
        print(f"\nPlugins ({len(valid)}):")
        for m in valid:
            cat = m.guard.category.name if m.guard else "?"
            name = m.guard.name if m.guard else "?"
            print(f"  {cat:10s} {name}  [{m.source}] {m.path}")

    if invalid:
        print(f"\nInvalid plugins ({len(invalid)}):")
        for m in invalid:
            print(f"  \u274c {m.path}: {'; '.join(m.errors)}")

    if not metas:
        print("\nNo plugins discovered.")


def cmd_plugins_validate(args: argparse.Namespace) -> None:
    """Validate a plugin file."""
    from ..plugin_loader import PluginError, load_guard_file

    path = args.path
    try:
        metas = load_guard_file(path)
    except PluginError as exc:
        print(f"\u274c {exc}")
        sys.exit(1)

    all_valid = True
    for m in metas:
        name = m.guard.name if m.guard else "(unknown)"
        if m.valid:
            print(f"\u2705 {name} — {m.guard.category.name} ({m.load_time_ms:.1f}ms)")
        else:
            print(f"\u274c {name} — {'; '.join(m.errors)}")
            all_valid = False

    if all_valid:
        print(f"\n\u2705 {len(metas)} guard(s) valid in {path}")
    else:
        sys.exit(1)


def cmd_plugins_scan(_args: argparse.Namespace) -> None:
    """Scan for installed plugin entrypoints."""
    from ..plugin_loader import discover_entrypoint_plugins

    metas = discover_entrypoint_plugins()
    if not metas:
        print("No plugin entrypoints found.")
        print('Packages register via [project.entry-points."concinno.guards"]')
        return

    for m in metas:
        status = "\u2705" if m.valid else "\u274c"
        name = m.guard.name if m.guard else "(failed)"
        print(f"  {status} {name}  {m.path} ({m.load_time_ms:.1f}ms)")
        for err in m.errors:
            print(f"     \u26a0\ufe0f  {err}")


def cmd_mcp_server(_args: argparse.Namespace) -> None:
    """Start MCP server over stdio transport."""
    from ..mcp_server import run_stdio_server

    run_stdio_server()


def cmd_cognitive_status(_args: argparse.Namespace) -> None:
    """Show cognitive layer summary."""
    from ..cognitive import cli_status

    print(cli_status())


def cmd_cognitive_thresholds(_args: argparse.Namespace) -> None:
    """Show adaptive thresholds."""
    from ..cognitive import cli_thresholds

    print(cli_thresholds())


def cmd_cognitive_journal(args: argparse.Namespace) -> None:
    """Show decision journal."""
    from ..cognitive import cli_journal

    print(cli_journal(limit=args.n))


def cmd_cognitive_profiles(args: argparse.Namespace) -> None:
    """Show session profiles."""
    from ..cognitive import cli_profiles

    print(cli_profiles(limit=args.n))


def cmd_cognitive_reset(_args: argparse.Namespace) -> None:
    """Reset adaptive thresholds."""
    from ..cognitive import cli_reset_thresholds

    print(cli_reset_thresholds())


def _aegis_context() -> tuple[str, str] | None:
    """Resolve (cache_dir, session_id) for the live session, or None."""
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    session_id = os.environ.get("CC_SESSION_ID", "")
    if not project_dir or not session_id:
        return None
    cache_dir = os.path.join(project_dir, ".concinno_cache")
    return cache_dir, session_id


def cmd_aegis_status(_args: argparse.Namespace) -> None:
    """Show the current session's Aegis progress + cost snapshot.

    Wires TaskOrchestrator + CostTracker + ProgressReporter end-to-end
    so users can inspect task/cost state without touching any hook.
    """
    ctx = _aegis_context()
    if ctx is None:
        print(
            "No session context. Set CLAUDE_PROJECT_DIR and CC_SESSION_ID "
            "to inspect live state."
        )
        return
    cache_dir, session_id = ctx

    from ..cost_tracker import CostTracker
    from ..progress_reporter import ProgressReporter
    from ..task_orchestrator import TaskOrchestrator

    orch = TaskOrchestrator(cache_dir=cache_dir, session_id=session_id)
    cost = CostTracker(cache_dir=cache_dir, session_id=session_id)
    reporter = ProgressReporter(orchestrator=orch, cost_tracker=cost)

    # tool_count isn't tracked persistently — show snapshot with 0
    print(reporter.generate_report(tool_count=0))


def cmd_aegis_goal(args: argparse.Namespace) -> None:
    """Set the current session's goal (and optional subtasks)."""
    ctx = _aegis_context()
    if ctx is None:
        print(
            "No session context. Set CLAUDE_PROJECT_DIR and CC_SESSION_ID.",
            file=sys.stderr,
        )
        sys.exit(1)
    cache_dir, session_id = ctx

    from ..task_orchestrator import TaskOrchestrator

    orch = TaskOrchestrator(cache_dir=cache_dir, session_id=session_id)
    subtasks = args.subtask or None
    orch.set_goal(args.goal, subtasks=subtasks)
    p = orch.progress()
    print(f"OK: goal set — {p['total']} subtask(s)")


# ── cc rag — RAG namespace inspection (no heavy deps) ──────────


def cmd_rag_namespaces(_args: argparse.Namespace) -> None:
    """List the RAG namespaces defined in concinno.rag.NAMESPACES."""
    from ..rag import NAMESPACES

    print(f"{'namespace':<12} {'dirs':<40} description")
    print("-" * 78)
    for name, cfg in NAMESPACES.items():
        dirs = ", ".join(cfg.get("dirs") or []) or "(empty)"
        desc = cfg.get("description", "")
        if len(dirs) > 38:
            dirs = dirs[:35] + "..."
        print(f"{name:<12} {dirs:<40} {desc}")


def cmd_rag_route(args: argparse.Namespace) -> None:
    """Show which namespaces route_query would pick for a given query.

    Exercises ``ZIQRetrieval.route_query`` without touching ChromaDB —
    pure keyword + confidence-band logic, safe on any install.
    """
    from ..ziq_retrieval import ZIQRetrieval

    # cache_dir is only used for FTRL state persistence, which route_query
    # doesn't need. A throwaway path is fine.
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    cache_dir = (
        os.path.join(project_dir, ".concinno_cache")
        if project_dir
        else os.path.join(os.path.expanduser("~"), ".cache", "concinno")
    )

    ziq = ZIQRetrieval(cache_dir=cache_dir)
    namespaces = ziq.route_query(args.query, confidence=args.confidence)

    band = (
        "high-confidence (1 namespace)"
        if args.confidence < 0.3
        else "medium-confidence (2-3 + fusion)"
        if args.confidence <= 0.6
        else "low-confidence (all 5 + reranker)"
    )
    print(f"Query     : {args.query}")
    print(f"Confidence: {args.confidence} — {band}")
    print(f"Routed to : {', '.join(namespaces)}")


def cmd_rag_weights(_args: argparse.Namespace) -> None:
    """Show current ZIQ FTRL source-type weights for this project."""
    from ..ziq_retrieval import ZIQRetrieval

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if not project_dir:
        print(
            "No CLAUDE_PROJECT_DIR — weights are per-project.",
            file=sys.stderr,
        )
        sys.exit(1)
    cache_dir = os.path.join(project_dir, ".concinno_cache")
    stats = ZIQRetrieval(cache_dir=cache_dir).stats()

    print(f"{'source':<12} {'weight':<8} {'hits':<8} {'used':<8} use_rate")
    print("-" * 50)
    for stype, s in stats.items():
        if stype.startswith("_") or "weight" not in s:
            continue
        print(
            f"{stype:<12} {s['weight']:<8.3f} {s['hits']:<8} "
            f"{s['used']:<8} {s['use_rate']:.2f}"
        )


def cmd_cleanup(args: argparse.Namespace) -> None:
    """Run cleanup operations (stale files, log rotation, dead handoffs, git gc)."""
    from concinno.cleanup import run_cleanup

    repo_dir = os.environ.get("CLAUDE_PROJECT_DIR", ".")
    results = run_cleanup(
        repo_dir=repo_dir,
        handoff_dir=getattr(args, "handoff_dir", "") or "",
        log_dir=getattr(args, "log_dir", "") or "",
        dry_run=getattr(args, "dry_run", False),
        squash_git=getattr(args, "squash", False),
        aggressive_gc=getattr(args, "gc", False),
    )
    if not results:
        print("Nothing to clean.")
        return
    for r in results:
        status = "❌" if r.error else "✅"
        summary = r.error or f"{r.items_cleaned}/{r.items_found} cleaned"
        print(f"  {status} {r.action}: {summary}")
        for d in r.details:
            print(f"      {d}")


def cmd_evolve(args: argparse.Namespace) -> None:
    """Weekly evolution digest — research + summarise recent AI/LLM/CC changes."""
    from concinno.tasks.weekly_evolve import TASK_CONFIG, render_prompt

    if args.render_prompt:
        print(render_prompt())
        return

    if args.install:
        from concinno.scheduler import install_schedule

        work_dir = os.environ.get("CLAUDE_PROJECT_DIR", str(Path.cwd()))
        msg = install_schedule(
            TASK_CONFIG["name"],
            interval_minutes=TASK_CONFIG["min_interval_hours"] * 60,
            work_dir=work_dir,
        )
        print(msg)
        return

    if args.run:
        from concinno.scheduler import TaskConfig, launch_task

        work_dir = os.environ.get("CLAUDE_PROJECT_DIR", str(Path.cwd()))
        hooks_dir = str(Path.home() / ".claude" / "hooks")
        config_path = os.path.join(hooks_dir, "schedule_config.json")

        # Write the prompt file if it doesn't exist.
        prompt_path = os.path.join(hooks_dir, TASK_CONFIG["prompt_file"])
        if not os.path.exists(prompt_path):
            os.makedirs(hooks_dir, exist_ok=True)
            with open(prompt_path, "w", encoding="utf-8") as f:
                f.write(render_prompt())

        tc = TaskConfig(**TASK_CONFIG)
        result = launch_task(tc, work_dir, hooks_dir, config_path)

        if result.skipped:
            print(f"SKIP: {result.skip_reason}")
        elif result.error:
            print(f"FAIL: {result.error}")
        else:
            print(f"DONE ({result.duration_sec:.0f}s)")
            if result.output_preview:
                print(result.output_preview[-2000:])
        return

    # No flag → show help.
    print("Usage: concinno evolve [--install | --run | --render-prompt]")
    print(f"  Task: {TASK_CONFIG['name']} (every {TASK_CONFIG['min_interval_hours']}h)")


def cmd_features(args: argparse.Namespace) -> None:
    """Export / sync the FEATURE_META Markdown table into README."""
    from concinno.feature_readme import main as _main
    raise SystemExit(_main([args.features_command] + (
        [args.path] if args.features_command == "sync-readme" and args.path else []
    )))


def cmd_commands(args: argparse.Namespace) -> None:
    """Install / list / clean Concinno slash-commands in ~/.claude/commands/."""
    from concinno.commands_sync import main as _main
    extra = [args.path] if getattr(args, "path", None) else []
    raise SystemExit(_main([args.commands_action] + extra))


def cmd_rules(args: argparse.Namespace) -> None:
    """Install / list / uninstall the bundled official rule set."""
    from concinno.rules_install import main as _main
    extra = [args.path] if getattr(args, "path", None) else []
    raise SystemExit(_main([args.rules_action] + extra))


def cmd_gui(args: argparse.Namespace) -> None:
    """Launch the localhost config GUI (requires ``concinno[gui]`` extras)."""
    try:
        from concinno.gui import run as _run
    except ImportError as err:
        print(f"[gui] missing dependency: {err}")
        print("Install with: pip install 'concinno[gui]'")
        sys.exit(1)
    print(f"[gui] serving on http://{args.host}:{args.port}  (Ctrl+C to stop)")
    _run(host=args.host, port=args.port, reload=args.reload)


def cmd_uninstall(args: argparse.Namespace) -> None:
    """Remove concinno hooks and config."""
    if not args.yes:
        try:
            answer = input("Remove all concinno hooks and config? [y/N] ")
        except EOFError:
            answer = "n"
        if answer.lower() not in ("y", "yes"):
            print("Cancelled.")
            return

    print("🗑 concinno uninstall")
    print()

    # 1. Remove hook files
    for _, dst_name in HOOK_FILES:
        path = HOOKS_DIR / dst_name
        if path.exists():
            path.unlink()
            print(f"  🗑 {dst_name}")

    # 2. Remove config
    cfg_rm = _cfg_path()
    if cfg_rm.exists():
        cfg_rm.unlink()
        print("  🗑 cc_config.json")

    # 3. Unregister from settings.json
    removed = _unregister_hooks_from_settings()
    if removed:
        print(f"  🗑 settings.json — {removed} hook(s) unregistered")
    else:
        print("  ⚠ settings.json — no concinno hooks found")

    print()
    print("  Done! concinno has been removed.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point."""
    # Fix Windows console encoding for Unicode output (⬜, ✅, etc.)
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr,attr-defined]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr,attr-defined]

    parser = argparse.ArgumentParser(
        prog="concinno",
        description="Production-grade hooks for Claude Code",
    )
    sub = parser.add_subparsers(dest="command")

    # init
    p_init = sub.add_parser("init", help="Initialize concinno in current project")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing files")
    p_init.set_defaults(func=cmd_init)

    # enable
    p_enable = sub.add_parser("enable", help="Enable a module")
    p_enable.add_argument("module", help="Module name")
    p_enable.set_defaults(func=cmd_enable)

    # disable
    p_disable = sub.add_parser("disable", help="Disable a module")
    p_disable.add_argument("module", help="Module name")
    p_disable.set_defaults(func=cmd_disable)

    # status
    p_status = sub.add_parser("status", help="Show module status")
    p_status.set_defaults(func=cmd_status)

    # doctor
    p_doctor = sub.add_parser("doctor", help="Check installation health")
    p_doctor.set_defaults(func=cmd_doctor)

    # backup
    p_backup = sub.add_parser("backup", help="Manage destruction guard backups")
    backup_sub = p_backup.add_subparsers(dest="backup_command")

    backup_list = backup_sub.add_parser("list", help="List all backups")
    backup_list.set_defaults(func=cmd_backup_list)

    backup_clean = backup_sub.add_parser("cleanup", help="Clean expired backups")
    backup_clean.add_argument(
        "--days",
        type=int,
        default=None,
        help="Retention days (default: from config)",
    )
    backup_clean.set_defaults(func=cmd_backup_cleanup)

    backup_restore = backup_sub.add_parser("restore", help="Restore a backup")
    backup_restore.add_argument("backup_id", help="Backup ID to restore")
    backup_restore.set_defaults(func=cmd_backup_restore)

    backup_pin = backup_sub.add_parser("pin", help="Pin a backup")
    backup_pin.add_argument("backup_id", help="Backup ID to pin")
    backup_pin.set_defaults(func=cmd_backup_pin)

    backup_unpin = backup_sub.add_parser("unpin", help="Unpin a backup")
    backup_unpin.add_argument("backup_id", help="Backup ID to unpin")
    backup_unpin.set_defaults(func=cmd_backup_unpin)

    p_backup.set_defaults(
        func=lambda a: cmd_backup_list(a) if not a.backup_command else None,
    )

    # autopilot
    p_auto = sub.add_parser("autopilot", help="Manage autopilot maintenance tasks")
    auto_sub = p_auto.add_subparsers(dest="autopilot_command")

    auto_init = auto_sub.add_parser("init", help="Install autopilot (config + scheduler)")
    auto_init.add_argument("--workspace", default="", help="Workspace path (default: cwd)")
    auto_init.add_argument("--force", action="store_true", help="Overwrite existing files")
    auto_init.set_defaults(func=cmd_autopilot_init)

    auto_list = auto_sub.add_parser("list", help="List all autopilot tasks")
    auto_list.set_defaults(func=cmd_autopilot_list)

    auto_status = auto_sub.add_parser("status", help="Show autopilot status")
    auto_status.set_defaults(func=cmd_autopilot_status)

    auto_enable = auto_sub.add_parser("enable", help="Enable an autopilot task")
    auto_enable.add_argument("name", help="Task name (e.g. reflect, scavenger)")
    auto_enable.set_defaults(func=cmd_autopilot_enable)

    auto_disable = auto_sub.add_parser("disable", help="Disable an autopilot task")
    auto_disable.add_argument("name", help="Task name")
    auto_disable.set_defaults(func=cmd_autopilot_disable)

    auto_set = auto_sub.add_parser("set", help="Set task property (model/budget/timeout)")
    auto_set.add_argument("name", help="Task name")
    auto_set.add_argument("key", help="Property: model, budget, timeout, time, freq")
    auto_set.add_argument("value", help="New value")
    auto_set.set_defaults(func=cmd_autopilot_set)

    auto_run = auto_sub.add_parser("run", help="Immediately run an autopilot task")
    auto_run.add_argument("name", help="Task name")
    auto_run.set_defaults(func=cmd_autopilot_run)

    auto_uninstall = auto_sub.add_parser("uninstall", help="Remove autopilot scheduler entries")
    auto_uninstall.set_defaults(func=cmd_autopilot_uninstall)

    p_auto.set_defaults(
        func=lambda a: cmd_autopilot_list(a) if not a.autopilot_command else None,
    )

    # cognitive
    p_cog = sub.add_parser("cognitive", help="Cognitive layer inspection")
    cog_sub = p_cog.add_subparsers(dest="cognitive_command")

    cog_status = cog_sub.add_parser("status", help="Show cognitive summary")
    cog_status.set_defaults(func=cmd_cognitive_status)

    cog_thresh = cog_sub.add_parser(
        "thresholds",
        help="Show adaptive thresholds",
    )
    cog_thresh.set_defaults(func=cmd_cognitive_thresholds)

    cog_journal = cog_sub.add_parser(
        "journal",
        help="Show decision journal",
    )
    cog_journal.add_argument(
        "-n",
        type=int,
        default=10,
        help="Number of entries",
    )
    cog_journal.set_defaults(func=cmd_cognitive_journal)

    cog_profiles = cog_sub.add_parser(
        "profiles",
        help="Show session profiles",
    )
    cog_profiles.add_argument(
        "-n",
        type=int,
        default=10,
        help="Number of entries",
    )
    cog_profiles.set_defaults(func=cmd_cognitive_profiles)

    cog_reset = cog_sub.add_parser(
        "reset",
        help="Reset adaptive thresholds to defaults",
    )
    cog_reset.set_defaults(func=cmd_cognitive_reset)

    p_cog.set_defaults(
        func=lambda a: cmd_cognitive_status(a) if not a.cognitive_command else None,
    )

    # audit
    p_audit = sub.add_parser("audit", help="Attention budget audit + strike scan")
    p_audit.add_argument(
        "audit_mode", nargs="?", default="all",
        choices=["all", "strike", "release", "budget", "upgrade"],
        help="Scan mode (default: all)",
    )
    p_audit.add_argument("--workspace", default="", help="Workspace path")
    p_audit.add_argument(
        "--corrections", default="",
        help="Relative path to corrections JSONL",
    )
    p_audit.add_argument(
        "--budget", default="",
        help="Relative path to attention-budget.json",
    )
    p_audit.add_argument("--dry", action="store_true", help="Dry run (upgrade mode)")
    p_audit.set_defaults(func=cmd_audit)

    # pipeline
    p_pipe = sub.add_parser("pipeline", help="Think→Ship pipeline state management")
    pipe_sub = p_pipe.add_subparsers(dest="pipeline_command")

    pipe_status = pipe_sub.add_parser("status", help="Show pipeline progress")
    pipe_status.set_defaults(func=cmd_pipeline_status)

    pipe_clear = pipe_sub.add_parser("clear", help="Clear pipeline state")
    pipe_clear.set_defaults(func=cmd_pipeline_clear)

    pipe_design = pipe_sub.add_parser(
        "design", help="Generate parallel design prompts"
    )
    pipe_design.add_argument("requirements", help="Feature requirements text")
    pipe_design.add_argument(
        "--constraint-set",
        dest="constraint_set",
        default="performance_vs_readability",
        help="Constraint set (performance_vs_readability|monolith_vs_modular|defensive_vs_minimal)",
    )
    pipe_design.set_defaults(func=cmd_pipeline_design)

    p_pipe.set_defaults(
        func=lambda a: cmd_pipeline_status(a) if not a.pipeline_command else None,
    )

    # validate
    p_validate = sub.add_parser("validate", help="Validate handoff files")
    p_validate.add_argument("path", nargs="?", default="", help="Handoff directory")
    p_validate.add_argument("--pattern", default="", help="Glob pattern (auto-detect)")
    p_validate.add_argument("--fix", action="store_true", help="Auto-fix missing frontmatter")
    p_validate.set_defaults(func=cmd_validate)

    # plugins
    p_plug = sub.add_parser("plugins", help="Manage guard plugins")
    plug_sub = p_plug.add_subparsers(dest="plugins_command")

    plug_list = plug_sub.add_parser("list", help="List all guards (built-in + plugins)")
    plug_list.set_defaults(func=cmd_plugins_list)

    plug_validate = plug_sub.add_parser("validate", help="Validate a plugin file")
    plug_validate.add_argument("path", help="Path to plugin .py file")
    plug_validate.set_defaults(func=cmd_plugins_validate)

    plug_scan = plug_sub.add_parser("scan", help="Scan for installed plugin entrypoints")
    plug_scan.set_defaults(func=cmd_plugins_scan)

    p_plug.set_defaults(
        func=lambda a: cmd_plugins_list(a) if not a.plugins_command else None,
    )

    # aegis — task/cost inspection
    p_aegis = sub.add_parser(
        "aegis",
        help="Inspect live Aegis task/cost state",
    )
    aegis_sub = p_aegis.add_subparsers(dest="aegis_command")

    aegis_status = aegis_sub.add_parser(
        "status",
        help="Show progress + cost snapshot",
    )
    aegis_status.set_defaults(func=cmd_aegis_status)

    aegis_goal = aegis_sub.add_parser(
        "goal",
        help="Set the current session's goal",
    )
    aegis_goal.add_argument("goal", help="Goal description")
    aegis_goal.add_argument(
        "--subtask",
        action="append",
        help="Subtask label (repeatable)",
    )
    aegis_goal.set_defaults(func=cmd_aegis_goal)

    def _aegis_default(_args: argparse.Namespace) -> None:
        p_aegis.print_help()

    p_aegis.set_defaults(func=_aegis_default)

    # rag — namespace routing inspection
    p_rag = sub.add_parser(
        "rag",
        help="Inspect RAG namespaces and ZIQ routing",
    )
    rag_sub = p_rag.add_subparsers(dest="rag_command")

    rag_ns = rag_sub.add_parser(
        "namespaces",
        help="List RAG namespaces",
    )
    rag_ns.set_defaults(func=cmd_rag_namespaces)

    rag_route = rag_sub.add_parser(
        "route",
        help="Show routed namespaces for a query",
    )
    rag_route.add_argument("query", help="Query text")
    rag_route.add_argument(
        "--confidence",
        type=float,
        default=0.5,
        help="ZIQ alpha_t uncertainty (0=certain, 1=unknown)",
    )
    rag_route.set_defaults(func=cmd_rag_route)

    rag_w = rag_sub.add_parser(
        "weights",
        help="Show current ZIQ FTRL source-type weights",
    )
    rag_w.set_defaults(func=cmd_rag_weights)

    def _rag_default(_args: argparse.Namespace) -> None:
        p_rag.print_help()

    p_rag.set_defaults(func=_rag_default)

    # cleanup
    p_clean = sub.add_parser("cleanup", help="Run cleanup (stale files, logs, git gc)")
    p_clean.add_argument("--handoff-dir", default="", help="Handoff directory to scan")
    p_clean.add_argument("--log-dir", default="", help="Log directory to rotate")
    p_clean.add_argument("--dry-run", action="store_true", help="Show what would be cleaned")
    p_clean.add_argument("--squash", action="store_true", help="Squash old auto-commits")
    p_clean.add_argument("--gc", action="store_true", help="Run aggressive git gc")
    p_clean.set_defaults(func=cmd_cleanup)

    # evolve
    p_evolve = sub.add_parser(
        "evolve",
        help="Weekly evolution digest — research + summarise recent AI/LLM/CC developments",
    )
    p_evolve.add_argument(
        "--install", action="store_true",
        help="Install recurring weekly schedule (platform-native)",
    )
    p_evolve.add_argument(
        "--run", action="store_true",
        help="Immediately run the weekly evolve task",
    )
    p_evolve.add_argument(
        "--render-prompt", action="store_true",
        help="Print the prompt template (debug)",
    )
    p_evolve.set_defaults(func=cmd_evolve)

    # config — layered user/project/env config for mode, locale, flags
    from .config_cmd import register as _register_config
    _register_config(sub)

    # convention — workspace convention engine CLI (2.12.1)
    from .convention_cmd import register as _register_convention
    _register_convention(sub)

    # 2.16.0 — session-switches / configure-permissions / publish
    from .configure_permissions_cmd import register as _register_cfg_perms
    from .publish_cmd import register as _register_publish
    from .session_switches_cmd import register as _register_switches
    _register_switches(sub)
    _register_cfg_perms(sub)
    _register_publish(sub)

    # 2.17.0 — new-feature scaffold CLI
    from .new_feature_cmd import register as _register_new_feature
    _register_new_feature(sub)

    # 2.30.1 — `concinno skills {new, list, enable, disable, delete}`
    from .skills_cmd import register as _register_skills
    _register_skills(sub)

    # narrower-scope v4 — preset cascade CLI
    from .preset_cmd import register as _register_preset
    _register_preset(sub)

    # mcp-server
    p_mcp = sub.add_parser("mcp-server", help="Start MCP server (stdio transport)")
    p_mcp.set_defaults(func=cmd_mcp_server)

    # rules — install / list / uninstall bundled official rules
    p_rules = sub.add_parser(
        "rules",
        help="Install / list / uninstall the bundled official rule set",
    )
    rules_sub = p_rules.add_subparsers(dest="rules_action")
    r_install = rules_sub.add_parser(
        "install", help="Install official rules to ~/.claude/rules/official/",
    )
    r_install.add_argument("path", nargs="?", default=None)
    r_install.set_defaults(func=cmd_rules)
    r_list = rules_sub.add_parser("list", help="List bundled rule files")
    r_list.set_defaults(func=cmd_rules)
    r_dry = rules_sub.add_parser(
        "dry-run", help="Preview install without writing",
    )
    r_dry.add_argument("path", nargs="?", default=None)
    r_dry.set_defaults(func=cmd_rules)
    r_uninstall = rules_sub.add_parser(
        "uninstall", help="Remove installed official rule tree",
    )
    r_uninstall.add_argument("path", nargs="?", default=None)
    r_uninstall.set_defaults(func=cmd_rules)

    # commands — install / list / clean Concinno slash-commands into CC
    p_cmd = sub.add_parser(
        "commands",
        help="Install Concinno slash-commands into ~/.claude/commands/",
    )
    cmd_sub = p_cmd.add_subparsers(dest="commands_action")
    cmd_sync = cmd_sub.add_parser("sync", help="Install/refresh commands")
    cmd_sync.add_argument("path", nargs="?", default=None)
    cmd_sync.set_defaults(func=cmd_commands)
    cmd_list = cmd_sub.add_parser("list", help="List discoverable slash commands")
    cmd_list.set_defaults(func=cmd_commands)
    cmd_clean = cmd_sub.add_parser("clean", help="Remove managed command files")
    cmd_clean.add_argument("path", nargs="?", default=None)
    cmd_clean.set_defaults(func=cmd_commands)

    # features — FEATURE_META export / README sync
    p_feat = sub.add_parser(
        "features",
        help="Export FEATURE_META as Markdown / sync README",
    )
    feat_sub = p_feat.add_subparsers(dest="features_command")
    feat_export = feat_sub.add_parser(
        "export-readme",
        help="Print FEATURE_META Markdown table to stdout",
    )
    feat_export.set_defaults(func=cmd_features)
    feat_sync = feat_sub.add_parser(
        "sync-readme",
        help="Inject FEATURE_META table into README.md between anchors",
    )
    feat_sync.add_argument("path", nargs="?", default=None,
                           help="Target README path (default: repo README.md)")
    feat_sync.set_defaults(func=cmd_features)

    # 2.30.1 — user-feature registry commands (attach to same `features` namespace)
    from .features_register_cmd import register_features_subcommands
    register_features_subcommands(feat_sub)

    # gui — localhost config dashboard (requires `concinno[gui]` extras)
    p_gui = sub.add_parser(
        "gui",
        help="Launch localhost config GUI (requires `pip install concinno[gui]`)",
    )
    p_gui.add_argument("--host", default="127.0.0.1",
                       help="Bind host (default: 127.0.0.1 loopback)")
    p_gui.add_argument("--port", type=int, default=8400,
                       help="Bind port (default: 8400)")
    p_gui.add_argument("--reload", action="store_true",
                       help="Enable uvicorn auto-reload (dev only)")
    p_gui.set_defaults(func=cmd_gui)

    # uninstall
    p_uninstall = sub.add_parser("uninstall", help="Remove concinno hooks and config")
    p_uninstall.add_argument("-y", "--yes", action="store_true", help="Skip confirmation")
    p_uninstall.set_defaults(func=cmd_uninstall)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    args.func(args)


if __name__ == "__main__":
    main()
