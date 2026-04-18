"""concinno.config — Layered config loader.

@module config
@responsibility Single source of truth for user-facing settings (mode, locale,
    auto_compact, memory_file_enabled). Four-layer override (env > project >
    user > default). Ship defaults locked per MEMORY #59 to general+en so
    PyPI downloads behave like normal LLM users expect. AI King's personal
    zh-TW+handoff preference lives in user config, never in source.

Priority (highest wins):
  1. Env var: CONCINNO_<KEY>=<value>
  2. Project: <cwd>/.concinno/config.json
  3. User: ~/.concinno/config.json
  4. Package default: _DEFAULT_CONFIG (immutable at ship)

Design rules:
  * `_DEFAULT_CONFIG` is frozen by test invariants — do NOT change to zh-TW
    or handoff. Those are user preferences, not ship defaults.
  * Malformed JSON / unknown keys / type mismatches log to stderr and fall
    back to the default; we never raise on `load()` because tooling depends
    on it returning a valid config always.
  * `set_user` / `set_project` validate strictly and raise ValueError so bad
    writes are caught early.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

# ── Ship defaults (DO NOT MODIFY — covered by invariant tests) ───

# SHIP DEFAULTS — locked per MEMORY #59. Do NOT change to zh-TW / handoff.
# These are for every anonymous PyPI downloader. AI King's personal
# preference (zh-TW + handoff mode) belongs in ~/.concinno/config.json.
_DEFAULT_CONFIG: dict[str, Any] = {
    "mode": "general",  # general | handoff
    "locale": "en",  # en | zh-TW | ja | ko | fr | de | es
    "auto_compact": True,  # auto-compact on ctx threshold
    "memory_file_enabled": True,
}

_VALID_MODES: frozenset[str] = frozenset({"general", "handoff"})
_VALID_LOCALES: frozenset[str] = frozenset(
    {"en", "zh-TW", "ja", "ko", "fr", "de", "es"},
)
_BOOL_KEYS: frozenset[str] = frozenset({"auto_compact", "memory_file_enabled"})
_SCHEMA_VERSION: int = 1

# Env var prefix and keys that are read through env overrides.
_ENV_PREFIX = "CONCINNO_"
_ENV_KEYS: frozenset[str] = frozenset(_DEFAULT_CONFIG.keys())

# Legacy env var map — preserve back-compat for i18n / earlier releases.
_LEGACY_ENV_MAP: dict[str, str] = {
    "locale": "CONCINNO_LOCALE",
}


# ── Paths ────────────────────────────────────────────────────────


def user_config_path() -> Path:
    """Return ~/.concinno/config.json (not created until set_user)."""
    return Path.home() / ".concinno" / "config.json"


def project_config_path(cwd: Path | None = None) -> Path:
    """Return <cwd>/.concinno/config.json (not created until set_project)."""
    base = Path(cwd) if cwd is not None else Path.cwd()
    return base / ".concinno" / "config.json"


# ── Validation ───────────────────────────────────────────────────


def validate(key: str, value: Any) -> None:
    """Raise ValueError if (key, value) is not a legal config pair.

    Args:
        key: One of the recognised config keys.
        value: Candidate value.

    Raises:
        ValueError: If ``key`` is unknown, or ``value`` is the wrong type
            or outside the allowed set (mode/locale).
    """
    if key not in _DEFAULT_CONFIG:
        raise ValueError(
            f"Unknown config key: {key!r}. "
            f"Valid keys: {sorted(_DEFAULT_CONFIG.keys())}",
        )
    if key == "mode":
        if value not in _VALID_MODES:
            raise ValueError(
                f"Invalid mode: {value!r}. Must be one of {sorted(_VALID_MODES)}",
            )
        return
    if key == "locale":
        if value not in _VALID_LOCALES:
            raise ValueError(
                f"Invalid locale: {value!r}. Must be one of {sorted(_VALID_LOCALES)}",
            )
        return
    if key in _BOOL_KEYS:
        if not isinstance(value, bool):
            raise ValueError(
                f"Invalid {key}: expected bool, got {type(value).__name__}",
            )
        return


# ── File IO helpers ──────────────────────────────────────────────


def _warn(msg: str) -> None:
    """Write a warning line to stderr. Never raise."""
    try:
        print(f"[concinno.config] {msg}", file=sys.stderr)
    except Exception:
        pass


def _read_json_file(path: Path) -> dict[str, Any]:
    """Best-effort JSON read. Returns empty dict on any failure + warns."""
    if not path.is_file():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        _warn(f"malformed config at {path}: {exc}; using defaults")
        return {}
    if not isinstance(data, dict):
        _warn(f"config at {path} is not a JSON object; using defaults")
        return {}
    return data


def _sanitize_layer(data: dict[str, Any], source: str) -> dict[str, Any]:
    """Filter a raw dict against the known schema.

    Unknown keys → warn + drop. Type mismatches / invalid values → warn + drop.
    ``$schema_version`` is silently dropped (reserved metadata).
    """
    clean: dict[str, Any] = {}
    for k, v in data.items():
        if k == "$schema_version":
            continue
        if k not in _DEFAULT_CONFIG:
            _warn(f"unknown key {k!r} in {source}; ignored")
            continue
        try:
            validate(k, v)
        except ValueError as exc:
            _warn(f"invalid value for {k!r} in {source}: {exc}; using default")
            continue
        clean[k] = v
    return clean


def _env_layer() -> dict[str, Any]:
    """Collect config values from environment variables.

    Supports both ``CONCINNO_<KEY>`` and legacy per-key names (e.g.
    ``CONCINNO_LOCALE`` is the same as the generic form, so layer order is
    fine — we only add legacy aliases when the canonical form is absent).
    """
    out: dict[str, Any] = {}
    for k in _ENV_KEYS:
        canonical = f"{_ENV_PREFIX}{k.upper()}"
        raw: str | None = os.environ.get(canonical)
        if raw is None and k in _LEGACY_ENV_MAP:
            raw = os.environ.get(_LEGACY_ENV_MAP[k])
        if raw is None:
            continue
        parsed = _parse_env_value(k, raw)
        if parsed is not None:
            try:
                validate(k, parsed)
            except ValueError as exc:
                _warn(f"invalid env value for {k!r}: {exc}; ignored")
                continue
            out[k] = parsed
    return out


def _parse_env_value(key: str, raw: str) -> Any:
    """Parse env string to the right type. Booleans accept 1/0/true/false."""
    if key in _BOOL_KEYS:
        low = raw.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
        _warn(f"invalid bool for env {key}: {raw!r}; ignored")
        return None
    return raw.strip()


# ── Core API ─────────────────────────────────────────────────────


def load(cwd: Path | None = None) -> dict[str, Any]:
    """Return merged config (layers 1-4). Always a valid dict; never raises.

    Args:
        cwd: Project directory for layer 2. Defaults to ``Path.cwd()``.

    Returns:
        Deep copy of default, overlaid by user → project → env. Malformed or
        unknown entries are dropped with a stderr warning.
    """
    merged: dict[str, Any] = copy.deepcopy(_DEFAULT_CONFIG)

    # Layer 3: user
    user_raw = _read_json_file(user_config_path())
    merged.update(_sanitize_layer(user_raw, "user config"))

    # Layer 2: project
    project_raw = _read_json_file(project_config_path(cwd))
    merged.update(_sanitize_layer(project_raw, "project config"))

    # Layer 1: env (wins)
    merged.update(_env_layer())

    return merged


def get(key: str, cwd: Path | None = None) -> Any:
    """Return the effective value for ``key`` (post-merge)."""
    if key not in _DEFAULT_CONFIG:
        raise ValueError(
            f"Unknown config key: {key!r}. "
            f"Valid keys: {sorted(_DEFAULT_CONFIG.keys())}",
        )
    return load(cwd=cwd)[key]


def _write_layer(path: Path, key: str, value: Any) -> None:
    """Merge-write (key, value) into a single-layer JSON file."""
    validate(key, value)
    existing: dict[str, Any] = {}
    if path.is_file():
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    existing = loaded
        except (json.JSONDecodeError, OSError) as exc:
            _warn(f"replacing malformed config at {path}: {exc}")
            existing = {}

    existing["$schema_version"] = _SCHEMA_VERSION
    existing[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False, sort_keys=False)
        f.write("\n")


def set_user(key: str, value: Any) -> None:
    """Write (key, value) to ``~/.concinno/config.json`` (user layer)."""
    _write_layer(user_config_path(), key, value)


def set_project(key: str, value: Any, cwd: Path | None = None) -> None:
    """Write (key, value) to ``<cwd>/.concinno/config.json`` (project layer)."""
    _write_layer(project_config_path(cwd), key, value)


def _unset_layer(path: Path, key: str) -> bool:
    """Remove ``key`` from a layer file. Returns True if anything was removed."""
    if key not in _DEFAULT_CONFIG:
        raise ValueError(f"Unknown config key: {key!r}")
    if not path.is_file():
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(data, dict) or key not in data:
        return False
    data.pop(key)
    # Drop file entirely when only metadata remains.
    meaningful = {k: v for k, v in data.items() if k != "$schema_version"}
    if not meaningful:
        path.unlink(missing_ok=True)
        return True
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=False)
        f.write("\n")
    return True


def unset_user(key: str) -> bool:
    """Remove ``key`` from the user config. Returns True on change."""
    return _unset_layer(user_config_path(), key)


def unset_project(key: str, cwd: Path | None = None) -> bool:
    """Remove ``key`` from the project config. Returns True on change."""
    return _unset_layer(project_config_path(cwd), key)


def sources(cwd: Path | None = None) -> dict[str, str]:
    """Report which layer supplied each effective value. For CLI display."""
    env = _env_layer()
    user = _sanitize_layer(_read_json_file(user_config_path()), "user config")
    project = _sanitize_layer(
        _read_json_file(project_config_path(cwd)), "project config",
    )
    result: dict[str, str] = {}
    for k in _DEFAULT_CONFIG:
        if k in env:
            result[k] = "env"
        elif k in project:
            result[k] = "project"
        elif k in user:
            result[k] = "user"
        else:
            result[k] = "default"
    return result


def default_config() -> dict[str, Any]:
    """Return a deep copy of the ship defaults (for tests / tooling)."""
    return copy.deepcopy(_DEFAULT_CONFIG)


__all__ = [
    "default_config",
    "get",
    "load",
    "project_config_path",
    "set_project",
    "set_user",
    "sources",
    "unset_project",
    "unset_user",
    "user_config_path",
    "validate",
]
