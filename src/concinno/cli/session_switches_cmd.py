"""concinno.cli.session_switches_cmd — Session-start switch summary.

@module session_switches_cmd
@responsibility Emit a one-screen summary of the user's **non-default**
    switch values so the agent can never forget to honor them. Fed into
    SessionStart hook as stderr context (``--format=hook``) or shown to
    humans (``--format=text`` / ``--format=json``).

Design rationale (AI King 2026-04-23 directive, MEMORY #71):

  Switches are only as useful as the agent's ability to notice them.
  When a user sets ``~/.concinno/release_auth.json {"disabled": true}``
  and the agent still asks for the publish string every session, the
  switch is cosmetic. Root cause = agent primacy-bias: rule text
  ("必須 AskUser") is read before switch state is checked.

  Fix: at session start, dump a 1-screen summary of every non-default
  switch into the agent's system context. The agent now reads
  ``release_auth.disabled=True`` in its very first turn and can't
  plausibly "forget" it.

3-layer classification (enforced by 2.16.0):

  L1 Index   — ``~/.claude/rules/switches.md`` (static table, all
                switches + opt-out commands)
  L2 Summary — this CLI output (dynamic, only non-defaults shown)
  L3 Full    — individual ``~/.concinno/<feature>.json`` + module docstrings

Switch config: ``~/.concinno/session_switches.json``::

    {
      "enabled": true,
      "top_n": 10,
      "hook_format_compact": true,
      "extra_switches": [ "custom.feature" ]
    }

@dependencies (stdlib only — argparse, json, os, sys, pathlib)
@exports cmd_session_switches, TOP_SWITCHES, SwitchRow, build_summary
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ── Top-10 critical switches (priority order) ──────────────────────
#
# Each row:
#   (key, default_value_repr, how_to_read_fn_name)
# `default_value_repr` is the STRING representation of the default so a
# user reading the output sees "release_auth.disabled=True (non-default)"
# without us loading the live Config.
#
# The order is the display order in --format=text / --format=hook.

TOP_SWITCHES: list[tuple[str, str, str]] = [
    ("release_auth.disabled", "False", "release_auth_disabled"),
    ("destruction_guard.enabled", "True", "feature_enabled:destruction_guard"),
    ("handoff_mode", "phase", "handoff_mode"),
    ("toast_notify.enabled", "False", "toast_enabled"),
    ("locale", "auto", "locale"),
    ("auto_commit.enabled", "True", "feature_enabled:auto_commit"),
    ("sweep_guard.enabled", "True", "feature_enabled:sweep_guard"),
    ("butterfly_guard.enabled", "True", "feature_enabled:butterfly_guard"),
    ("wiredo.enabled", "True", "feature_enabled:wiredo_guard"),
    ("premise_gate.enabled", "True", "feature_enabled:premise_gate"),
    ("premise_gate.mode", "mode_1+mode_2", "feature_param:premise_gate.mode"),
]


@dataclass
class SwitchRow:
    """One resolved switch row for output."""

    key: str
    value: str
    default: str
    is_default: bool
    source: str
    # Extra informational fields (not emitted in hook-format):
    notes: list[str] = field(default_factory=list)


# ── Resolution helpers (one per switch family) ─────────────────────


def _read_release_auth() -> tuple[str, str]:
    """Return (value_str, source) for release_auth.disabled."""
    try:
        from concinno.release_authorization import load_config

        cfg = load_config()
        return str(cfg.disabled), cfg.source
    except Exception as exc:  # pragma: no cover — defensive
        return f"<err:{type(exc).__name__}>", "error"


def _read_feature_enabled(feature_name: str) -> tuple[str, str]:
    """Return (value_str, source) for a FEATURE_META `enabled` flag."""
    try:
        from concinno.core.config import get_config

        cfg = get_config()
        val = cfg.feature(feature_name, "enabled")
        return str(bool(val)), "feature_config"
    except Exception:
        return "True", "default"


def _read_feature_param(spec: str) -> tuple[str, str]:
    """Return (value_str, source) for `feature.param` format."""
    _, rest = spec.split(":", 1)
    feature_name, param = rest.split(".", 1)
    try:
        from concinno.core.config import get_config

        cfg = get_config()
        val = cfg.feature(feature_name, param)
        return str(val), "feature_config"
    except Exception:
        return "mode_1+mode_2", "default"


def _read_handoff_mode() -> tuple[str, str]:
    """Return (value_str, source) for handoff_mode."""
    try:
        from concinno.handoff_engine import get_handoff_mode

        return str(get_handoff_mode()), "handoff_engine"
    except Exception:
        return "phase", "default"


def _read_toast_enabled() -> tuple[str, str]:
    """Return (value_str, source) for toast_enabled."""
    try:
        from concinno.core.config import get_config

        cfg = get_config()
        return str(bool(cfg.toast_enabled)), "cc_config.json"
    except Exception:
        return "False", "default"


def _read_locale() -> tuple[str, str]:
    """Return (value_str, source) for locale."""
    try:
        from concinno.core.notify import _get_locale

        return str(_get_locale()), "locale chain"
    except Exception:
        return "auto", "default"


_RESOLVERS = {
    "release_auth_disabled": _read_release_auth,
    "handoff_mode": _read_handoff_mode,
    "toast_enabled": _read_toast_enabled,
    "locale": _read_locale,
}


def _resolve(how: str) -> tuple[str, str]:
    """Dispatch a switch key to its reader. Returns (value_str, source)."""
    if how in _RESOLVERS:
        return _RESOLVERS[how]()
    if how.startswith("feature_enabled:"):
        _, feature = how.split(":", 1)
        return _read_feature_enabled(feature)
    if how.startswith("feature_param:"):
        return _read_feature_param(how)
    return "?", "unknown_resolver"


# ── User config for top-N / extra switches ─────────────────────────


def _user_config_path() -> Path:
    return Path.home() / ".concinno" / "session_switches.json"


def _load_user_config() -> dict:
    """Load ~/.concinno/session_switches.json. Never raises."""
    path = _user_config_path()
    if not path.is_file():
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


# ── Core summary build ─────────────────────────────────────────────


def build_summary() -> list[SwitchRow]:
    """Resolve every switch in TOP_SWITCHES (+ user extras) to a SwitchRow."""
    user_cfg = _load_user_config()
    top_n = int(user_cfg.get("top_n", 10)) if user_cfg else 10
    extra = user_cfg.get("extra_switches", []) if user_cfg else []

    rows: list[SwitchRow] = []
    entries = TOP_SWITCHES[: max(1, top_n + 1)]  # allow config to trim
    for key, default_repr, how in entries:
        value, source = _resolve(how)
        is_default = _normalize(value) == _normalize(default_repr)
        rows.append(
            SwitchRow(
                key=key,
                value=value,
                default=default_repr,
                is_default=is_default,
                source=source,
            )
        )

    # Extra user switches (always feature_enabled:<name> format).
    for name in extra:
        if not isinstance(name, str):
            continue
        value, source = _read_feature_enabled(name)
        rows.append(
            SwitchRow(
                key=f"{name}.enabled",
                value=value,
                default="True",
                is_default=_normalize(value) == "true",
                source=source,
                notes=["user-extra"],
            )
        )
    return rows


def _normalize(s: str) -> str:
    """Case/whitespace-insensitive normalisation for default comparison."""
    return s.strip().lower()


def _non_default_rows(rows: list[SwitchRow]) -> list[SwitchRow]:
    return [r for r in rows if not r.is_default]


# ── Output formatters ──────────────────────────────────────────────


def format_text(rows: list[SwitchRow], show_all: bool = False) -> str:
    """Human-readable multi-line format."""
    target = rows if show_all else _non_default_rows(rows)
    if not target:
        return "concinno session switches: all defaults (nothing to show)"

    lines = ["concinno session switches (non-default values only):"]
    for r in target:
        marker = "  " if r.is_default else "⚡"
        lines.append(
            f"{marker} {r.key} = {r.value} "
            f"(default: {r.default}, source: {r.source})"
        )
    return "\n".join(lines)


def format_json(rows: list[SwitchRow], show_all: bool = False) -> str:
    """Structured JSON format, stable schema."""
    target = rows if show_all else _non_default_rows(rows)
    payload = {
        "schema": "concinno.session_switches.v1",
        "switches": [
            {
                "key": r.key,
                "value": r.value,
                "default": r.default,
                "is_default": r.is_default,
                "source": r.source,
            }
            for r in target
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def format_hook(rows: list[SwitchRow]) -> str:
    """One-liner for SessionStart hook stderr injection.

    Only emits non-default rows. Safe to append to stderr — no ANSI,
    no control chars, ``\\n``-terminated. Empty string when all defaults
    (caller may filter to avoid blank lines).
    """
    target = _non_default_rows(rows)
    if not target:
        return ""
    parts = [f"{r.key}={r.value}" for r in target]
    return "concinno: active switches — " + ", ".join(parts)


# ── CLI entry ──────────────────────────────────────────────────────


def cmd_session_switches(args: argparse.Namespace) -> None:
    """`concinno session-switches` entry point."""
    # Feature-level on/off respect (new FEATURE_META entry).
    if not _feature_enabled():
        # Graceful off: say nothing to stdout so hook wire-up doesn't
        # produce a blank line. Exit 0 so shell pipelines don't break.
        return

    fmt = getattr(args, "format", "text") or "text"
    show_all = bool(getattr(args, "all", False))
    rows = build_summary()

    if fmt == "hook":
        line = format_hook(rows)
        if line:
            # stderr is the contract for SessionStart — appears in
            # agent's system context without polluting stdout pipelines.
            print(line, file=sys.stderr)
        return
    if fmt == "json":
        print(format_json(rows, show_all=show_all))
        return
    # default: text
    print(format_text(rows, show_all=show_all))


def _feature_enabled() -> bool:
    """Check FEATURE_META session_switches.enabled."""
    # Env opt-out beats config for debugging.
    env = os.environ.get("CONCINNO_SESSION_SWITCHES_ENABLED")
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    try:
        from concinno.core.config import get_config

        return bool(get_config().feature("session_switches", "enabled"))
    except Exception:
        return True


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the `session-switches` subcommand on an argparse parent."""
    p = subparsers.add_parser(
        "session-switches",
        help="Emit active switch summary for SessionStart hook / humans",
    )
    p.add_argument(
        "--format",
        choices=["text", "json", "hook"],
        default="text",
        help="Output format (default: text)",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="Show defaults too (default: non-default only)",
    )
    p.set_defaults(func=cmd_session_switches)


__all__ = [
    "TOP_SWITCHES",
    "SwitchRow",
    "build_summary",
    "format_text",
    "format_json",
    "format_hook",
    "cmd_session_switches",
    "register",
]


if __name__ == "__main__":  # pragma: no cover — CLI smoke path
    _parser = argparse.ArgumentParser(prog="concinno session-switches")
    _sub = _parser.add_subparsers(dest="command")
    register(_sub)
    _args = _parser.parse_args(["session-switches"] + sys.argv[1:])
    cmd_session_switches(_args)
