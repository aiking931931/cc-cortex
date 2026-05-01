"""``concinno features set-profile <name>`` — bulk-toggle 4.0.0 default-off
features in one command.

Added in 4.2.x as the carryover deferred from the 4.0.0 SEMVER-MAJOR
ship. The 4.0.0 release flipped 27 hard-gate / soft-gate features
default-OFF for senior-dev permissive baseline; users wanting strict
mode previously had to run ``concinno config set features.<name>.enabled
true`` 27 times. This shortcut applies a named profile in one call.

Profile definitions live in
:data:`concinno.feature_config.FEATURE_TOGGLE_PROFILES`. Bound profiles:

* ``strict`` — enable every feature in ``DEFAULT_OFF_4_0_0`` (27 guards).
* ``permissive`` — disable every feature in ``DEFAULT_OFF_4_0_0`` (revert
  ``strict``; no-op on a fresh install).
* ``dev`` — enable productivity tools only (``dspy_prompt_optimization``,
  ``polling_watcher``, ``pip_aftermath_hint``).

Idempotent: re-running ``set-profile strict`` after the first apply
yields zero diff (``enabled == disabled == []``, ``unchanged ==``
full set).
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

__all__ = [
    "cmd_features_disable_all_d_class",
    "cmd_features_set_profile",
    "register_disable_all_d_class",
    "register_set_profile",
]


def _print_summary(result: dict[str, Any]) -> None:
    """Render a human-readable summary of the toggle result to stdout."""
    if result.get("error"):
        print(f"Error: {result['error']}", file=sys.stderr)
        return

    profile = result["profile"]
    enabled = result.get("enabled", [])
    disabled = result.get("disabled", [])
    unchanged = result.get("unchanged", [])
    errors = result.get("errors", [])

    print(
        f"Applied profile '{profile}': "
        f"{len(enabled)} features enabled, "
        f"{len(disabled)} disabled, "
        f"{len(unchanged)} unchanged."
    )
    if result.get("description"):
        print(f"  ↳ {result['description']}")
    if enabled:
        print(f"  Enabled  ({len(enabled)}): {', '.join(enabled)}")
    if disabled:
        print(f"  Disabled ({len(disabled)}): {', '.join(disabled)}")
    if errors:
        print("  Errors:", file=sys.stderr)
        for err in errors:
            print(f"    {err}", file=sys.stderr)


def cmd_features_set_profile(args: argparse.Namespace) -> None:
    """Implementation of ``concinno features set-profile <name>``.

    Honours an optional ``args._injected_cfg`` (set by tests) so the
    apply call writes to a tmp ``cc_config.json`` instead of the
    process-wide singleton.

    Side effect: every successful invocation (including ``--list``) writes
    the ``~/.concinno/.4_0_0_seen`` first-run marker via
    :func:`concinno.cli._first_run.mark_seen`. This silences the
    onboarding banner from then on — the user has consciously engaged
    with the profile system, so further nag is noise.
    """
    from concinno.cli._first_run import mark_seen
    from concinno.feature_config import (
        FEATURE_TOGGLE_PROFILES,
        apply_feature_toggle_profile,
        list_feature_toggle_profiles,
    )

    if getattr(args, "list_profiles", False):
        # Touch the marker even on --list so a user who just inspects
        # the available profiles doesn't get nagged again on the next
        # invocation.
        mark_seen()
        profiles = list_feature_toggle_profiles()
        print("Available profiles:")
        for name, desc in profiles.items():
            print(f"  {name:12s}  {desc}")
        return

    name = getattr(args, "profile_name", None)
    if not name:
        print(
            "Usage: concinno features set-profile <name>\n"
            f"Available: {', '.join(FEATURE_TOGGLE_PROFILES)}",
            file=sys.stderr,
        )
        raise SystemExit(2)

    injected_cfg = getattr(args, "_injected_cfg", None)
    result = apply_feature_toggle_profile(name, cfg=injected_cfg)

    # Mark the user as having seen / acknowledged the 4.0.0 onboarding
    # message regardless of which profile was chosen — even ``permissive``
    # (the no-op profile) is a conscious "I want default-OFF" decision.
    if not result.get("error"):
        mark_seen()

    if getattr(args, "json_output", False):
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        _print_summary(result)

    if result.get("error"):
        raise SystemExit(1)


def cmd_features_disable_all_d_class(args: argparse.Namespace) -> None:
    """Implementation of ``concinno features disable-all-d-class``.

    Thin alias for ``concinno features set-profile 4-x-compat``. The
    explicit name exists per the 5.0.0 SEMVER-MAJOR migration plan
    (verdict doc 2026-04-29 P1 #1 step 7) so users searching for
    "how do I get my old 4.x default-off behaviour back" land on the
    obvious command rather than having to learn the profile vocabulary.
    Idempotent — re-running yields ``unchanged`` for the full D-class
    set.
    """
    args.profile_name = "4-x-compat"
    cmd_features_set_profile(args)


def register_disable_all_d_class(
    features_sub: argparse._SubParsersAction,
) -> None:
    """Attach ``disable-all-d-class`` to ``concinno features``.

    Wired from :func:`concinno.cli.main._register_features` so the
    command lives at ``concinno features disable-all-d-class``.
    """
    p = features_sub.add_parser(
        "disable-all-d-class",
        help=(
            "Restore 4.x default-off behaviour for the 27 D-class "
            "features promoted to default-on in 5.0.0. Alias for "
            "``concinno features set-profile 4-x-compat``."
        ),
    )
    p.add_argument(
        "--json", dest="json_output", action="store_true",
        help="Emit machine-readable JSON instead of a human summary.",
    )
    p.set_defaults(func=cmd_features_disable_all_d_class)


def register_set_profile(features_sub: argparse._SubParsersAction) -> None:
    """Attach ``set-profile`` to an existing ``concinno features``
    subparser action.

    Wired from :func:`concinno.cli.main._register_features` so the
    command lives at ``concinno features set-profile <name>``.
    """
    p = features_sub.add_parser(
        "set-profile",
        help=(
            "Apply a named feature-toggle profile (strict / permissive / "
            "dev) — bulk-set enabled flags for the 4.0.0 default-off "
            "feature set."
        ),
    )
    p.add_argument(
        "profile_name",
        nargs="?",
        default=None,
        help="Profile name (strict / permissive / dev).",
    )
    p.add_argument(
        "--list", dest="list_profiles", action="store_true",
        help="List available profiles and exit.",
    )
    p.add_argument(
        "--json", dest="json_output", action="store_true",
        help="Emit machine-readable JSON instead of a human summary.",
    )
    p.set_defaults(func=cmd_features_set_profile)
