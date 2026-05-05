"""``concinno features register <name>`` — user-level feature registry CLI.

Added in 2.30.1 per 2.30.0 carry-over. Writes an entry to
``~/.concinno/user_features.json`` (schema v1) without patching the
shipped ``concinno.feature_config.FEATURE_META``. GUI picks it up on
the next 3-second digest tick — the new card appears with a ``source:
"user"`` badge. On name collision with a shipped feature, the shipped
entry wins and the collision is visible via
``concinno.user_features.collision_warnings()``.

Named ``concinno features register`` rather than ``new-feature --kind
feature`` per 2.30.0 red FATAL-3 (command-name overload).
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

__all__ = ["register", "register_features_subcommands"]


_TYPE_CHOICES = ("bool", "int", "float", "str")
_VALID_CATEGORIES = (
    "user_gate", "user_quality", "user_info",
    "hard_gate", "hard_quality", "info", "ux",
)


def _prompt(msg: str, default: str = "") -> str:
    shown = f"{msg} [{default}]: " if default else f"{msg}: "
    try:
        reply = input(shown).strip()
    except EOFError:
        return default
    return reply or default


def _prompt_bool(msg: str, default: bool) -> bool:
    d = "Y/n" if default else "y/N"
    while True:
        reply = _prompt(f"{msg} [{d}]").lower()
        if not reply:
            return default
        if reply in ("y", "yes", "true", "1"):
            return True
        if reply in ("n", "no", "false", "0"):
            return False
        print("  please answer y or n")


def _coerce_default(ptype: str, raw: str) -> Any:
    if ptype == "bool":
        return raw.strip().lower() in ("true", "yes", "1", "y")
    if ptype == "int":
        return int(raw.strip())
    if ptype == "float":
        return float(raw.strip())
    return raw


def _params_interactive() -> dict[str, dict[str, Any]]:
    params: dict[str, dict[str, Any]] = {}
    if not _prompt_bool("Add params?", default=False):
        return params
    while True:
        pname = _prompt("Param name (blank to stop)")
        if not pname:
            break
        if not pname.replace("_", "").isalnum():
            print("  param name must be alphanumeric / underscore")
            continue
        ptype = _prompt(
            f"Type {'/'.join(_TYPE_CHOICES)}",
            default="int",
        )
        if ptype not in _TYPE_CHOICES:
            print(f"  unsupported type: {ptype!r}")
            continue
        default_raw = _prompt("Default value")
        try:
            default = _coerce_default(ptype, default_raw)
        except ValueError as exc:
            print(f"  could not coerce default to {ptype}: {exc}")
            continue
        pdata: dict[str, Any] = {"type": ptype, "default": default}
        if ptype in ("int", "float"):
            if _prompt_bool("Add min/max range?", default=False):
                try:
                    pdata["min"] = _coerce_default(ptype, _prompt("min"))
                    pdata["max"] = _coerce_default(ptype, _prompt("max"))
                except ValueError as exc:
                    print(f"  invalid number: {exc}; skipping range")
        risk_low = _prompt("Risk when low (optional)")
        if risk_low:
            pdata["risk_low"] = risk_low
        risk_high = _prompt("Risk when high (optional)")
        if risk_high:
            pdata["risk_high"] = risk_high
        params[pname] = pdata
    return params


def _cmd_register(args: argparse.Namespace) -> int:
    from concinno.feature_config import FEATURE_META
    from concinno.user_features import save_user_feature

    name = args.name
    if not name.replace("_", "").replace("-", "").isalnum():
        print(
            f"error: name must be alphanumeric / underscore / hyphen, got {name!r}",
            file=sys.stderr,
        )
        return 2

    if name in FEATURE_META:
        print(
            f"warning: {name!r} already exists as a shipped feature. "
            "On the shipped-wins policy, your entry will be shadowed in "
            "the GUI until the shipped entry is removed. Proceed anyway? ",
            file=sys.stderr,
        )
        if not args.force:
            if args.no_interactive:
                print("  use --force to register anyway", file=sys.stderr)
                return 3
            if not _prompt_bool("Continue?", default=False):
                print("aborted")
                return 3

    # Assemble entry
    if args.params_json:
        try:
            entry = json.loads(args.params_json)
        except json.JSONDecodeError as exc:
            print(f"error: --params-json is not valid JSON: {exc}", file=sys.stderr)
            return 2
        if not isinstance(entry, dict):
            print("error: --params-json must decode to a JSON object", file=sys.stderr)
            return 2
    elif args.no_interactive:
        if not args.category or not args.description:
            print(
                "error: --no-interactive requires --category, --description "
                "(and usually --enabled / --ziq-autotunable / --cosmetic)",
                file=sys.stderr,
            )
            return 2
        entry = {
            "category": args.category,
            "description": args.description,
            "enabled": args.enabled if args.enabled is not None else True,
            "ziq_autotunable": bool(args.ziq_autotunable),
            "cosmetic": bool(args.cosmetic),
        }
        if args.description_zh:
            entry["description_zh"] = args.description_zh
    else:
        category = args.category or _prompt(
            f"Category ({'/'.join(_VALID_CATEGORIES)})",
            default="user_gate",
        )
        description = args.description or _prompt(
            "Description (one-line English)"
        )
        while not description:
            print("  description is required")
            description = _prompt("Description")
        description_zh = args.description_zh or _prompt(
            "Description (zh-TW, optional)"
        )
        enabled_default = (
            args.enabled if args.enabled is not None else True
        )
        enabled = _prompt_bool("Enabled by default?", enabled_default)
        ziq_autotunable = _prompt_bool(
            "ZIQ auto-tunable? (online-learner may adjust params)",
            bool(args.ziq_autotunable),
        )
        cosmetic = _prompt_bool(
            "Cosmetic? (UX-only, skipped from ZIQ budget)",
            bool(args.cosmetic),
        )
        params = _params_interactive()
        entry = {
            "category": category,
            "description": description,
            "enabled": enabled,
            "ziq_autotunable": ziq_autotunable,
            "cosmetic": cosmetic,
        }
        if description_zh:
            entry["description_zh"] = description_zh
        if params:
            entry["params"] = params

    if args.dry_run:
        print(f"[dry-run] would save user feature {name!r}:")
        print(json.dumps(entry, indent=2, ensure_ascii=False))
        return 0

    try:
        save_user_feature(name, entry)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4

    print(f"Registered user feature {name!r} in ~/.concinno/user_features.json")
    print(
        "The GUI (if running) refreshes within 3 seconds; the new feature "
        "appears on the Features tab with a 'user' source badge."
    )
    return 0


def _cmd_unregister(args: argparse.Namespace) -> int:
    from concinno.user_features import delete_user_feature

    if delete_user_feature(args.name):
        print(f"Removed user feature {args.name!r} from user_features.json")
        return 0
    print(f"No user feature named {args.name!r} found", file=sys.stderr)
    return 4


def _cmd_list_user(args: argparse.Namespace) -> int:
    from concinno.user_features import load_user_features

    feats = load_user_features()
    if not feats:
        print("(no user features registered)")
        return 0
    for name in sorted(feats):
        entry = feats[name]
        print(
            f"{name:<30} cat={entry['category']:<15} "
            f"enabled={entry['enabled']} cosmetic={entry.get('cosmetic', False)}"
        )
        print(f"    {entry.get('description', '')}")
    return 0


def register_features_subcommands(
    features_sub: argparse._SubParsersAction,
) -> None:
    """Attach ``register`` / ``unregister`` / ``list-user`` subcommands
    to an existing ``concinno features`` subparser action."""
    p_reg = features_sub.add_parser(
        "register",
        help="Register a new user feature in ~/.concinno/user_features.json",
    )
    p_reg.add_argument("name", help="Feature name (snake_case)")
    p_reg.add_argument("--category", default=None)
    p_reg.add_argument("--description", default=None)
    p_reg.add_argument("--description-zh", dest="description_zh", default=None)
    p_reg.add_argument(
        "--enabled",
        type=lambda v: v.lower() in ("1", "true", "yes", "y"),
        default=None,
    )
    p_reg.add_argument(
        "--ziq-autotunable", dest="ziq_autotunable",
        action="store_true",
    )
    p_reg.add_argument(
        "--cosmetic", action="store_true",
    )
    p_reg.add_argument(
        "--params-json", dest="params_json", default=None,
        help=(
            "Pass the full entry as JSON (bypasses interactive params prompt); "
            "useful for agent automation. Example: "
            '--params-json \'{"category": "user_gate", '
            '"description": "...", "enabled": true, "ziq_autotunable": false, '
            '"cosmetic": false, "params": {"threshold": {"type": "int", "default": 10}}}\''
        ),
    )
    p_reg.add_argument("--force", action="store_true")
    p_reg.add_argument(
        "--no-interactive", dest="no_interactive", action="store_true",
    )
    p_reg.add_argument(
        "--dry-run", dest="dry_run", action="store_true",
    )
    p_reg.set_defaults(func=_cmd_register)

    p_unreg = features_sub.add_parser(
        "unregister",
        help="Remove a user feature from user_features.json",
    )
    p_unreg.add_argument("name")
    p_unreg.set_defaults(func=_cmd_unregister)

    p_listu = features_sub.add_parser(
        "list-user",
        help="List only user-registered features",
    )
    p_listu.set_defaults(func=_cmd_list_user)


# Back-compat alias — same signature as other cmd modules (e.g. `publish_cmd.register`)
# but this module attaches under an existing subparser, not the top-level one.
# Main's wiring imports ``register_features_subcommands`` directly.
register = register_features_subcommands
