"""concinno.auto_update.tier1_registry -- SessionStart-time registry refresh.

Tier 1 = cheap discovery refresh; **never** runs ``pip install``.

Flow on SessionStart:

1. Compute digest of currently-installed ``concinno.skills`` +
   ``concinno.features`` entry-points (sorted ``(name, version)``
   tuples → blake2b 16-byte hex).
2. Read cached digest from ``~/.concinno/registry_digest``.
3. **Same** → ``digest_hit=True`` early return (sub-50ms typical).
4. **Different** (or ``force=True``) →
   a. Walk the entry-points via existing
      :func:`concinno.cli.plugins_cmd._gather_features_rows` /
      :func:`_gather_skills_rows` discovery (already battle-tested).
   b. Read existing ``~/.concinno/skills.json`` and
      ``~/.concinno/features_registry.json``; for each fresh
      entry **preserve the user's ``enabled`` field** (R#10
      amendment) before atomic-writing the merged result.
   c. Update the digest cache.
5. **Hard 300ms wall-clock budget** (R#5 amendment); on overrun,
   set ``timed_out=True`` and exit early with whatever was
   completed. SessionStart never blocks on us.

Race protection (R#5): a `~/.concinno/registry_digest.lock` is
acquired before merge_and_write. Two concurrent SessionStart hooks
serialize their writes; the second observes the first's digest and
short-circuits.

Atomic-write (R#5 + W3): ``tempfile + os.rename`` via
:func:`concinno.core.atomic.write_atomic`. Cross-OS — on Windows
``os.replace`` is atomic on the same volume.

This module owns no state outside ``~/.concinno/``; all paths can be
overridden via the ``home`` keyword for tests.
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from concinno.core.atomic import (
    acquire_file_lock,
    read_json,
    release_file_lock,
    write_atomic,
)

__all__ = [
    "RegistryCache",
    "RegistryDigest",
    "RegistryRefreshResult",
    "refresh_tier1_registry",
]

# Hard latency ceiling; commander R#5 fixed it at 300ms.
DEFAULT_TIMEOUT_MS = 300

# Entry-points groups Tier 1 watches.
GROUP_SKILLS = "concinno.skills"
GROUP_FEATURES = "concinno.features"


# ── digest ─────────────────────────────────────────────────


class RegistryDigest:
    """Hash of currently-installed plugin entry-points.

    Input shape: sorted ``(group, ep.name, dist.version)`` tuples
    across the watched groups. Stable across Python releases (sorted)
    and across re-runs (no timestamps).
    """

    @staticmethod
    def _entry_signature(group: str) -> list[tuple[str, str, str]]:
        """Return ``[(group, ep_name, version), ...]`` sorted, never raises."""
        sig: list[tuple[str, str, str]] = []
        try:
            from importlib.metadata import entry_points

            try:
                if sys.version_info >= (3, 10):
                    eps = entry_points(group=group)
                else:
                    all_eps = entry_points()
                    if isinstance(all_eps, dict):
                        eps = all_eps.get(group, [])  # type: ignore[assignment]
                    else:
                        eps = all_eps.select(group=group)  # type: ignore[union-attr]
            except Exception:
                return sig

            for ep in eps:
                ep_name = str(getattr(ep, "name", "") or "")
                version = ""
                dist = getattr(ep, "dist", None)
                if dist is not None:
                    version = str(getattr(dist, "version", "") or "")
                sig.append((group, ep_name, version))
        except Exception:
            return sig
        sig.sort()
        return sig

    @classmethod
    def compute(cls, groups: Optional[Iterable[str]] = None) -> str:
        """Return a stable hex digest for the union of the listed groups.

        Default groups: skills + features. Both must be in the digest
        because either changing invalidates the cached registry files.
        """
        groups = list(groups) if groups is not None else [GROUP_SKILLS, GROUP_FEATURES]
        sig: list[tuple[str, str, str]] = []
        for g in groups:
            sig.extend(cls._entry_signature(g))
        sig.sort()
        # blake2b @ 16 bytes is the same family used elsewhere in the
        # codebase (state_store, agent_gate); 32 hex chars is plenty.
        h = hashlib.blake2b(digest_size=16)
        for tup in sig:
            h.update(("\x1f".join(tup) + "\x1e").encode("utf-8"))
        return h.hexdigest()


# ── cache file (read-modify-write with state preservation) ─


class RegistryCache:
    """Read-modify-write JSON file that preserves user ``enabled`` state.

    The cache files Tier 1 owns are keyed by skill / feature name. Each
    value is a dict; the **only** field the user has authority over is
    ``enabled``. Everything else (description, scope, source, dir...)
    is library-supplied and gets refreshed on every cache miss.

    Lock is acquired for the read-modify-write window so two concurrent
    SessionStart hooks can't tear the file.
    """

    def __init__(self, path: Path, lock_path: Optional[Path] = None) -> None:
        self.path = Path(path)
        self.lock_path = Path(lock_path) if lock_path else Path(str(self.path) + ".lock")

    def load_existing(self) -> dict[str, dict[str, Any]]:
        """Load JSON, returning ``{}`` on missing / malformed."""
        data = read_json(str(self.path), default={})
        if not isinstance(data, dict):
            return {}
        return data

    def merge_and_write(
        self,
        fresh_entries: list[dict[str, Any]],
        *,
        key_field: str = "name",
        preserve_fields: tuple[str, ...] = ("enabled",),
        lock_timeout: float = 2.0,
    ) -> bool:
        """Merge fresh entries with existing user state and atomic-write.

        ``fresh_entries`` is a list of dicts each containing
        ``key_field`` (default ``"name"``). For each entry:

        - If the key already exists in the on-disk cache, copy the
          ``preserve_fields`` from the old entry **only when** they
          are absent in the fresh entry (so a fresh write that
          explicitly carries a value still wins; this avoids
          accidentally clobbering a deliberate operator change).
        - Otherwise the fresh entry lands as-is.

        Returns ``True`` on successful write, ``False`` if the lock
        could not be acquired (caller treats as a soft skip — next
        SessionStart will retry).
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_str = str(self.lock_path)
        if not acquire_file_lock(lock_str, timeout=lock_timeout):
            return False
        try:
            existing = self.load_existing()
            merged: dict[str, dict[str, Any]] = {}
            for entry in fresh_entries:
                if not isinstance(entry, dict):
                    continue
                key = entry.get(key_field)
                if not isinstance(key, str) or not key:
                    continue
                merged_entry = dict(entry)
                old = existing.get(key)
                if isinstance(old, dict):
                    for f in preserve_fields:
                        if f not in merged_entry and f in old:
                            merged_entry[f] = old[f]
                merged[key] = merged_entry
            write_atomic(str(self.path), merged)
            return True
        finally:
            release_file_lock(lock_str)


