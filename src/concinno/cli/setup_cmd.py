"""concinno.cli.setup_cmd — ``concinno setup`` subcommand.

@module setup_cmd
@responsibility Expose the five-profile starter recommender from
    :mod:`concinno.setup.recommender` as a deferred-input CLI:
    ``setup --list`` / ``setup --profile=<name>`` /
    ``setup --profile=<name> --apply``. No interactive prompts —
    harness compatibility favours flag-driven invocation.
@dependencies concinno.setup.recommender
@exports register, cmd_setup
"""

from __future__ import annotations

import argparse
import json
import sys

from concinno.setup.recommender import PROFILES, apply, recommend


def _print_list(*, as_json: bool) -> None:
    """Print all five profiles + their descriptions."""
    if as_json:
        payload = {
            "schema": "concinno.setup.v1",
            "profiles": [
                {
                    "name": p.name,
                    "description": p.description,
                    "feature_keys": sorted(p.feature_overrides.keys()),
                    "notes": list(p.notes),
                }
                for p in PROFILES.values()
            ],
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    print("Available profiles:")
    print()
    for profile in PROFILES.values():
        print(f"  {profile.name:<12} {profile.description}")
    print()
    print(
        "Run `concinno setup --profile=<name>` for a dry-run preview, "
        "then add `--apply` to persist."
    )


def cmd_setup(args: argparse.Namespace) -> None:
    """Dispatch ``concinno setup`` based on flag combination."""
    as_json = getattr(args, "format", "text") == "json"

    if getattr(args, "list", False):
        _print_list(as_json=as_json)
        return

    profile_name = getattr(args, "profile", None)
    if not profile_name:
        # Deferred-input mode: print catalogue + usage hint and exit 0.
        _print_list(as_json=as_json)
        return

    if profile_name not in PROFILES:
        valid = ", ".join(sorted(PROFILES.keys()))
        print(
            f"error: unknown profile {profile_name!r}; valid: {valid}",
            file=sys.stderr,
        )
        sys.exit(2)

    do_apply = bool(getattr(args, "apply", False))
    preview = recommend(profile_name)
    diff = apply(profile_name, dry_run=not do_apply)

    if as_json:
        payload = {
            "schema": "concinno.setup.v1",
            "preview": preview,
            "diff": diff,
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    mode = "APPLIED" if do_apply else "DRY-RUN"
    print(f"profile: {preview['profile']}  [{mode}]")
    print(f"  description: {preview['description']}")
    print(f"  config path: {diff['path']}")
    print()
    print("  Feature overrides:")
    for key in sorted(preview["features"].keys()):
        marker = "*" if key in diff["changed"] else " "
        overrides = preview["features"][key]
        print(f"   {marker} {key}: {overrides}")
    if preview["notes"]:
        print()
        print("  Notes:")
        for note in preview["notes"]:
            print(f"    - {note}")
    print()
    if do_apply:
        print(f"  Wrote {len(diff['changed'])} feature change(s) to {diff['path']}.")
    else:
        print(
            f"  Dry-run only — {len(diff['changed'])} feature(s) would change. "
            "Re-run with --apply to persist."
        )


def register(subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    """Register ``setup`` on the argparse tree."""
    p = subparsers.add_parser(
        "setup",
        help="Recommend a starter feature configuration for a user type",
        description=(
            "Map your Claude Code user type (senior / junior / benchmark "
            "/ production / researcher) to a tailored Concinno feature "
            "override block. Defaults to a dry-run preview; pass --apply "
            "to persist into ~/.claude/hooks/cc_config.json."
        ),
    )
    p.add_argument(
        "--list",
        action="store_true",
        help="List the five profiles + their descriptions",
    )
    p.add_argument(
        "--profile",
        default=None,
        choices=sorted(PROFILES.keys()),
        help="Profile to preview or apply",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Persist the change instead of showing a dry-run",
    )
    p.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    p.set_defaults(func=cmd_setup)
