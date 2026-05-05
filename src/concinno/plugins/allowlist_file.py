"""concinno.plugins.allowlist_file -- Persistent allowlist file
management for the ``concinno plugins allowlist`` CLI.

2.32.0 introduces ``~/.concinno/plugins_allowlist.json`` as a
**CLI-managed** persistent store. Runtime gating in
:func:`concinno.plugins.plugin_allowlist` is **not** wired to this
file -- it remains env-var-only (2.31.0 behaviour). Users who want
runtime enforcement of the file run ``concinno plugins allowlist
export-env`` to print a shell ``export`` line and source it into
their shell rc.

Separation rationale (per 2.32.0 commander adjudication FATAL-2 +
FATAL-3): mixing env + file allowlist at runtime creates an
intersection-vs-union debate that is ambiguous in shared environments
(CI runners vs workstations). Keeping file as CLI-scope avoids the
ambiguity entirely.

Schema (schema_version = 0, **UNSTABLE** -- may evolve until 3.0.0)::

    {
      "schema_version": 0,
      "allowlist": [
        "concinno-skills-google",
        "concinno-skills-obsidian"
      ],
      "updated_at": "2026-04-24T18:30:00Z",
      "note": "optional operator annotation"
    }

Atomicity: reads + writes go through :func:`tempfile.mkstemp` +
:func:`os.replace` so concurrent readers never see a half-written
file. Writes include an ``mtime_ns`` re-check before replace to
detect read-modify-write races with a concurrent GUI / second CLI
invocation (one retry, then stderr warn + abort).
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("concinno.plugins.allowlist_file")

# UNSTABLE schema -- see module docstring + 2.32.0 CHANGELOG.
SCHEMA_VERSION = 0


def _allowlist_path() -> Path:
    """Return the absolute path to the allowlist file."""
    return Path.home() / ".concinno" / "plugins_allowlist.json"


def _empty_doc() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "allowlist": [],
        "updated_at": None,
        "note": "",
    }


def _read_doc_raw() -> tuple[dict[str, Any], int]:
    """Read the doc + its ``mtime_ns``. Fail-closed on malformed JSON.

    Returns an empty doc + ``mtime_ns=0`` when the file is absent
    (first call on a fresh install).
    """
    path = _allowlist_path()
    if not path.is_file():
        return _empty_doc(), 0
    try:
        mtime_ns = path.stat().st_mtime_ns
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"concinno: warning -- plugins_allowlist.json malformed "
            f"({exc}); treating as empty",
            file=sys.stderr,
        )
        return _empty_doc(), 0

    if not isinstance(data, dict):
        print(
            "concinno: warning -- plugins_allowlist.json root is not "
            "an object; treating as empty",
            file=sys.stderr,
        )
        return _empty_doc(), 0

    # Forward-compat: accept unknown schema_version with stderr
    # warning; coerce fields to safe defaults.
    sv = data.get("schema_version", SCHEMA_VERSION)
    if not isinstance(sv, int):
        print(
            f"concinno: warning -- plugins_allowlist.json "
            f"schema_version is non-int ({sv!r}); treating as empty",
            file=sys.stderr,
        )
        return _empty_doc(), mtime_ns

    allow = data.get("allowlist", [])
    if not isinstance(allow, list):
        allow = []
    cleaned: list[str] = []
    for item in allow:
        if isinstance(item, str) and item.strip():
            cleaned.append(item.strip())
    data["allowlist"] = cleaned
    data.setdefault("updated_at", None)
    data.setdefault("note", "")
    return data, mtime_ns


def load_allowlist_file() -> list[str]:
    """Return the current allowlist package-name list."""
    doc, _ = _read_doc_raw()
    return list(doc["allowlist"])


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` to ``path`` via tempfile + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=".plugins_allowlist.",
        suffix=".json",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_doc(doc: dict[str, Any], *, expected_mtime_ns: int) -> bool:
    """Write ``doc`` after re-checking ``expected_mtime_ns``.

    Returns True on successful write. Returns False when a race is
    detected (file was mutated by another process between our read
    and this write attempt).
    """
    path = _allowlist_path()
    current_mtime = path.stat().st_mtime_ns if path.is_file() else 0
    if current_mtime != expected_mtime_ns:
        return False
    doc["schema_version"] = SCHEMA_VERSION
    doc["updated_at"] = datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    _atomic_write(path, doc)
    return True


def _mutate_allowlist(
    mutator, *, what: str,
) -> tuple[bool, bool]:
    """Read / mutate / write with one retry on mtime-race.

    ``mutator`` receives the current allowlist list and a mutable
    doc; returns True when the mutation changed state (the caller
    distinguishes "already present" from "newly added").

    Returns ``(success, changed)``. ``success=False`` means both
    attempts raced and the file was left alone.
    """
    for attempt in (1, 2):
        doc, mtime_ns = _read_doc_raw()
        changed = mutator(doc)
        if not changed:
            return True, False
        ok = _write_doc(doc, expected_mtime_ns=mtime_ns)
        if ok:
            return True, True
        logger.warning(
            "plugins_allowlist.json race detected on %s (attempt %d); "
            "retrying",
            what,
            attempt,
        )
    print(
        f"concinno: warning -- plugins_allowlist.json race could not "
        f"be resolved after retry on {what}; no write performed",
        file=sys.stderr,
    )
    return False, False


def add_to_allowlist(pkg: str, *, note: str = "") -> tuple[bool, bool]:
    """Idempotent add. Returns ``(success, newly_added)``.

    ``pkg`` is stored as-given; caller is responsible for name
    normalisation if they want dash-vs-underscore insensitive
    behaviour (the matching side, :func:`concinno.plugins._is_pkg_allowed`,
    already handles both variants on lookup).
    """
    pkg = pkg.strip()
    if not pkg:
        raise ValueError("package name cannot be empty")

    def mutator(doc: dict[str, Any]) -> bool:
        if pkg in doc["allowlist"]:
            return False
        doc["allowlist"].append(pkg)
        doc["allowlist"].sort()
        if note:
            doc["note"] = note
        return True

    return _mutate_allowlist(mutator, what=f"add {pkg!r}")


def remove_from_allowlist(pkg: str) -> tuple[bool, bool]:
    """Idempotent remove. Returns ``(success, removed)``."""
    pkg = pkg.strip()
    if not pkg:
        raise ValueError("package name cannot be empty")

    def mutator(doc: dict[str, Any]) -> bool:
        if pkg not in doc["allowlist"]:
            return False
        doc["allowlist"].remove(pkg)
        return True

    return _mutate_allowlist(mutator, what=f"remove {pkg!r}")


def get_note() -> str:
    """Return the operator annotation (empty string if unset)."""
    doc, _ = _read_doc_raw()
    return doc.get("note", "") or ""


def get_updated_at() -> str | None:
    """Return the ISO-8601 ``updated_at`` timestamp, or None."""
    doc, _ = _read_doc_raw()
    return doc.get("updated_at")
