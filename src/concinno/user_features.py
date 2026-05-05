"""concinno.user_features — user-level feature registry.

Lets users declare switch-able features **without patching the shipped
``FEATURE_META``** in ``feature_config.py``. Added in 2.30.1 per the
2.30.0 carry-over plan (red/blue CBUA verdict).

File: ``~/.concinno/user_features.json``

Schema (v1)::

    {
      "schema_version": 1,
      "features": {
        "<user_feature_name>": {
          "category": "user_gate" | "user_quality" | "user_info" | ...,
          "description": "One-line English",
          "description_zh": "一行中文",      # optional
          "enabled": true,
          "ziq_autotunable": false,
          "cosmetic": false,
          "params": {
            "<param>": {
              "type": "bool|int|float|str",
              "default": ...,
              "min": ...,           # optional, numeric only
              "max": ...,           # optional, numeric only
              "recommended": ...,   # optional
              "risk_low": "...",    # optional
              "risk_high": "...",   # optional
            }
          }
        }
      }
    }

Design constraints:

- **Fail-closed**: malformed JSON / unknown schema version -> empty
  dict + warning to stderr. Never raises into the GUI / CLI layer.
- **Atomic write**: write to temp + rename so concurrent reader never
  sees a half-written file.
- **Shipped wins on collision**: a user-registered name that clashes
  with ``FEATURE_META`` is kept in the file (for future resolution)
  but ``list_features()`` surfaces the shipped entry and records a
  warning accessible via ``collision_warnings()``.
- **schema_version discipline**: bump the version when a
  non-backwards-compat field change happens. Migration lives in
  :func:`_migrate`.

@exports load_user_features, save_user_feature, delete_user_feature,
         user_features_path, collision_warnings, CURRENT_SCHEMA_VERSION
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "collision_warnings",
    "delete_user_feature",
    "load_user_features",
    "save_user_feature",
    "user_features_path",
    "validate_user_feature",
]

CURRENT_SCHEMA_VERSION = 1

_COLLISION_WARNINGS: list[str] = []


def user_features_path() -> Path:
    """Return ``~/.concinno/user_features.json``."""
    return Path.home() / ".concinno" / "user_features.json"


def _warn(msg: str) -> None:
    print(f"concinno.user_features: {msg}", file=sys.stderr)


def _default_doc() -> dict[str, Any]:
    return {"schema_version": CURRENT_SCHEMA_VERSION, "features": {}}


def _migrate(doc: dict[str, Any]) -> dict[str, Any]:
    """Apply forward migrations. Today there is only v1; future
    versions add cases here without changing callers."""
    version = doc.get("schema_version")
    if version is None or version == CURRENT_SCHEMA_VERSION:
        return doc
    if version > CURRENT_SCHEMA_VERSION:
        _warn(
            f"user_features.json schema_version={version} is newer "
            f"than this Concinno build supports ({CURRENT_SCHEMA_VERSION}); "
            f"treating as empty to avoid data loss."
        )
        return _default_doc()
    # v0 -> v1 migration: v0 had no "features" wrapper
    if version == 0:
        _warn("migrating user_features.json from schema v0 to v1")
        return {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "features": {
                k: v for k, v in doc.items() if k != "schema_version"
            },
        }
    _warn(f"unknown schema_version={version}, treating as empty")
    return _default_doc()


def load_user_features() -> dict[str, dict[str, Any]]:
    """Read and validate ``user_features.json``. Returns the ``features``
    sub-dict. Fail-closed on any error."""
    path = user_features_path()
    if not path.is_file():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
        doc = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        _warn(f"failed to read {path}: {exc}; treating as empty")
        return {}
    if not isinstance(doc, dict):
        _warn(f"{path} is not a JSON object; treating as empty")
        return {}
    doc = _migrate(doc)
    feats = doc.get("features", {})
    if not isinstance(feats, dict):
        _warn(f"{path} 'features' is not an object; treating as empty")
        return {}
    # Light validation: strip entries that fail shape check.
    clean: dict[str, dict[str, Any]] = {}
    for name, entry in feats.items():
        ok, reason = validate_user_feature(name, entry)
        if ok:
            clean[name] = entry
        else:
            _warn(f"skipping user_features['{name}']: {reason}")
    return clean


def validate_user_feature(
    name: str, entry: Any,
) -> tuple[bool, str]:
    """Minimal shape check. Returns (ok, reason_if_not_ok)."""
    if not isinstance(name, str) or not name:
        return False, "name must be non-empty string"
    if not name.replace("_", "").replace("-", "").isalnum():
        return False, "name must be alphanumeric / underscore / hyphen"
    if not isinstance(entry, dict):
        return False, "entry must be object"
    required = ("category", "description", "enabled",
                "ziq_autotunable", "cosmetic")
    for key in required:
        if key not in entry:
            return False, f"missing required field '{key}'"
    if not isinstance(entry["category"], str):
        return False, "'category' must be string"
    if not isinstance(entry["description"], str):
        return False, "'description' must be string"
    for bool_key in ("enabled", "ziq_autotunable", "cosmetic"):
        if not isinstance(entry[bool_key], bool):
            return False, f"'{bool_key}' must be bool"
    params = entry.get("params", {})
    if params and not isinstance(params, dict):
        return False, "'params' must be object when present"
    for pname, pmeta in params.items():
        if not isinstance(pmeta, dict):
            return False, f"param '{pname}' must be object"
        if "type" not in pmeta:
            return False, f"param '{pname}' missing 'type'"
        if pmeta["type"] not in ("bool", "int", "float", "str"):
            return False, f"param '{pname}' has unsupported 'type'={pmeta['type']!r}"
    return True, ""


def save_user_feature(name: str, entry: dict[str, Any]) -> None:
    """Register (or update) a user feature. Atomic write."""
    ok, reason = validate_user_feature(name, entry)
    if not ok:
        raise ValueError(f"invalid user feature '{name}': {reason}")
    path = user_features_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = _read_raw_doc(path)
    doc.setdefault("features", {})[name] = entry
    doc["schema_version"] = CURRENT_SCHEMA_VERSION
    _atomic_write(path, doc)


def delete_user_feature(name: str) -> bool:
    """Remove a user feature from the registry. Returns True if removed."""
    path = user_features_path()
    if not path.is_file():
        return False
    doc = _read_raw_doc(path)
    feats = doc.get("features", {})
    if name not in feats:
        return False
    del feats[name]
    _atomic_write(path, doc)
    return True


def _read_raw_doc(path: Path) -> dict[str, Any]:
    """Raw read without validation — for save/delete paths that need
    to preserve unknown keys on round-trip."""
    if not path.is_file():
        return _default_doc()
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(doc, dict):
            return _migrate(doc)
    except (OSError, json.JSONDecodeError) as exc:
        _warn(f"corrupt {path} will be replaced: {exc}")
    return _default_doc()


def _atomic_write(path: Path, doc: dict[str, Any]) -> None:
    """Write ``doc`` JSON to ``path`` via tmp + replace — so concurrent
    readers never see a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # NamedTemporaryFile on Windows holds a lock; use a sibling tmp.
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(doc, fp, indent=2, ensure_ascii=False, sort_keys=True)
            fp.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        # Best-effort cleanup of the tmp file; re-raise.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def collision_warnings() -> list[str]:
    """Return a snapshot of the most recent collision warnings built
    during the last ``feature_config.list_features()`` merge. Used by
    the GUI to render a visible badge instead of hoping the user reads
    stderr."""
    return list(_COLLISION_WARNINGS)


def record_collision(name: str, reason: str) -> None:
    """Internal — called by ``feature_config`` merge path when a
    user feature is shadowed by a shipped entry."""
    msg = f"collision: user feature '{name}' shadowed by shipped entry ({reason})"
    if msg not in _COLLISION_WARNINGS:
        _COLLISION_WARNINGS.append(msg)
    _warn(msg)


def clear_collision_warnings() -> None:
    """Clear the collision warning buffer. Called at the start of
    every ``list_features()`` merge."""
    _COLLISION_WARNINGS.clear()