# ── orchestrator ───────────────────────────────────────────


@dataclass
class RegistryRefreshResult:
    """One-shot result of :func:`refresh_tier1_registry`.

    ``elapsed_ms`` is the wall-clock measurement from entry; useful
    for SessionStart stderr telemetry. ``timed_out`` flips True when
    we hit the budget and bailed early. ``error`` is non-None only on
    catastrophic failure (importlib unavailable etc.) — callers should
    treat any non-None error as a soft skip.
    """

    elapsed_ms: float = 0.0
    digest_hit: bool = False
    skills_count: int = 0
    features_count: int = 0
    timed_out: bool = False
    error: Optional[str] = None
    warnings: list[str] = field(default_factory=list)


def _concinno_home(home: Optional[Path] = None) -> Path:
    if home is not None:
        return Path(home)
    return Path.home() / ".concinno"


def _digest_cache_path(home: Optional[Path] = None) -> Path:
    return _concinno_home(home) / "registry_digest"


def _read_cached_digest(path: Path) -> Optional[str]:
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
        # File holds either bare digest or JSON; accept both for forward
        # compat with future spec where we record the timestamp too.
        if text.startswith("{"):
            try:
                obj = json.loads(text)
                if isinstance(obj, dict):
                    val = obj.get("digest")
                    return str(val) if isinstance(val, str) else None
            except Exception:
                return None
            return None
        return text or None
    except Exception:
        return None


def _write_cached_digest(path: Path, digest: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"digest": digest, "ts": time.time()}
    write_atomic(str(path), payload)


def _gather_safely() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Call the existing plugins_cmd discovery helpers, never raising."""
    warnings: list[str] = []
    features_rows: list[dict[str, Any]] = []
    skills_rows: list[dict[str, Any]] = []
    try:
        from concinno.cli.plugins_cmd import (
            _gather_features_rows,
            _gather_skills_rows,
        )
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"plugins_cmd import failed: {exc}")
        return features_rows, skills_rows, warnings

    try:
        features_rows = _gather_features_rows() or []
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"_gather_features_rows failed: {exc}")

    try:
        skills_rows = _gather_skills_rows() or []
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"_gather_skills_rows failed: {exc}")

    return features_rows, skills_rows, warnings


def _flatten_skills_rows(skills_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn plugin-grouped skill rows into per-skill-name entries.

    Existing ``_gather_skills_rows`` returns one row per *plugin*
    (``{"package": ..., "skills": [...]}``); the on-disk
    ``~/.concinno/skills.json`` is keyed by **skill name** so we
    flatten before merging.
    """
    out: list[dict[str, Any]] = []
    for row in skills_rows:
        if not isinstance(row, dict):
            continue
        pkg = row.get("package", "")
        ep_name = row.get("entry_point", "")
        resolved = row.get("resolved_path")
        for skill_name in row.get("skills", []) or []:
            if not isinstance(skill_name, str) or not skill_name:
                continue
            out.append({
                "name": skill_name,
                "scope": f"plugin:{pkg}" if pkg else "plugin",
                "package": pkg,
                "entry_point": ep_name,
                "resolved_path": resolved,
                # ``enabled`` deliberately omitted — RegistryCache
                # will preserve the on-disk value if present, default
                # to True otherwise (handled below in callers).
            })
    return out


