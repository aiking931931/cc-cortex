"""concinno.cli.plugins_cmd -- ``concinno plugins`` CLI namespace.

2.32.0 extends the pre-existing ``plugins list`` / ``scan`` /
``validate`` trio (which covered only ``concinno.guards`` plugins,
shipped before 2.31.0) with:

* A **unified** ``plugins list`` that also shows
  ``concinno.features`` and ``concinno.skills`` plugins and the
  effective allowlist state (env + file).
* A new ``plugins allowlist`` subcommand tree:

  * ``add <pkg>`` / ``remove <pkg>`` -- mutate
    ``~/.concinno/plugins_allowlist.json``
  * ``show`` -- print current allowlist (file + env, side by side)
  * ``export-env`` -- print a shell ``export`` line so users can
    promote the file contents to the env var the runtime actually
    reads.

Design note (per 2.32.0 commander adjudication): the file allowlist
does **not** flow into the runtime
:func:`concinno.plugins.plugin_allowlist` gate -- that stays
env-var-only. File is a CLI-scoped persistence aid.

JSON output shape is marked ``"schema_version": 0`` (UNSTABLE); see
``~/.concinno/docs/how-to-ship-a-skills-package.md`` and the 2.32.0
CHANGELOG for the semver policy.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import sys
from typing import Any

# Current JSON output shape -- UNSTABLE, may evolve until 3.0.0.
OUTPUT_SCHEMA_VERSION = 0


def _installed_distributions() -> set[str]:
    """Return the set of currently-installed distribution names.

    Used by ``allowlist add`` to warn about typos / pre-install
    entries, and by ``list`` to flag "allowlisted but not installed"
    rows.
    """
    names: set[str] = set()
    try:
        for dist in importlib.metadata.distributions():
            name = dist.metadata.get("Name") if dist.metadata else None
            if name:
                names.add(name)
                names.add(name.replace("-", "_"))
                names.add(name.replace("_", "-"))
    except Exception:
        pass
    return names


def _gather_guards_rows() -> list[dict[str, Any]]:
    """Reach into the pre-existing guards discovery path."""
    try:
        from concinno.guards.registry import create_default_pipeline
        from concinno.plugin_loader import discover_plugins
    except Exception as exc:
        return [{
            "kind": "guards",
            "error": f"could not import guards discovery: {exc}",
        }]

    pipe = create_default_pipeline()
    builtin_names = {g.name for g in pipe._guards}
    builtin_rows = [
        {
            "kind": "guards",
            "source": "built-in",
            "name": g.name,
            "category": g.category.name,
        }
        for g in pipe._guards
    ]
    metas = discover_plugins(use_entrypoints=True, existing_names=builtin_names)
    plugin_rows: list[dict[str, Any]] = []
    for m in metas:
        plugin_rows.append({
            "kind": "guards",
            "source": "plugin",
            "valid": m.valid,
            "name": m.guard.name if m.guard else "(failed)",
            "category": m.guard.category.name if m.guard else "?",
            "entry_point": m.path,
            "load_time_ms": round(m.load_time_ms, 2),
            "errors": list(m.errors),
        })
    return builtin_rows + plugin_rows


def _gather_features_rows() -> list[dict[str, Any]]:
    from concinno.plugins.features import discover_feature_entrypoints

    rows: list[dict[str, Any]] = []
    for m in discover_feature_entrypoints():
        rows.append({
            "kind": "features",
            "source": "plugin",
            "package": m.package,
            "entry_point": m.entry_point_name,
            "features": sorted(m.meta_dict.keys()),
            "valid": m.valid,
            "load_time_ms": round(m.load_time_ms, 2),
            "errors": list(m.errors),
        })
    return rows


def _gather_skills_rows() -> list[dict[str, Any]]:
    from concinno.plugins.skills import discover_skill_entrypoints

    rows: list[dict[str, Any]] = []
    for m in discover_skill_entrypoints():
        skill_names: list[str] = []
        if m.resolved_path is not None and m.resolved_path.is_dir():
            try:
                skill_names = sorted(
                    child.name for child in m.resolved_path.iterdir()
                    if child.is_dir() and (child / "SKILL.md").is_file()
                )
            except OSError:
                pass
        rows.append({
            "kind": "skills",
            "source": "plugin",
            "package": m.package,
            "entry_point": m.entry_point_name,
            "resolved_path": str(m.resolved_path) if m.resolved_path else None,
            "skills": skill_names,
            "valid": m.valid,
            "load_time_ms": round(m.load_time_ms, 2),
            "errors": list(m.errors),
        })
    return rows


def _allowlist_state() -> dict[str, Any]:
    from concinno.plugins import is_plugins_enabled
    from concinno.plugins.allowlist_file import (
        get_note,
        get_updated_at,
        load_allowlist_file,
    )

    file_list = load_allowlist_file()
    env_raw = os.environ.get("CONCINNO_PLUGINS_ALLOWLIST", "").strip()
    env_list = [p.strip() for p in env_raw.split(",") if p.strip()] if env_raw else []
    return {
        "discovery_enabled": is_plugins_enabled(),
        "allowlist_file": file_list,
        "allowlist_env": env_list,
        "effective_runtime": env_list if env_list else None,
        "note": get_note(),
        "updated_at": get_updated_at(),
    }


def _gather_all(*, verbose: bool) -> dict[str, Any]:
    # Guards pipeline construction is the heaviest step (builds the
    # default guard registry + imports every guard module). Skip it
    # unless ``--verbose`` is requested so the default ``plugins list``
    # stays fast (< 500 ms on realistic systems, per Red Opus HIGH-3).
    guards = _gather_guards_rows() if verbose else []
    features = _gather_features_rows()
    skills = _gather_skills_rows()
    state = _allowlist_state()
    installed = _installed_distributions()

    # Annotate allowlist rows with installed status.
    file_rows: list[dict[str, Any]] = []
    for pkg in state["allowlist_file"]:
        file_rows.append({
            "package": pkg,
            "installed": (pkg in installed),
        })
    env_rows: list[dict[str, Any]] = []
    for pkg in state["allowlist_env"]:
        env_rows.append({
            "package": pkg,
            "installed": (pkg in installed),
        })

    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "discovery_enabled": state["discovery_enabled"],
        "guards": guards,
        "features": features,
        "skills": skills,
        "allowlist": {
            "file": file_rows,
            "env": env_rows,
            "note": state["note"],
            "updated_at": state["updated_at"],
            "effective_runtime_source": (
                "env" if state["allowlist_env"]
                else "none (all plugins allowed)"
            ),
        },
        "plugin_load_errors": [
            row for row in (features + skills) if row.get("errors")
        ],
    }


def _print_text(data: dict[str, Any], *, verbose: bool) -> None:
    pad = "  "
    print(f"Plugin discovery: {'enabled' if data['discovery_enabled'] else 'DISABLED'}")

    print()
    print("Features plugins (concinno.features):")
    if not data["features"]:
        print(f"{pad}(none installed)")
    for row in data["features"]:
        status = "OK" if row["valid"] else "ERROR"
        feats = ", ".join(row["features"]) or "(none)"
        print(f"{pad}{row['package']}  [{status}]  features: {feats}")
        if verbose and row["errors"]:
            for e in row["errors"]:
                print(f"{pad}{pad}! {e}")

    print()
    print("Skills plugins (concinno.skills):")
    if not data["skills"]:
        print(f"{pad}(none installed)")
    for row in data["skills"]:
        status = "OK" if row["valid"] else "ERROR"
        skills = ", ".join(row["skills"]) or "(none)"
        print(f"{pad}{row['package']}  [{status}]  skills: {skills}")
        if verbose and row["errors"]:
            for e in row["errors"]:
                print(f"{pad}{pad}! {e}")

    print()
    print("Allowlist (CLI-scope only; runtime reads env var):")
    al = data["allowlist"]
    print(f"{pad}File entries: {len(al['file'])}")
    for row in al["file"]:
        tag = "installed" if row["installed"] else "NOT INSTALLED"
        print(f"{pad}{pad}- {row['package']}  [{tag}]")
    print(f"{pad}Env  entries: {len(al['env'])}")
    for row in al["env"]:
        tag = "installed" if row["installed"] else "NOT INSTALLED"
        print(f"{pad}{pad}- {row['package']}  [{tag}]")
    print(f"{pad}Effective runtime source: {al['effective_runtime_source']}")
    if al["note"]:
        print(f"{pad}Note: {al['note']}")
    if al["updated_at"]:
        print(f"{pad}Updated at: {al['updated_at']}")

    if verbose:
        print()
        print("Guards (built-in + plugins):")
        for row in data["guards"]:
            tag = row.get("source", "?")
            print(f"{pad}[{tag}] {row.get('category', '?'):10s} {row['name']}")

    if data["plugin_load_errors"]:
        print()
        print(
            f"⚠ {len(data['plugin_load_errors'])} plugin load error(s); "
            f"re-run with --verbose for details."
        )


def cmd_plugins_list(args: argparse.Namespace) -> None:
    """Unified plugin listing across guards / features / skills +
    allowlist state. Replaces the 2.31.0 guards-only view.
    """
    verbose = getattr(args, "verbose", False)
    fmt = getattr(args, "format", "text")
    data = _gather_all(verbose=verbose)
    if fmt == "json":
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        _print_text(data, verbose=verbose)


def cmd_plugins_allowlist_show(args: argparse.Namespace) -> None:
    fmt = getattr(args, "format", "text")
    from concinno.plugins.allowlist_file import (
        get_note,
        get_updated_at,
        load_allowlist_file,
    )

    file_list = load_allowlist_file()
    env_raw = os.environ.get("CONCINNO_PLUGINS_ALLOWLIST", "").strip()
    env_list = [p.strip() for p in env_raw.split(",") if p.strip()] if env_raw else []
    installed = _installed_distributions()

    payload = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "file": [
            {"package": p, "installed": p in installed}
            for p in file_list
        ],
        "env": [
            {"package": p, "installed": p in installed}
            for p in env_list
        ],
        "note": get_note(),
        "updated_at": get_updated_at(),
    }
    if fmt == "json":
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    print(f"Allowlist file entries ({len(file_list)}):")
    for row in payload["file"]:
        tag = "installed" if row["installed"] else "NOT INSTALLED"
        print(f"  - {row['package']}  [{tag}]")
    print(f"Allowlist env entries ({len(env_list)}):")
    for row in payload["env"]:
        tag = "installed" if row["installed"] else "NOT INSTALLED"
        print(f"  - {row['package']}  [{tag}]")
    print()
    print("Runtime gating reads env var only (2.31.0 behaviour).")
    print("To promote file -> env:   concinno plugins allowlist export-env")
    if payload["note"]:
        print(f"Note: {payload['note']}")
    if payload["updated_at"]:
        print(f"Updated at: {payload['updated_at']}")


def cmd_plugins_allowlist_add(args: argparse.Namespace) -> None:
    from concinno.plugins.allowlist_file import add_to_allowlist

    pkg = args.package.strip()
    note = (getattr(args, "note", "") or "").strip()
    if not pkg:
        print("concinno: error -- package name required", file=sys.stderr)
        sys.exit(2)

    installed = _installed_distributions()
    if pkg not in installed:
        print(
            f"concinno: warning -- '{pkg}' is not currently installed; "
            f"allowlist entry will activate when pip installed",
            file=sys.stderr,
        )

    success, newly_added = add_to_allowlist(pkg, note=note)
    if not success:
        sys.exit(1)
    if newly_added:
        print(f"Added '{pkg}' to ~/.concinno/plugins_allowlist.json.")
    else:
        print(f"'{pkg}' was already in the allowlist; no change.")


def cmd_plugins_allowlist_remove(args: argparse.Namespace) -> None:
    from concinno.plugins.allowlist_file import remove_from_allowlist

    pkg = args.package.strip()
    if not pkg:
        print("concinno: error -- package name required", file=sys.stderr)
        sys.exit(2)

    success, removed = remove_from_allowlist(pkg)
    if not success:
        sys.exit(1)
    if removed:
        print(f"Removed '{pkg}' from ~/.concinno/plugins_allowlist.json.")
    else:
        print(f"'{pkg}' was not in the allowlist; no change.")


def cmd_plugins_allowlist_export_env(args: argparse.Namespace) -> None:
    """Print a shell ``export`` line for the current file allowlist."""
    from concinno.plugins.allowlist_file import load_allowlist_file

    fmt = getattr(args, "format", "text")
    pkgs = load_allowlist_file()
    joined = ",".join(pkgs)
    if fmt == "json":
        print(json.dumps({
            "schema_version": OUTPUT_SCHEMA_VERSION,
            "env_var": "CONCINNO_PLUGINS_ALLOWLIST",
            "value": joined,
        }, ensure_ascii=False))
    else:
        print(f"export CONCINNO_PLUGINS_ALLOWLIST={joined}")


def register_allowlist_subparsers(
    plug_sub: argparse._SubParsersAction,
) -> None:
    """Register the ``allowlist`` subcommand tree under ``plugins``.

    Called from :mod:`concinno.cli.main` after the pre-existing
    ``list`` / ``scan`` / ``validate`` subparsers are created.
    """
    p_allow = plug_sub.add_parser(
        "allowlist",
        help="Manage the CLI-scope plugin allowlist file",
    )
    allow_sub = p_allow.add_subparsers(dest="allowlist_command")

    p_show = allow_sub.add_parser("show", help="Show file + env allowlist")
    p_show.add_argument("--format", choices=("text", "json"), default="text")
    p_show.set_defaults(func=cmd_plugins_allowlist_show)

    p_add = allow_sub.add_parser("add", help="Add a package to the file allowlist")
    p_add.add_argument("package", help="Distribution name (e.g. concinno-skills-google)")
    p_add.add_argument("--note", default="", help="Operator annotation (overwrites existing note)")
    p_add.set_defaults(func=cmd_plugins_allowlist_add)

    p_rm = allow_sub.add_parser("remove", help="Remove a package from the file allowlist")
    p_rm.add_argument("package", help="Distribution name")
    p_rm.set_defaults(func=cmd_plugins_allowlist_remove)

    p_exp = allow_sub.add_parser(
        "export-env",
        help="Print a shell export line for the current file allowlist",
    )
    p_exp.add_argument("--format", choices=("text", "json"), default="text")
    p_exp.set_defaults(func=cmd_plugins_allowlist_export_env)

    p_allow.set_defaults(
        func=lambda a: cmd_plugins_allowlist_show(a)
        if not getattr(a, "allowlist_command", None)
        else None,
    )
