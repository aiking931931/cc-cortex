"""concinno.config_preservation — User-config preservation across upgrades.

@module config_preservation
@responsibility Guarantee that ``pip install --upgrade concinno`` never
    resets user-set values in ``~/.concinno/*.json`` or ``~/.claude/*.json``.
    Provides a ``preserve_user_values`` merge primitive + ``safe_write_config``
    atomic writer + ``assert_preservation_invariant`` regression helper.

Design rationale (AI King 2026-04-23 directive):

  Problem: a user who types ``{"disabled": true}`` into
  ``~/.concinno/release_auth.json`` once should never have that flipped
  back to ``false`` because a new Concinno version shipped with a
  different default. Switches become a lie if the user can't trust them
  across upgrades.

  Solution:
    - User config files live in ``~/.concinno/`` (per-feature JSON) or
      OS keyring. Package install never writes to those paths.
    - New features that need a default ship the default in code
      (``FEATURE_META`` or ``_DEFAULTS`` dict in ``core/config.py``),
      not in a file. Files are pure user-override layer.
    - ``preserve_user_values`` merges new keys from ``new_defaults`` into
      an existing user file **without** overwriting any key the user
      already set. Deep-merge for nested dicts.
    - ``safe_write_config`` is atomic (temp + ``os.replace``) with a
      rotating backup (.bak.N up to 3 generations). Safe on Windows +
      POSIX.
    - ``assert_preservation_invariant`` is for tests only: given
      ``before`` / ``after`` dicts and a set of keys the caller is
      allowed to touch, raises if any other key drifted.

Corrupted-file policy (fail-safe, not fail-open):
    - If JSON parse fails → do NOT overwrite the user's file. Emit a
      stderr warning. In-memory code falls back to ``FEATURE_META``
      default. User gets to decide whether to fix or delete the
      corrupted file manually.

@dependencies (stdlib only — json, os, sys, pathlib, copy)
@exports preserve_user_values, safe_write_config, read_user_config,
    assert_preservation_invariant, ConfigPreservationError
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable


class ConfigPreservationError(RuntimeError):
    """Raised when a preservation invariant is violated."""


# ── Read / Write primitives ────────────────────────────────────────


def read_user_config(path: Path) -> tuple[dict, str | None]:
    """Read a user JSON config file.

    Args:
        path: Absolute or home-relative path to the config file.

    Returns:
        ``(data, warning)``. ``data`` is an empty dict on any failure
        (missing / malformed / non-object). ``warning`` is ``None`` on
        clean read, otherwise a human-readable string. Never raises.

    Corrupted-file policy: returns ``({}, "<path> is malformed JSON — "
    "using in-memory defaults; user file untouched")``. The file itself
    is NOT touched — user decides whether to fix or delete.
    """
    path = Path(path)
    if not path.is_file():
        return {}, None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError:
        return {}, (
            f"{path} is malformed JSON — using in-memory defaults; "
            f"user file untouched"
        )
    except OSError as exc:
        return {}, f"{path} could not be read ({exc}); using defaults"
    if not isinstance(data, dict):
        return {}, f"{path} is not a JSON object; using defaults"
    return data, None


def safe_write_config(
    path: Path,
    data: dict,
    *,
    rotate_backups: int = 3,
) -> None:
    """Atomically write ``data`` to ``path``.

    - Creates parent dir if missing.
    - Rotates existing ``path`` into ``path.bak.1`` / ``.bak.2`` / …
      up to ``rotate_backups`` generations (oldest is deleted).
    - Writes to ``path.tmp.<pid>`` first, then ``os.replace`` → atomic
      on POSIX + Windows ≥Vista.
    - On Windows, ``os.replace`` atomically overwrites the destination,
      so concurrent readers see either the old or new version, never a
      half-written file.

    Raises ``OSError`` on disk / permission failure — not silently
    ignored (caller decides whether to retry or log).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.is_file() and rotate_backups > 0:
        _rotate_backup_generations(path, rotate_backups)

    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def _rotate_backup_generations(path: Path, rotate_backups: int) -> None:
    """Shift path → path.bak.1, .bak.1 → .bak.2, etc. up to rotate_backups."""
    for n in range(rotate_backups, 0, -1):
        dst = path.with_suffix(path.suffix + f".bak.{n}")
        if n == 1:
            src = path
        else:
            src = path.with_suffix(path.suffix + f".bak.{n - 1}")
        if not src.exists():
            continue
        _replace_file(src, dst, copy_mode=(n == 1))


def _replace_file(src: Path, dst: Path, *, copy_mode: bool) -> None:
    """Move src→dst, unless copy_mode (which copies bytes and keeps src)."""
    if dst.exists():
        try:
            dst.unlink()
        except OSError:
            return
    try:
        if copy_mode:
            dst.write_bytes(src.read_bytes())
        else:
            src.rename(dst)
    except OSError:
        pass


# ── Preserve-merge primitive ───────────────────────────────────────


