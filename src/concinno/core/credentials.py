"""concinno.core.credentials — unified secret / OAuth token store.

@module core.credentials
@responsibility Shared credential lookup for Concinno sub-packages
    (e.g. ``concinno-skills-google``) and daemon-hosted tools. Four-source
    precedence (default < ~/.concinno/credentials.json < env var <
    runtime set()), ``$ref: env:VAR`` indirection for secrets that
    shouldn't sit in plaintext on disk, and thread-safe in-process
    overrides.
@dependencies concinno.core.atomic.read_json (soft; falls back to stdlib
    json if atomic helper fails on malformed disk)
@exports CredentialStore, get_default_store

This module deliberately stays leaf-level: no imports from any other
``concinno.*`` submodule beyond ``core.atomic`` (which is itself a leaf).
That keeps it safe to import from ``concinno.daemon`` and from plugin
packages that only depend on ``concinno>=2.15``.

Precedence (later overrides earlier):
    1. Built-in default (``None`` unless caller passes ``default=``)
    2. ``~/.concinno/credentials.json``
    3. Env var ``CONCINNO_CRED_<UPPER_KEY>``
    4. Runtime :meth:`CredentialStore.set`

A value of the form ``{"$ref": "env:SOME_VAR"}`` inside the JSON file is
dereferenced at read time; this lets a user check the JSON file into
``$HOME/.concinno`` without baking the secret into it.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger("concinno.core.credentials")


def _default_config_path() -> Path:
    """Return the standard credentials JSON path.

    Always rooted at ``Path.home() / ".concinno"``. Never hard-codes any
    personal username/path (BoundaryGuard rule in
    ``projects/concinno/CLAUDE.md``).
    """
    return Path.home() / ".concinno" / "credentials.json"


def _env_key_for(key: str) -> str:
    """Map a credential key to its env-var form.

    ``google_oauth_token`` → ``CONCINNO_CRED_GOOGLE_OAUTH_TOKEN``.
    Non-alphanumeric chars become ``_`` to avoid invalid env names.
    """
    sanitized = "".join(c if c.isalnum() else "_" for c in key).upper()
    return f"CONCINNO_CRED_{sanitized}"


def _resolve_ref(value: Any) -> Any:
    """Dereference ``{"$ref": "env:VAR"}`` to ``os.environ[VAR]`` or None.

    Unknown ``$ref`` schemes (e.g. ``file:...``) are returned untouched
    rather than raising; future schemes can be added without breaking
    existing JSON files.
    """
    if not isinstance(value, dict):
        return value
    ref = value.get("$ref")
    if not isinstance(ref, str):
        return value
    if ref.startswith("env:"):
        var_name = ref[len("env:") :]
        return os.environ.get(var_name)
    return value


class CredentialStore:
    """Thread-safe credential lookup with JSON + env + runtime overrides.

    Usage::

        store = CredentialStore()
        token = store.get("google_oauth_token")
        store.set("api_key", "sk-...")
        store.delete("api_key")

    The store is cheap to construct — the JSON file is only read once
    per instance (cached after first successful load), and env lookups
    are O(1). Callers who want a module-level singleton can use
    :func:`get_default_store`.
    """

    def __init__(self, config_path: Path | None = None) -> None:
        self._config_path = config_path if config_path is not None else _default_config_path()
        self._runtime: dict[str, Any] = {}
        self._file_cache: dict[str, Any] | None = None
        self._lock = threading.RLock()

    # ── Disk ──────────────────────────────────────────────────────────

    def _load_file(self) -> dict[str, Any]:
        """Read the JSON file once and cache. Returns ``{}`` on any error."""
        with self._lock:
            if self._file_cache is not None:
                return self._file_cache
            if not self._config_path.exists():
                self._file_cache = {}
                return self._file_cache
            try:
                with self._config_path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    logger.warning(
                        "credentials: %s is not a JSON object; ignoring",
                        self._config_path,
                    )
                    self._file_cache = {}
                    return self._file_cache
                self._file_cache = data
                return self._file_cache
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning(
                    "credentials: failed to read %s: %s",
                    self._config_path,
                    exc,
                )
                self._file_cache = {}
                return self._file_cache

    def reload(self) -> None:
        """Invalidate the file cache. Next :meth:`get` re-reads from disk."""
        with self._lock:
            self._file_cache = None

    # ── Accessors ─────────────────────────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        """Return the value for ``key`` following the 4-source precedence.

        Args:
            key: credential key (case-sensitive; env form upper-cases it).
            default: returned when key not found in any source.

        Returns:
            The resolved value, or ``default`` if missing.
        """
        with self._lock:
            # 4. Runtime override wins.
            if key in self._runtime:
                return self._runtime[key]
            # 3. Env var.
            env_val = os.environ.get(_env_key_for(key))
            if env_val is not None:
                return env_val
            # 2. Config file (with $ref dereference).
            file_data = self._load_file()
            if key in file_data:
                resolved = _resolve_ref(file_data[key])
                if resolved is not None:
                    return resolved
            # 1. Default.
            return default

    def set(self, key: str, value: Any) -> None:
        """Set a runtime override. Does not write to disk."""
        with self._lock:
            self._runtime[key] = value

    def delete(self, key: str) -> None:
        """Remove a runtime override. Disk/env sources are untouched.

        Silent no-op when the key wasn't set at runtime — mirrors
        ``dict.pop(k, None)`` semantics.
        """
        with self._lock:
            self._runtime.pop(key, None)

    def keys(self) -> list[str]:
        """Return the union of runtime + file keys (env keys not enumerated)."""
        with self._lock:
            file_data = self._load_file()
            return sorted(set(self._runtime.keys()) | set(file_data.keys()))

    def has(self, key: str) -> bool:
        """Return True if ``get(key)`` would return a non-None value."""
        sentinel = object()
        return self.get(key, default=sentinel) is not sentinel


# ── Module-level default ──────────────────────────────────────────────


_default_store: CredentialStore | None = None
_default_lock = threading.Lock()


def get_default_store() -> CredentialStore:
    """Return (and lazily construct) a process-wide default store.

    Sub-packages that want to share the same config file across a single
    process call this instead of instantiating their own store. Tests
    should construct a fresh :class:`CredentialStore` with an explicit
    ``config_path`` rather than mutating the default.
    """
    global _default_store
    with _default_lock:
        if _default_store is None:
            _default_store = CredentialStore()
        return _default_store


__all__ = [
    "CredentialStore",
    "get_default_store",
]
