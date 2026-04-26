"""concinno.state_client — Thin client for the Sancio state_store.

@module state_client
@responsibility Project-scoped, schema-flexible key-value storage that
    survives CC session boundaries. Implements the v1 client portion of
    the Sancio state_store spec
    (``_AI_BRAIN/05_Planning/sancio_state_store_spec_2026-04-26.md``).

The Sancio runtime daemon owning this storage is not yet shipped, so the
v1 backend default is **file** (one JSON per project under
``~/.sancio/state/``). The fallback chain — Sancio HTTP daemon → file →
legacy ``~/.concinno/state/<project>.json`` — is API-stable: the daemon
upgrade in v2 only changes the transport, not the JSON shape on disk.

Design contract
---------------

- **Public reads never raise.** ``get`` / ``list_keys`` / ``snapshot`` /
  ``health`` degrade to ``None`` / empty results and log to stderr; they
  must be safe to call from a hook hot path.
- **Writes do raise** on persistence failure. Silent write loss is more
  dangerous than a loud failure for the caller (e.g. a post-commit hook
  recording ``last_commit_hash``).
- **Project name canonicalization** (per spec §10 Q6): lowercase, strip
  whitespace + dots, replace path separators / spaces with underscores.
  Diverging input is logged once to stderr.
- **TTL semantics** (per spec §10 Q5): values are stored as
  ``{"value": ..., "expires_at": iso8601 | null, "set_at": iso8601}``.
  Lazy expiration — a ``get`` past ``expires_at`` returns ``None`` and
  removes the key on next write; no background thread.

Configuration sources (later overrides earlier; per Concinno L0 rule #6
6-source chain)::

    1. Defaults (backend=auto, port=8530, dir=~/.sancio/state)
    2. ~/.concinno/state_client.json
    3. Env vars: CONCINNO_STATE_BACKEND, SANCIO_STATE_DIR,
       SANCIO_STATE_STORE_PORT

Backend identifiers
-------------------

- ``"sancio_http"`` — Sancio daemon reachable on
  ``http://127.0.0.1:<port>/v1/state``
- ``"file"`` — local JSON file under ``~/.sancio/state/<project>.json``
- ``"legacy_file"`` — read-only fallback to ``~/.concinno/state/...``
  for projects that wrote under the old path
- ``"unavailable"`` — every backend failed; reads return None, writes
  raise

@dependencies (stdlib only — json, os, sys, time, threading,
    contextlib, dataclasses, datetime, pathlib, typing, urllib.request,
    urllib.error). Optional: ``filelock`` for cross-process safety;
    falls back to ``os.O_EXCL`` advisory lock when absent.
@exports StateClient, BackendUnavailable, get, set, delete, list_keys,
    snapshot, health, canonicalize_project, default_client,
    reset_default_client
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib import error as urllib_error
from urllib import request as urllib_request

# ── Constants & defaults ─────────────────────────────────────────────

DEFAULT_SANCIO_PORT = 8530  # picked to avoid persona-api 8500 / llama-cpp 9000
SANCIO_HTTP_TIMEOUT = 0.25  # seconds — fail fast, hot-path-safe
WRITE_RETRY_DELAYS = (0.05, 0.10, 0.20)  # 3 retries with backoff
LOCK_TIMEOUT = 0.5  # seconds — degrade rather than hang

_BACKEND_VALUES = frozenset({"auto", "sancio_http", "file", "legacy_file"})


# ── Errors ───────────────────────────────────────────────────────────


class BackendUnavailable(RuntimeError):
    """Raised by ``set`` / ``delete`` when no backend can persist writes."""


# ── Project name canonicalization (per spec §10 Q6) ──────────────────

_NORMALIZE_RE = re.compile(r"[\s/\\]+")


def canonicalize_project(name: str) -> str:
    """Return the canonical project slug for ``name``.

    - Lowercased, leading / trailing whitespace + dots stripped
    - ``/``, ``\\``, and any whitespace run collapsed to a single ``_``
    - Empty / non-string input → ``"_unknown"`` (logged)

    Examples::

        canonicalize_project("Sancio")         -> "sancio"
        canonicalize_project(" Arb Bot ")       -> "arb_bot"
        canonicalize_project("foo/bar")         -> "foo_bar"
        canonicalize_project(".concinno.")      -> "concinno"
        canonicalize_project("")                -> "_unknown"
    """
    if not isinstance(name, str):
        return "_unknown"
    stripped = name.strip().strip(".").strip()
    if not stripped:
        return "_unknown"
    lowered = stripped.lower()
    return _NORMALIZE_RE.sub("_", lowered)


# ── Config loader ────────────────────────────────────────────────────


@dataclass(frozen=True)
class StateClientConfig:
    """Resolved state_client config for the current process."""

    preferred_backend: str = "auto"
    sancio_port: int = DEFAULT_SANCIO_PORT
    state_dir: Path = field(
        default_factory=lambda: Path.home() / ".sancio" / "state",
    )
    legacy_dir: Path = field(
        default_factory=lambda: Path.home() / ".concinno" / "state",
    )
    source: str = "default"
    warnings: tuple[str, ...] = field(default_factory=tuple)


def _read_config_file(path: Path) -> tuple[dict, Optional[str]]:
    """Read the user config JSON. Fail-open; never raise."""
    try:
        if not path.is_file():
            return {}, None
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return {}, f"{path} is not a JSON object; ignoring"
        return data, None
    except json.JSONDecodeError:
        return {}, f"{path} is malformed JSON; ignoring"
    except OSError:
        return {}, None


def _coerce_backend(raw: object) -> Optional[str]:
    if isinstance(raw, str):
        norm = raw.strip().lower().replace("-", "_")
        if norm in _BACKEND_VALUES:
            return norm
    return None


def _coerce_port(raw: object) -> Optional[int]:
    try:
        port = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if 1 <= port <= 65535:
        return port
    return None


def load_config(path: Optional[Path] = None) -> StateClientConfig:
    """Load config from file + env. File first, env overrides."""
    cfg_path = path or (Path.home() / ".concinno" / "state_client.json")
    warnings: list[str] = []
    sources: list[str] = []

    backend = "auto"
    port = DEFAULT_SANCIO_PORT
    state_dir = Path.home() / ".sancio" / "state"
    legacy_dir = Path.home() / ".concinno" / "state"

    file_data, file_warning = _read_config_file(cfg_path)
    if file_warning:
        warnings.append(file_warning)
    if file_data:
        if "preferred_backend" in file_data:
            coerced = _coerce_backend(file_data.get("preferred_backend"))
            if coerced is not None:
                backend = coerced
            else:
                warnings.append(
                    f"state_client.json preferred_backend="
                    f"{file_data.get('preferred_backend')!r} invalid; "
                    f"using default 'auto'",
                )
        if "sancio_port" in file_data:
            coerced_p = _coerce_port(file_data.get("sancio_port"))
            if coerced_p is not None:
                port = coerced_p
        if isinstance(file_data.get("state_dir"), str):
            state_dir = Path(file_data["state_dir"]).expanduser()
        sources.append("file")

    env_backend = os.environ.get("CONCINNO_STATE_BACKEND")
    if env_backend is not None:
        coerced = _coerce_backend(env_backend)
        if coerced is not None:
            backend = coerced
            sources.append("env:backend")
        else:
            warnings.append(
                f"CONCINNO_STATE_BACKEND={env_backend!r} invalid; "
                f"keeping {backend}",
            )

    env_port = os.environ.get("SANCIO_STATE_STORE_PORT")
    if env_port is not None:
        coerced_p = _coerce_port(env_port)
        if coerced_p is not None:
            port = coerced_p
            sources.append("env:port")
        else:
            warnings.append(
                f"SANCIO_STATE_STORE_PORT={env_port!r} invalid; "
                f"keeping {port}",
            )

    env_dir = os.environ.get("SANCIO_STATE_DIR")
    if env_dir:
        state_dir = Path(env_dir).expanduser()
        sources.append("env:dir")

    return StateClientConfig(
        preferred_backend=backend,
        sancio_port=port,
        state_dir=state_dir,
        legacy_dir=legacy_dir,
        source="+".join(sources) if sources else "default",
        warnings=tuple(warnings),
    )


# ── stderr helper ────────────────────────────────────────────────────


def _stderr(msg: str) -> None:
    """Write a single line to stderr; never raise."""
    try:
        sys.stderr.write(f"state_client: {msg}\n")
        sys.stderr.flush()
    except Exception:
        pass


# ── Cross-process file lock (filelock if available, O_EXCL fallback) ─


@contextmanager
def _file_lock(path: Path, timeout: float = LOCK_TIMEOUT):
    """Cross-process advisory lock for a state file.

    Uses ``filelock`` when available (best behavior on Windows + POSIX).
    Falls back to ``os.O_EXCL`` create + timed retry. On timeout the
    block runs anyway in degraded mode (logged) — matches spec §6.4
    "Lock contention timeout (>500ms) → degrade".
    """
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from filelock import FileLock, Timeout  # type: ignore[import-not-found]

        lock = FileLock(str(lock_path), timeout=timeout)
        try:
            with lock:
                yield True
            return
        except Timeout:
            _stderr(f"lock timeout {lock_path}; proceeding degraded")
            yield False
            return
    except ImportError:
        pass

    # Fallback: O_EXCL polling
    deadline = time.monotonic() + timeout
    fd: Optional[int] = None
    while time.monotonic() < deadline:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            time.sleep(0.01)
    if fd is None:
        _stderr(f"lock timeout {lock_path}; proceeding degraded")
        yield False
        return
    try:
        os.close(fd)
        yield True
    finally:
        try:
            os.unlink(str(lock_path))
        except OSError:
            pass


# ── Atomic JSON write helper ─────────────────────────────────────────


def _write_json_atomic(path: Path, data: Any) -> None:
    """tmp+rename atomic write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False, sort_keys=True)
    os.replace(str(tmp), str(path))