def preserve_user_values(
    existing: dict,
    new_defaults: dict,
) -> dict:
    """Merge ``new_defaults`` into ``existing`` without overwriting user keys.

    Rules:
        1. Every key present in ``existing`` keeps its ``existing`` value.
        2. Every key in ``new_defaults`` but NOT in ``existing`` is added.
        3. Nested dicts recurse: ``existing[k]`` wins at each level, but
           new nested keys from ``new_defaults[k]`` are added.
        4. Lists are treated as scalars (user owns the list wholesale).
        5. Input dicts are never mutated — result is a fresh dict.

    Example::

        >>> existing = {"disabled": True, "mode": "askuser"}
        >>> new_defaults = {"disabled": False, "mode": "string_match",
        ...                 "new_knob": 42}
        >>> preserve_user_values(existing, new_defaults)
        {'disabled': True, 'mode': 'askuser', 'new_knob': 42}
    """
    if not isinstance(existing, dict):
        return copy.deepcopy(new_defaults) if isinstance(new_defaults, dict) else {}
    if not isinstance(new_defaults, dict):
        return copy.deepcopy(existing)

    merged: dict[str, Any] = {}
    # Start from new_defaults → add any new keys.
    for k, v in new_defaults.items():
        if k in existing:
            existing_v = existing[k]
            if isinstance(existing_v, dict) and isinstance(v, dict):
                merged[k] = preserve_user_values(existing_v, v)
            else:
                # User scalar/list wins.
                merged[k] = copy.deepcopy(existing_v)
        else:
            merged[k] = copy.deepcopy(v)

    # Keep any keys the user has that aren't in new_defaults (forward-compat).
    for k, v in existing.items():
        if k not in merged:
            merged[k] = copy.deepcopy(v)

    return merged


def merge_and_write(
    path: Path,
    new_defaults: dict,
    *,
    rotate_backups: int = 3,
) -> tuple[dict, str | None]:
    """Read ``path``, merge with ``new_defaults``, write atomically.

    On malformed JSON: does NOT write (corrupted-file policy). Returns
    ``(in_memory_defaults, warning)`` so the caller can operate in
    memory without clobbering the user's file.

    Empty-file / missing-file: writes ``new_defaults`` as-is on first
    access only if the caller requests (current behaviour: never touch
    the filesystem unless merge succeeds and something actually changed).
    """
    existing, warning = read_user_config(path)
    if warning and not existing:
        # Corrupted — return defaults in memory, do NOT write.
        return copy.deepcopy(new_defaults), warning

    merged = preserve_user_values(existing, new_defaults)

    # Only write if the merged result differs from what's on disk.
    if merged != existing:
        safe_write_config(path, merged, rotate_backups=rotate_backups)

    return merged, warning


# ── Invariant check (test helper) ──────────────────────────────────


def assert_preservation_invariant(
    before: dict,
    after: dict,
    touched_keys: Iterable[str] = (),
) -> None:
    """Raise ``ConfigPreservationError`` if any untouched key drifted.

    Used in regression tests: ``before`` is the user config before an
    upgrade, ``after`` is after a ``preserve_user_values`` merge. Any
    key in ``before`` that is not in ``touched_keys`` must have the
    exact same value in ``after``.

    Nested dicts: checks recursively. A key path like ``"a.b.c"`` in
    ``touched_keys`` exempts only that leaf.

    Args:
        before: User config snapshot before upgrade.
        after: User config snapshot after upgrade.
        touched_keys: Dotted key paths the upgrade is allowed to change.
            Leaves outside this set must match ``before`` exactly.

    Raises:
        ConfigPreservationError: on any unauthorized drift, with a
            message listing every offending path.
    """
    touched = set(touched_keys)
    drifted: list[str] = []

    def _walk(a: dict, b: dict, prefix: str = "") -> None:
        for k, av in a.items():
            path = f"{prefix}.{k}" if prefix else k
            if path in touched:
                continue
            if k not in b:
                drifted.append(f"{path}: removed (was {av!r})")
                continue
            bv = b[k]
            if isinstance(av, dict) and isinstance(bv, dict):
                _walk(av, bv, path)
            elif av != bv:
                drifted.append(f"{path}: {av!r} → {bv!r}")

    _walk(before, after)

    if drifted:
        raise ConfigPreservationError(
            "User config drifted across upgrade:\n  " + "\n  ".join(drifted)
        )


# ── Convenience: warn to stderr helper ──────────────────────────────


def warn_corruption(warning: str) -> None:
    """Emit a corruption warning to stderr. Separate function so hooks
    can silence / redirect without touching read_user_config."""
    if warning:
        print(f"concinno: {warning}", file=sys.stderr)


__all__ = [
    "ConfigPreservationError",
    "read_user_config",
    "safe_write_config",
    "preserve_user_values",
    "merge_and_write",
    "assert_preservation_invariant",
    "warn_corruption",
]
