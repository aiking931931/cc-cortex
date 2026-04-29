"""Tiny PyPI JSON client with disk-cached responses.

Wraps :mod:`urllib.request` (no extra runtime dep) and a one-hour
file-system cache at ``~/.concinno/marketplace_cache.json``. Used by
:func:`concinno.marketplace.discovery.list_available_pypi`.

Cache shape::

    {
      "fetched_at": <unix-ts>,
      "packages": [{"name": str, "version": str, "summary": str}, ...]
    }

The cache is intentionally simple — corruption recovers by treating
the file as missing and re-fetching. We never trust the cache file's
shape blindly; readers validate types before using.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

logger = logging.getLogger("concinno.marketplace.pypi_client")


# 1-hour cache lifetime — fresh enough to surface new releases within
# an hour, dampens load on PyPI when the operator opens / closes the
# GUI repeatedly.
DEFAULT_CACHE_TTL_SEC = 3600

# Hard timeout per HTTP request — better to fail fast and serve cache
# than to block the GUI indefinitely on a slow PyPI mirror.
HTTP_TIMEOUT_SEC = 10

# Curated allowlist used when the index endpoint returns nothing
# parseable — we never auto-discover arbitrary names from PyPI search,
# only confirm versions for the known curated set.
KNOWN_FIRST_PARTY = (
    "concinno-skills-memory",
    "concinno-skills-memoria",
    "concinno-skills-session-search",
    "concinno-skills-ziq",
)


class PyPIUnreachableError(RuntimeError):
    """Raised when both the live fetch and the cache fail."""


def _default_cache_path() -> Path:
    return Path.home() / ".concinno" / "marketplace_cache.json"


class PyPIClient:
    """Cache-aware PyPI JSON fetcher.

    All network I/O is funnelled through this class so test code can
    inject a fake transport via the ``transport`` kwarg without
    monkey-patching ``urllib``.
    """

    def __init__(
        self,
        *,
        cache_path: Path | None = None,
        cache_ttl_sec: int = DEFAULT_CACHE_TTL_SEC,
        transport: Any | None = None,
        clock: Any | None = None,
    ) -> None:
        self._cache_path = cache_path or _default_cache_path()
        self._cache_ttl = int(cache_ttl_sec)
        # ``transport`` is a callable taking ``(url: str) -> dict``.
        # When None, the real urllib path is used.
        self._transport = transport or self._default_transport
        self._clock = clock or time.time

    # ── Public API ────────────────────────────────────────

    def list_concinno_skills_packages(
        self, *, force_refresh: bool = False,
    ) -> list[dict[str, Any]]:
        """Return list of ``{"name", "version", "summary"}`` dicts.

        Honours the cache unless ``force_refresh`` is True.

        Raises:
            PyPIUnreachableError: When live fetch fails AND no usable
                cache is available.
        """
        if not force_refresh:
            cached = self._read_cache()
            if cached is not None:
                return cached

        try:
            packages = self._fetch_live()
        except PyPIUnreachableError:
            cached = self._read_cache(ignore_ttl=True)
            if cached is not None:
                return cached
            raise

        self._write_cache(packages)
        return packages

    def cache_age_seconds(self) -> int:
        """Return wall-clock seconds since the cache was last written.

        Returns ``0`` when no cache exists yet (keeps the API total —
        the GUI shows "no cache" in that branch by reading the
        ``pypi_reachable`` flag separately).
        """
        try:
            stat = self._cache_path.stat()
        except OSError:
            return 0
        return max(0, int(self._clock() - stat.st_mtime))

    def invalidate_cache(self) -> None:
        """Best-effort delete of the cache file. Used by the
        ``/api/skills/marketplace/refresh`` endpoint.
        """
        try:
            self._cache_path.unlink()
        except OSError:
            pass

    # ── Internals ─────────────────────────────────────────

    def _read_cache(self, *, ignore_ttl: bool = False) -> list[dict[str, Any]] | None:
        if not self._cache_path.is_file():
            return None
        try:
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        fetched_at = data.get("fetched_at")
        if not isinstance(fetched_at, (int, float)):
            return None
        if not ignore_ttl:
            age = self._clock() - float(fetched_at)
            if age > self._cache_ttl:
                return None
        packages = data.get("packages")
        if not isinstance(packages, list):
            return None
        # Defensive copy + shape validation.
        out: list[dict[str, Any]] = []
        for entry in packages:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not isinstance(name, str):
                continue
            out.append(
                {
                    "name": name,
                    "version": entry.get("version"),
                    "summary": entry.get("summary", "") or "",
                }
            )
        return out

    def _write_cache(self, packages: list[dict[str, Any]]) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "fetched_at": int(self._clock()),
            "packages": packages,
        }
        tmp = self._cache_path.with_suffix(".tmp")
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self._cache_path)
        except OSError as exc:
            logger.warning("marketplace cache write failed: %s", exc)

    def _fetch_live(self) -> list[dict[str, Any]]:
        """Fetch the curated first-party set's metadata via PyPI JSON.

        We deliberately do NOT hit ``/simple/`` for arbitrary discovery —
        the marketplace surface is curated to the ``concinno-skills-*``
        first-party namespace. This keeps trust boundaries narrow:
        ``pip install`` runs only when the operator explicitly clicks
        on a known name we returned.
        """
        out: list[dict[str, Any]] = []
        any_success = False
        for name in KNOWN_FIRST_PARTY:
            url = f"https://pypi.org/pypi/{name}/json"
            try:
                payload = self._transport(url)
            except PyPIUnreachableError:
                continue
            info = payload.get("info") if isinstance(payload, dict) else None
            if not isinstance(info, dict):
                continue
            version = info.get("version")
            summary = info.get("summary", "") or ""
            out.append(
                {
                    "name": name,
                    "version": version,
                    "summary": summary,
                }
            )
            any_success = True
        if not any_success:
            raise PyPIUnreachableError(
                "All PyPI fetches failed for the first-party set"
            )
        return out

    @staticmethod
    def _default_transport(url: str) -> dict[str, Any]:
        try:
            req = urllib_request.Request(
                url,
                headers={"User-Agent": "concinno-marketplace/1.0"},
            )
            with urllib_request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
                body = resp.read()
        except (urllib_error.URLError, TimeoutError, OSError) as exc:
            raise PyPIUnreachableError(f"{url}: {exc}") from exc
        try:
            return json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise PyPIUnreachableError(f"{url}: malformed JSON") from exc