def _read_json(path: Path) -> dict:
    """Read a JSON dict; return ``{}`` on any failure (logged)."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
        _stderr(f"{path} is not a JSON object; treating as empty")
        return {}
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        _stderr(f"read failed {path}: {exc}; treating as empty")
        return {}


# ── TTL helpers ──────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _wrap_value(value: Any, ttl_seconds: Optional[int]) -> dict:
    """Pack value + expiry envelope per spec §10 Q5."""
    expires_at: Optional[str] = None
    if ttl_seconds is not None and ttl_seconds > 0:
        expires_at = datetime.fromtimestamp(
            time.time() + ttl_seconds, tz=timezone.utc,
        ).isoformat()
    return {
        "value": value,
        "expires_at": expires_at,
        "set_at": _now_iso(),
    }


def _is_expired(envelope: Any) -> bool:
    if not isinstance(envelope, dict):
        return False
    expires_at = envelope.get("expires_at")
    if not expires_at:
        return False
    try:
        # Accept both ``Z`` suffix and offset; isoformat output is offset
        norm = expires_at.replace("Z", "+00:00")
        deadline = datetime.fromisoformat(norm)
    except (TypeError, ValueError):
        return False
    return datetime.now(timezone.utc) >= deadline


def _unwrap(envelope: Any) -> Any:
    """Return the user value, or the raw payload if the legacy file has no
    envelope (back-compat with hand-edited / pre-spec state files)."""
    if isinstance(envelope, dict) and "value" in envelope and (
        "expires_at" in envelope or "set_at" in envelope
    ):
        return envelope["value"]
    return envelope


# ── Sancio HTTP backend ──────────────────────────────────────────────


class _SancioHTTP:
    """Tiny HTTP client for the future Sancio state daemon.

    No request library dependency: stdlib ``urllib.request`` only.
    Every method returns ``None`` / raises ``BackendUnavailable``
    rather than propagating ``urllib`` errors so the fallback chain in
    ``StateClient`` can route around an unhealthy daemon transparently.
    """

    def __init__(self, port: int, timeout: float = SANCIO_HTTP_TIMEOUT):
        self._base = f"http://127.0.0.1:{port}/v1/state"
        self._timeout = timeout

    def _req(
        self, method: str, path: str, payload: Optional[dict] = None,
    ) -> Optional[dict]:
        url = f"{self._base}{path}"
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib_request.Request(
            url, data=body, headers=headers, method=method,
        )
        try:
            with urllib_request.urlopen(req, timeout=self._timeout) as resp:
                if resp.status >= 500:
                    raise BackendUnavailable(f"http {resp.status}")
                if resp.status == 404:
                    return None
                raw = resp.read()
                if not raw:
                    return {}
                parsed = json.loads(raw)
                return parsed if isinstance(parsed, dict) else {"value": parsed}
        except urllib_error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise BackendUnavailable(f"http {exc.code}") from exc
        except (urllib_error.URLError, OSError, TimeoutError) as exc:
            raise BackendUnavailable(f"transport: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise BackendUnavailable(f"bad json: {exc}") from exc

    def health(self) -> bool:
        try:
            self._req("GET", "/healthz")
            return True
        except BackendUnavailable:
            return False

    def get(self, project: str, key: str) -> Any:
        body = self._req("GET", f"/{project}/{key}")
        if body is None:
            return None
        if "value" in body:
            return body["value"]
        return body

    def set(
        self, project: str, key: str, value: Any,
        ttl_seconds: Optional[int] = None,
    ) -> None:
        payload = {"value": value}
        if ttl_seconds is not None:
            payload["ttl_seconds"] = ttl_seconds
        self._req("PUT", f"/{project}/{key}", payload)

    def delete(self, project: str, key: str) -> None:
        try:
            self._req("DELETE", f"/{project}/{key}")
        except BackendUnavailable:
            raise

    def list_keys(self, project: str) -> list[str]:
        body = self._req("GET", f"/{project}/_keys")
        if not body:
            return []
        keys = body.get("keys")
        return list(keys) if isinstance(keys, list) else []

    def snapshot(self, project: str) -> dict[str, Any]:
        body = self._req("GET", f"/{project}/_snapshot")
        if not body:
            return {}
        data = body.get("data")
        return dict(data) if isinstance(data, dict) else {}


# ── Main client ──────────────────────────────────────────────────────


class StateClient:
    """Thin client that prefers Sancio, falls back to local files.

    Per spec §6.1 ``concinno.state_client``. ``get`` / ``snapshot`` /
    ``list_keys`` / ``health`` never raise. ``set`` / ``delete`` raise
    ``BackendUnavailable`` when no writable backend exists.

    Backend selection is **lazy** — the constructor does not contact
    the Sancio daemon. ``health()`` and the first read/write attempt
    probe the daemon, cache the probe result for ``probe_ttl`` seconds,
    and use the file backend in the meantime.
    """

    _DAEMON_PROBE_TTL = 5.0

    def __init__(
        self,
        config: Optional[StateClientConfig] = None,
        *,
        http_client: Optional[_SancioHTTP] = None,
    ):
        self._config = config if config is not None else load_config()
        self._http = http_client if http_client is not None else _SancioHTTP(
            self._config.sancio_port,
        )
        self._lock = threading.Lock()
        self._daemon_alive: Optional[bool] = None
        self._daemon_probed_at: float = 0.0

    # ── backend probing ─────────────────────────────────────────────

    def _daemon_reachable(self) -> bool:
        """Cached daemon-up check. Refreshes every ``_DAEMON_PROBE_TTL``s."""
        if self._config.preferred_backend == "file":
            return False
        if self._config.preferred_backend == "legacy_file":
            return False
        with self._lock:
            now = time.monotonic()
            if (
                self._daemon_alive is not None
                and (now - self._daemon_probed_at) < self._DAEMON_PROBE_TTL
            ):
                return self._daemon_alive
            alive = self._http.health()
            self._daemon_alive = alive
            self._daemon_probed_at = now
            if not alive and self._config.preferred_backend == "sancio_http":
                _stderr(
                    "sancio daemon unreachable, using file backend "
                    f"(port={self._config.sancio_port})",
                )
            return alive

    # ── path helpers ────────────────────────────────────────────────

    def _file_path(self, project: str) -> Path:
        return self._config.state_dir / f"{canonicalize_project(project)}.json"

    def _legacy_path(self, project: str) -> Path:
        return self._config.legacy_dir / f"{canonicalize_project(project)}.json"

    # ── file backend primitives (envelope-aware) ────────────────────

    def _read_envelope_file(self, project: str) -> dict:
        """Return raw {key: envelope} from file backend. {} on absent."""
        path = self._file_path(project)
        return _read_json(path)

    def _write_envelope_file(self, project: str, envelopes: dict) -> None:
        """Atomic write with retry under contention."""
        path = self._file_path(project)
        last_exc: Optional[Exception] = None
        for delay in (0.0, *WRITE_RETRY_DELAYS):
            if delay:
                time.sleep(delay)
            try:
                with _file_lock(path):
                    _write_json_atomic(path, envelopes)
                return
            except OSError as exc:
                last_exc = exc
        raise BackendUnavailable(
            f"file backend write failed after retries: {last_exc}",
        )

    def _read_legacy_envelope(self, project: str) -> dict:
        """Best-effort legacy read. Always returns dict; never raises."""
        path = self._legacy_path(project)
        if not path.is_file():
            return {}
        return _read_json(path)

    # ── Public API: reads ───────────────────────────────────────────

    def health(self) -> dict[str, Any]:
        """Return current backend identity + writable flag.

        Backend resolution order: ``sancio_http`` (if daemon up) →
        ``file`` (writable iff state_dir creatable) → ``legacy_file``
        (read-only) → ``unavailable``.
        """
        backend = "unavailable"
        writable = False
        notes: list[str] = []

        if (
            self._config.preferred_backend in ("auto", "sancio_http")
            and self._daemon_reachable()
        ):
            backend = "sancio_http"
            writable = True
        else:
            try:
                self._config.state_dir.mkdir(parents=True, exist_ok=True)
                backend = "file"
                writable = True
            except OSError as exc:
                notes.append(f"file backend unwritable: {exc}")
                if self._legacy_path("__probe__").parent.is_dir():
                    backend = "legacy_file"
                    writable = False

        return {
            "backend": backend,
            "writable": writable,
            "preferred_backend": self._config.preferred_backend,
            "sancio_port": self._config.sancio_port,
            "state_dir": str(self._config.state_dir),
            "config_source": self._config.source,
            "warnings": list(self._config.warnings),
            "notes": notes,
        }

    def snapshot(self, project: str) -> dict[str, Any]:
        """Bulk read all live (non-expired) keys for ``project``.

        Returns ``{}`` on any error. FieldRead's primary call.
        Implements lazy expiration: expired keys are filtered out and
        rewritten to disk on next ``set`` (no write here — read path
        stays side-effect-free for the hot path).
        """
        canonical = canonicalize_project(project)

        # 1. Try Sancio
        if (
            self._config.preferred_backend in ("auto", "sancio_http")
            and self._daemon_reachable()
        ):
            try:
                snap = self._http.snapshot(canonical)
                if snap:
                    return snap
            except BackendUnavailable as exc:
                _stderr(f"sancio snapshot failed ({exc}); falling back")

        # 2. File backend
        envelopes = self._read_envelope_file(canonical)

        # 3. Legacy fallback (only if file backend empty)
        if not envelopes and self._config.preferred_backend != "file":
            envelopes = self._read_legacy_envelope(canonical)

        return {
            k: _unwrap(v) for k, v in envelopes.items()
            if not _is_expired(v)
        }

    def get(self, project: str, key: str) -> Any:
        """Return value for ``key``, or ``None`` if absent / expired.

        Never raises. Lazy expiration: an expired value returns ``None``
        and is removed by the next ``set`` of any key in the project.
        """
        canonical = canonicalize_project(project)

        if (
            self._config.preferred_backend in ("auto", "sancio_http")
            and self._daemon_reachable()
        ):
            try:
                value = self._http.get(canonical, key)
                if value is not None:
                    return value
            except BackendUnavailable as exc:
                _stderr(f"sancio get failed ({exc}); falling back")

        envelopes = self._read_envelope_file(canonical)
        envelope = envelopes.get(key)
        if envelope is None and self._config.preferred_backend != "file":
            envelopes = self._read_legacy_envelope(canonical)
            envelope = envelopes.get(key)
        if envelope is None:
            return None
        if _is_expired(envelope):
            return None
        return _unwrap(envelope)

    def list_keys(self, project: str) -> list[str]:
        """Return live (non-expired) keys for ``project``. Never raises."""
        canonical = canonicalize_project(project)

        if (
            self._config.preferred_backend in ("auto", "sancio_http")
            and self._daemon_reachable()
        ):
            try:
                keys = self._http.list_keys(canonical)
                if keys:
                    return sorted(keys)
            except BackendUnavailable as exc:
                _stderr(f"sancio list_keys failed ({exc}); falling back")

        envelopes = self._read_envelope_file(canonical)
        if not envelopes and self._config.preferred_backend != "file":
            envelopes = self._read_legacy_envelope(canonical)
        return sorted(k for k, v in envelopes.items() if not _is_expired(v))

    # ── Public API: writes (raise on failure) ───────────────────────

    def set(
        self,
        project: str,
        key: str,
        value: Any,
        ttl_seconds: Optional[int] = None,
    ) -> None:
        """Persist ``value`` under ``project/key``.

        Raises ``BackendUnavailable`` when no backend can persist the
        write. Raises ``TypeError`` when ``value`` is not JSON-serializable
        (caught early so the user sees a clear error rather than a
        cryptic ``json.dumps`` traceback half-way through write logic).
        """
        if not isinstance(key, str) or not key:
            raise ValueError("state_client.set: key must be a non-empty string")
        # Pre-flight serialize check: surfacing TypeError here is much
        # easier to debug than failing inside the file lock retry loop.
        try:
            json.dumps(value)
        except TypeError as exc:
            raise TypeError(
                f"state_client.set({project!r}, {key!r}): "
                f"value is not JSON-serializable: {exc}",
            ) from exc

        canonical = canonicalize_project(project)
        envelope = _wrap_value(value, ttl_seconds)

        if (
            self._config.preferred_backend in ("auto", "sancio_http")
            and self._daemon_reachable()
        ):
            try:
                self._http.set(canonical, key, value, ttl_seconds)
                return
            except BackendUnavailable as exc:
                _stderr(f"sancio set failed ({exc}); falling back to file")

        # File backend with read-modify-write under lock
        path = self._file_path(canonical)
        last_exc: Optional[Exception] = None
        for delay in (0.0, *WRITE_RETRY_DELAYS):
            if delay:
                time.sleep(delay)
            try:
                with _file_lock(path):
                    envelopes = _read_json(path)
                    # Lazy GC: drop expired neighbours on every write
                    envelopes = {
                        k: v for k, v in envelopes.items()
                        if not _is_expired(v)
                    }
                    envelopes[key] = envelope
                    _write_json_atomic(path, envelopes)
                return
            except OSError as exc:
                last_exc = exc
        raise BackendUnavailable(
            f"file backend write failed after retries: {last_exc}",
        )

    def delete(self, project: str, key: str) -> None:
        """Remove ``key`` from ``project``. Idempotent — absent key = no-op.

        Raises ``BackendUnavailable`` when no backend can persist the
        change. A no-op delete on an absent key still tries to acquire
        the file lock so concurrent writers see a consistent view; this
        matches spec §3 ``delete`` semantics ("idempotent").
        """
        if not isinstance(key, str) or not key:
            raise ValueError(
                "state_client.delete: key must be a non-empty string",
            )

        canonical = canonicalize_project(project)

        if (
            self._config.preferred_backend in ("auto", "sancio_http")
            and self._daemon_reachable()
        ):
            try:
                self._http.delete(canonical, key)
                return
            except BackendUnavailable as exc:
                _stderr(f"sancio delete failed ({exc}); falling back")

        path = self._file_path(canonical)
        if not path.is_file():
            return  # idempotent
        last_exc: Optional[Exception] = None
        for delay in (0.0, *WRITE_RETRY_DELAYS):
            if delay:
                time.sleep(delay)
            try:
                with _file_lock(path):
                    envelopes = _read_json(path)
                    if key not in envelopes:
                        return  # idempotent within lock
                    envelopes.pop(key, None)
                    _write_json_atomic(path, envelopes)
                return
            except OSError as exc:
                last_exc = exc
        raise BackendUnavailable(
            f"file backend delete failed after retries: {last_exc}",
        )


# ── Module-level convenience singleton ───────────────────────────────


_default_client_lock = threading.Lock()
_default_client: Optional[StateClient] = None


def default_client() -> StateClient:
    """Return the process-wide default ``StateClient`` (lazy)."""
    global _default_client
    with _default_client_lock:
        if _default_client is None:
            _default_client = StateClient()
        return _default_client


def reset_default_client() -> None:
    """Clear the cached default client. Test-only convenience."""
    global _default_client
    with _default_client_lock:
        _default_client = None


def get(project: str, key: str) -> Any:
    return default_client().get(project, key)


def set(  # noqa: A001 — mirror spec ``state.set``; module API by design
    project: str, key: str, value: Any, ttl_seconds: Optional[int] = None,
) -> None:
    default_client().set(project, key, value, ttl_seconds=ttl_seconds)


def delete(project: str, key: str) -> None:
    default_client().delete(project, key)


def list_keys(project: str) -> list[str]:
    return default_client().list_keys(project)


def snapshot(project: str) -> dict[str, Any]:
    return default_client().snapshot(project)


def health() -> dict[str, Any]:
    return default_client().health()


__all__ = [
    "BackendUnavailable",
    "DEFAULT_SANCIO_PORT",
    "StateClient",
    "StateClientConfig",
    "canonicalize_project",
    "default_client",
    "delete",
    "get",
    "health",
    "list_keys",
    "load_config",
    "reset_default_client",
    "set",
    "snapshot",
]
