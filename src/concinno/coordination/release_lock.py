"""Atomic per-package release lock with stale detection.

Prevents the PyPI 400 already-exists race that hit Concinno 4.2.1 in
session ``cc_c08e_0113``: the markdown ``RELEASE_COORDINATION.md::Active``
section is self-validation only — two sessions can read it
simultaneously, both write ``Active: <session>``, neither sees the
other's write before the ``twine upload`` runs, and the second upload
crashes with PyPI 400.

This module replaces the markdown self-validation with an OS-level file
lock plus a JSON content schema that survives reboots, captures the
holder's identity, and auto-revokes after a configurable TTL so a
crashed session cannot wedge releases forever.

Cross-platform: reuses :class:`concinno.coordination._os_lock.OSFileLock`
which already wraps ``msvcrt.locking`` on Windows and ``fcntl.flock``
on POSIX.

The pre-check helper :func:`pypi_version_taken` queries the public PyPI
JSON endpoint so the wrapper layer (next ship cycle) can short-circuit
before invoking ``twine`` — turning a 400 race into a fast local check.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ._os_lock import OSFileLock

DEFAULT_LOCK_DIR = Path.home() / ".concinno" / "release_locks"
DEFAULT_TTL_MINUTES = 30
_OS_LOCK_TIMEOUT = 5.0


def _ttl_seconds() -> int:
    """Resolve the stale-lock TTL from env or the default."""
    raw = os.environ.get("CONCINNO_RELEASE_LOCK_TTL_MIN", "").strip()
    if raw:
        try:
            minutes = int(raw)
            if minutes > 0:
                return minutes * 60
        except ValueError:
            pass
    return DEFAULT_TTL_MINUTES * 60


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: str) -> Optional[datetime]:
    try:
        # Python 3.11+ handles trailing 'Z' but we only ever emit '+00:00'.
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


class ReleaseLock:
    """Atomic per-package release lock with stale detection.

    Lock file lives at ``<lock_dir>/<pkg>.lock`` (default
    ``~/.concinno/release_locks/<pkg>.lock``).  The lock file content is
    JSON ``{pkg, version, holder_session, host, acquired_at}``; the
    advisory lock guarding read/write of that JSON is held on a sibling
    sentinel file ``<pkg>.lock.lockfile`` so atomic ``os.replace`` of
    the JSON cannot orphan the held fd.

    Stale lock (older than ``CONCINNO_RELEASE_LOCK_TTL_MIN`` minutes,
    default 30) is auto-revoked on the next acquire attempt — protects
    against crashed sessions wedging releases forever.
    """

    def __init__(self, lock_dir: Optional[Path] = None) -> None:
        self.lock_dir = Path(lock_dir) if lock_dir is not None else DEFAULT_LOCK_DIR

    # ── path helpers ──────────────────────────────────────────────

    def _lock_path(self, pkg: str) -> Path:
        return self.lock_dir / f"{pkg}.lock"

    def _sentinel_path(self, pkg: str) -> Path:
        return self.lock_dir / f"{pkg}.lock.lockfile"

    def _os_lock(self, pkg: str) -> OSFileLock:
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        return OSFileLock(str(self._sentinel_path(pkg)), timeout=_OS_LOCK_TIMEOUT)

    # ── content read / write (must be called under self._os_lock) ──

    def _read_content(self, pkg: str) -> Optional[dict]:
        path = self._lock_path(pkg)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        return None

    def _write_content(self, pkg: str, content: dict) -> None:
        path = self._lock_path(pkg)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(content, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def _is_stale(self, content: dict) -> bool:
        ts = _parse_iso(str(content.get("acquired_at", "")))
        if ts is None:
            # Unparseable timestamp — treat as stale rather than wedge.
            return True
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        return age > _ttl_seconds()

    # ── public API ────────────────────────────────────────────────

    def acquire(
        self,
        pkg: str,
        version: str,
        session: str,
        host: Optional[str] = None,
    ) -> bool:
        """Try to acquire the lock for *pkg* + *version*.

        Returns ``True`` on success, ``False`` if held by another live
        session.  Stale locks (older than the TTL) are silently
        revoked.  Re-acquire by the same *session* is idempotent and
        always succeeds (refreshes ``acquired_at``).
        """
        host = host or socket.gethostname()
        with self._os_lock(pkg):
            existing = self._read_content(pkg)
            if existing is not None:
                holder = str(existing.get("holder_session", ""))
                if holder == session:
                    # Same session re-acquiring — refresh timestamp.
                    pass
                elif self._is_stale(existing):
                    # Different holder but TTL exceeded — take over.
                    pass
                else:
                    return False
            self._write_content(
                pkg,
                {
                    "pkg": pkg,
                    "version": version,
                    "holder_session": session,
                    "host": host,
                    "acquired_at": _now_iso(),
                },
            )
            return True

    def release(self, pkg: str) -> None:
        """Release the lock for *pkg*.  Idempotent — no-op if absent."""
        with self._os_lock(pkg):
            path = self._lock_path(pkg)
            try:
                path.unlink()
            except FileNotFoundError:
                return
            except OSError:
                # Permission error or similar — surface nothing; caller
                # cannot meaningfully recover.  Stale TTL will reclaim.
                return

    def check(self, pkg: str) -> Optional[dict]:
        """Return current lock content for *pkg*, or ``None`` if free.

        A stale lock returns ``None`` so callers see "free" — same view
        an :meth:`acquire` would take. Use :meth:`raw` if you need to
        introspect a stale lock without revoking it.
        """
        with self._os_lock(pkg):
            content = self._read_content(pkg)
            if content is None:
                return None
            if self._is_stale(content):
                return None
            return content

    def raw(self, pkg: str) -> Optional[dict]:
        """Return the lock content verbatim (does not honour staleness)."""
        with self._os_lock(pkg):
            return self._read_content(pkg)

    def list_active(self) -> list[dict]:
        """List all currently held (non-stale) locks across the lock dir."""
        if not self.lock_dir.exists():
            return []
        out: list[dict] = []
        for path in sorted(self.lock_dir.glob("*.lock")):
            pkg = path.stem
            content = self.check(pkg)
            if content is not None:
                out.append(content)
        return out


# ── PyPI version pre-check helper ─────────────────────────────────


def pypi_version_taken(pkg: str, ver: str, timeout: float = 5.0) -> bool:
    """Return True if *pkg* version *ver* already exists on PyPI.

    Hits ``https://pypi.org/pypi/<pkg>/<ver>/json``: 200 = taken,
    404 = available.  Network errors are propagated so the caller can
    decide whether to fail-open or fail-closed (we don't silently say
    "available" — that would re-introduce the 400 race).
    """
    url = f"https://pypi.org/pypi/{pkg}/{ver}/json"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise


__all__ = [
    "ReleaseLock",
    "DEFAULT_LOCK_DIR",
    "DEFAULT_TTL_MINUTES",
    "pypi_version_taken",
]