def _flatten_features_rows(features_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten ``{"package": ..., "features": [name, ...]}`` shape."""
    out: list[dict[str, Any]] = []
    for row in features_rows:
        if not isinstance(row, dict):
            continue
        pkg = row.get("package", "")
        ep_name = row.get("entry_point", "")
        for feat_name in row.get("features", []) or []:
            if not isinstance(feat_name, str) or not feat_name:
                continue
            out.append({
                "name": feat_name,
                "package": pkg,
                "entry_point": ep_name,
                "source": "plugin",
            })
    return out


def refresh_tier1_registry(
    *,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    force: bool = False,
    home: Optional[Path] = None,
) -> RegistryRefreshResult:
    """Refresh the tier-1 plugin registry caches under ``~/.concinno/``.

    Idempotent. Cheap on the digest-hit path (≤50ms typical). Bounded
    by ``timeout_ms`` on the cache-miss path; over-budget = fail-soft.

    Args:
        timeout_ms: Hard wall-clock budget. 300ms by default.
        force: Skip the digest short-circuit and rewrite caches.
        home: Override ``~/.concinno`` root (test seam).

    Returns:
        :class:`RegistryRefreshResult`.
    """
    t0 = time.monotonic()
    result = RegistryRefreshResult()
    deadline = t0 + (timeout_ms / 1000.0)

    def _elapsed_ms() -> float:
        return (time.monotonic() - t0) * 1000.0

    def _over_budget() -> bool:
        return time.monotonic() >= deadline

    try:
        cache_dir = _concinno_home(home)
        digest_path = _digest_cache_path(home)

        # 1. compute fresh digest. ``timed_out`` after this point is
        #    a *telemetry signal* (caller may surface a stderr warn),
        #    NOT a reason to skip persisting the result — once we
        #    paid the cost of computing, persisting the digest +
        #    caches lets the next session take the fast path.
        try:
            fresh_digest = RegistryDigest.compute()
        except Exception as exc:  # noqa: BLE001
            result.error = f"digest compute failed: {exc}"
            result.elapsed_ms = _elapsed_ms()
            return result

        if _over_budget():
            result.timed_out = True

        # 2. compare with cached
        cached_digest = _read_cached_digest(digest_path)
        if not force and cached_digest == fresh_digest:
            result.digest_hit = True
            result.elapsed_ms = _elapsed_ms()
            return result

        # 3. cache miss → discover via existing plugins_cmd helpers
        features_rows, skills_rows, warnings = _gather_safely()
        result.warnings.extend(warnings)

        if _over_budget():
            result.timed_out = True

        # 4. flatten + atomic merge-write each cache file
        skills_path = cache_dir / "skills.json"
        features_path = cache_dir / "features_registry.json"

        flat_skills = _flatten_skills_rows(skills_rows)
        flat_features = _flatten_features_rows(features_rows)

        skills_cache = RegistryCache(skills_path)
        features_cache = RegistryCache(features_path)

        if not skills_cache.merge_and_write(flat_skills):
            result.warnings.append(f"skills cache lock contention at {skills_path}")
        if _over_budget():
            result.timed_out = True

        if not features_cache.merge_and_write(flat_features):
            result.warnings.append(
                f"features cache lock contention at {features_path}"
            )
        if _over_budget():
            result.timed_out = True

        # 5. update digest cache last (so a crash mid-write leaves a
        #    stale digest that triggers a redo next session, not a
        #    fresh digest pointing at half-written caches).
        try:
            _write_cached_digest(digest_path, fresh_digest)
        except Exception as exc:  # noqa: BLE001
            result.warnings.append(f"digest cache write failed: {exc}")

        result.skills_count = len(flat_skills)
        result.features_count = len(flat_features)
        result.elapsed_ms = _elapsed_ms()
        return result

    except Exception as exc:  # noqa: BLE001
        result.error = f"unexpected: {exc}"
        result.elapsed_ms = _elapsed_ms()
        return result
