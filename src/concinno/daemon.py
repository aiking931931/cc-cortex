"""concinno.daemon — long-running in-process tool host.

@module daemon
@responsibility Host the tool registry + executor inside a single,
    persistent Python process. Expose a simple line-delimited JSON
    protocol over a Unix domain socket (POSIX) or named pipe (Windows)
    so external agent loops can call tools without paying the
    ``python -c`` + import cost per call.
@dependencies concinno.tools.registry (soft, late-imported);
    concinno.tool_executor.Tool (Protocol check only)
@exports Daemon, DaemonClient, DaemonConfig, main

Why a daemon? In-process Python is the Concinno preferred stack
(MEMORY #36): no IPC overhead, shared state, no cold-start. But CC
hook scripts and external consumers need an entry point that speaks a
stable protocol. The daemon bridges the two: tools stay in-process
inside the daemon; clients talk JSON lines over a local socket.

Protocol (one JSON object per line, ``\\n``-terminated):

Request::

    {"op": "list"}
    {"op": "call", "name": "FileRead", "kwargs": {"path": "/x"}}
    {"op": "search", "query": "select:Shell", "max_results": 5}
    {"op": "ping"}
    {"op": "shutdown"}

Response::

    {"ok": true, "result": ...}
    {"ok": false, "error": "...message..."}

Transport:

- POSIX: Unix domain socket at ``~/.concinno/daemon.sock``.
- Windows: TCP loopback (``127.0.0.1:<port>``) — named pipes via
  ``socket`` require pywin32 which violates "zero runtime deps for
  core". A loopback-bound socket with a lockfile-stored port achieves
  the same local-only IPC.

Lifecycle: ``Daemon.start()`` writes a pidfile + transport info to
``~/.concinno/daemon.pid`` (JSON). ``Daemon.stop()`` removes both.
Stale pidfiles (process dead) are reclaimed automatically.
"""

from __future__ import annotations

import argparse
import errno
import json
import logging
import os
import socket
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("concinno.daemon")

# Protocol constants.
_MAX_LINE_BYTES = 4 * 1024 * 1024  # 4 MiB — generous for file Read results.
_RECV_CHUNK = 65536
_SHUTDOWN_GRACE_S = 2.0

# Default transport locations.
_DEFAULT_DIR = Path.home() / ".concinno"
_DEFAULT_PIDFILE = _DEFAULT_DIR / "daemon.pid"
_DEFAULT_SOCKET = _DEFAULT_DIR / "daemon.sock"


@dataclass
class DaemonConfig:
    """Runtime configuration for the daemon.

    ``registry_factory`` lets tests inject a pre-populated ToolRegistry
    without depending on the default builtin set. Production callers
    pass nothing; :func:`concinno.tools.registry.get_default_registry`
    is used.
    """

    pidfile: Path = field(default_factory=lambda: _DEFAULT_PIDFILE)
    socket_path: Path = field(default_factory=lambda: _DEFAULT_SOCKET)
    tcp_host: str = "127.0.0.1"
    tcp_port: int = 0  # 0 = auto-assign
    registry_factory: Any = None  # Callable[[], ToolRegistry] | None


def _is_posix_socket_available() -> bool:
    """True on platforms with AF_UNIX (POSIX including Mac, not Windows)."""
    return hasattr(socket, "AF_UNIX") and sys.platform != "win32"


def _read_pidfile(path: Path) -> dict[str, Any] | None:
    """Return parsed pidfile dict or None if missing/corrupt."""
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        return None
    except (OSError, json.JSONDecodeError):
        return None


def _process_alive(pid: int) -> bool:
    """Cross-platform pid liveness check."""
    if pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            # On Windows, signal 0 isn't supported; use OpenProcess via os.kill
            # fallback — ``os.kill(pid, 0)`` raises OSError for missing pid.
            os.kill(pid, 0)
            return True
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError as exc:
        # EPERM on POSIX = process exists but owned by other user.
        return exc.errno == errno.EPERM


class Daemon:
    """In-process tool host with JSON-lines IPC.

    The daemon runs a thread-per-connection accept loop. Each tool call
    executes synchronously on the handler thread — ``Tool`` implementations
    that declare ``is_concurrency_safe = True`` can safely be invoked in
    parallel since the registry's ``get()`` is thread-safe (instance
    caching under GIL-protected dict set). Non-safe tools should use
    their own locking.
    """

    def __init__(self, config: DaemonConfig | None = None) -> None:
        self.config = config or DaemonConfig()
        self._server: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._stop_flag = threading.Event()
        self._registry: Any | None = None  # concinno.tools.registry.ToolRegistry

    # ── Registry ──────────────────────────────────────────────────────

    def _get_registry(self) -> Any:
        if self._registry is not None:
            return self._registry
        if self.config.registry_factory is not None:
            self._registry = self.config.registry_factory()
        else:
            from .tools.registry import get_default_registry

            self._registry = get_default_registry()
        return self._registry

    # ── Lifecycle ─────────────────────────────────────────────────────

    def start(self) -> dict[str, Any]:
        """Start the daemon and block until :meth:`stop` is called.

        Returns the transport info dict (also written to the pidfile)
        once the server has bound. Callers that want non-blocking
        behavior should invoke :meth:`start_background`.
        """
        info = self.start_background()
        try:
            # Join the accept loop thread; exits when _stop_flag set.
            assert self._accept_thread is not None
            while self._accept_thread.is_alive():
                self._accept_thread.join(timeout=0.5)
        except KeyboardInterrupt:
            logger.info("daemon: KeyboardInterrupt — shutting down")
            self.stop()
        return info

    def start_background(self) -> dict[str, Any]:
        """Bind + spawn accept thread without blocking the caller."""
        if self._server is not None:
            msg = "daemon already started"
            raise RuntimeError(msg)

        # Clean up stale pidfile if previous owner is dead.
        existing = _read_pidfile(self.config.pidfile)
        if existing and _process_alive(int(existing.get("pid", 0))):
            msg = f"daemon already running (pid {existing['pid']})"
            raise RuntimeError(msg)
        if existing:
            logger.info("daemon: reclaiming stale pidfile %s", self.config.pidfile)
            try:
                self.config.pidfile.unlink()
            except OSError:
                pass

        self.config.pidfile.parent.mkdir(parents=True, exist_ok=True)
        transport_info = self._bind_server()

        payload = {
            "pid": os.getpid(),
            "transport": transport_info,
            "concinno_version": _get_version(),
        }
        with self.config.pidfile.open("w", encoding="utf-8") as f:
            json.dump(payload, f)

        self._accept_thread = threading.Thread(
            target=self._accept_loop,
            name="concinno-daemon-accept",
            daemon=True,
        )
        self._accept_thread.start()
        logger.info("daemon: started pid=%d transport=%r", os.getpid(), transport_info)
        return payload

    def stop(self) -> None:
        """Signal the accept loop to exit and clean up pidfile/socket."""
        self._stop_flag.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None
        if self._accept_thread is not None and self._accept_thread.is_alive():
            self._accept_thread.join(timeout=_SHUTDOWN_GRACE_S)
        # Remove pidfile + unix socket (best-effort).
        for path in (self.config.pidfile, self.config.socket_path):
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass

    # ── Transport ─────────────────────────────────────────────────────

    def _bind_server(self) -> dict[str, Any]:
        """Bind the listening socket. Returns transport descriptor dict."""
        if _is_posix_socket_available():
            # Remove stale socket file.
            if self.config.socket_path.exists():
                try:
                    self.config.socket_path.unlink()
                except OSError:
                    pass
            self.config.socket_path.parent.mkdir(parents=True, exist_ok=True)
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(str(self.config.socket_path))
            srv.listen(8)
            try:
                os.chmod(self.config.socket_path, 0o600)
            except OSError:
                pass
            self._server = srv
            return {"kind": "unix", "path": str(self.config.socket_path)}
        # Windows: TCP loopback.
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind((self.config.tcp_host, self.config.tcp_port))
        srv.listen(8)
        host, port = srv.getsockname()
        self._server = srv
        return {"kind": "tcp", "host": host, "port": port}

    def _accept_loop(self) -> None:
        assert self._server is not None
        self._server.settimeout(0.5)
        while not self._stop_flag.is_set():
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            handler = threading.Thread(
                target=self._handle_connection,
                args=(conn,),
                name="concinno-daemon-conn",
                daemon=True,
            )
            handler.start()

    def _handle_connection(self, conn: socket.socket) -> None:
        """Read line-delimited JSON requests until peer closes."""
        buf = b""
        try:
            while not self._stop_flag.is_set():
                chunk = conn.recv(_RECV_CHUNK)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > _MAX_LINE_BYTES:
                    self._send_json(conn, {"ok": False, "error": "request too large"})
                    break
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    response = self._dispatch_line(line)
                    self._send_json(conn, response)
                    if response.get("_shutdown"):
                        self._stop_flag.set()
                        return
        except OSError as exc:
            logger.debug("daemon: connection closed: %s", exc)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _send_json(conn: socket.socket, payload: dict[str, Any]) -> None:
        shutdown = payload.pop("_shutdown", False)
        try:
            data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
            conn.sendall(data)
        except (OSError, TypeError) as exc:
            logger.debug("daemon: failed to send response: %s", exc)
        if shutdown:
            payload["_shutdown"] = True  # restore for caller

    # ── Dispatch ──────────────────────────────────────────────────────

    def _dispatch_line(self, line: bytes) -> dict[str, Any]:
        try:
            req = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return {"ok": False, "error": f"bad json: {exc}"}
        if not isinstance(req, dict):
            return {"ok": False, "error": "request must be a JSON object"}
        op = req.get("op")
        try:
            if op == "ping":
                return {"ok": True, "result": "pong"}
            if op == "list":
                reg = self._get_registry()
                return {
                    "ok": True,
                    "result": {
                        "core": reg.list_core(),
                        "deferred": reg.list_deferred(),
                    },
                }
            if op == "search":
                reg = self._get_registry()
                results = reg.search(
                    req.get("query", ""),
                    max_results=int(req.get("max_results", 5)),
                )
                return {
                    "ok": True,
                    "result": [
                        {
                            "name": r.name,
                            "description": r.description,
                            "score": r.score,
                            "source": r.source,
                        }
                        for r in results
                    ],
                }
            if op == "call":
                name = req.get("name")
                kwargs = req.get("kwargs") or {}
                if not isinstance(name, str) or not isinstance(kwargs, dict):
                    return {"ok": False, "error": "call requires 'name' and dict 'kwargs'"}
                reg = self._get_registry()
                tool = reg.get(name)
                if tool is None:
                    return {"ok": False, "error": f"unknown tool: {name}"}
                result = tool.call(**kwargs)
                return {"ok": True, "result": result}
            if op == "shutdown":
                return {"ok": True, "result": "bye", "_shutdown": True}
            return {"ok": False, "error": f"unknown op: {op!r}"}
        except Exception as exc:  # noqa: BLE001 — tool faults must not kill daemon
            logger.warning("daemon: op %r failed: %s", op, exc)
            return {"ok": False, "error": str(exc)}


# ── Client ────────────────────────────────────────────────────────────


class DaemonClient:
    """Thin JSON-lines client. Connects to a running daemon via pidfile."""

    def __init__(self, pidfile: Path | None = None) -> None:
        self._pidfile = pidfile or _DEFAULT_PIDFILE
        self._sock: socket.socket | None = None
        self._buf = b""

    def connect(self) -> None:
        info = _read_pidfile(self._pidfile)
        if info is None:
            msg = f"no daemon pidfile at {self._pidfile}"
            raise RuntimeError(msg)
        transport = info.get("transport", {})
        kind = transport.get("kind")
        if kind == "unix":
            if not _is_posix_socket_available():
                msg = "pidfile says unix socket but this platform has no AF_UNIX"
                raise RuntimeError(msg)
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(transport["path"])
        elif kind == "tcp":
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((transport["host"], int(transport["port"])))
        else:
            msg = f"unknown transport kind: {kind!r}"
            raise RuntimeError(msg)
        self._sock = sock

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def request(self, op: str, **fields: Any) -> dict[str, Any]:
        if self._sock is None:
            self.connect()
        assert self._sock is not None
        payload = {"op": op, **fields}
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        self._sock.sendall(data)
        return self._recv_line()

    def _recv_line(self) -> dict[str, Any]:
        assert self._sock is not None
        while b"\n" not in self._buf:
            chunk = self._sock.recv(_RECV_CHUNK)
            if not chunk:
                msg = "daemon closed connection"
                raise RuntimeError(msg)
            self._buf += chunk
            if len(self._buf) > _MAX_LINE_BYTES:
                msg = "response too large"
                raise RuntimeError(msg)
        line, self._buf = self._buf.split(b"\n", 1)
        return json.loads(line.decode("utf-8"))

    def __enter__(self) -> DaemonClient:
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


# ── Version helper ────────────────────────────────────────────────────


def _get_version() -> str:
    try:
        from importlib import metadata as _m

        return _m.version("concinno")
    except Exception:  # noqa: BLE001 — version reporting must not fail
        return "unknown"


# ── CLI ───────────────────────────────────────────────────────────────


def _cmd_status(config: DaemonConfig) -> int:
    info = _read_pidfile(config.pidfile)
    if info is None:
        print("concinno-daemon: not running")
        return 1
    pid = int(info.get("pid", 0))
    if not _process_alive(pid):
        print(f"concinno-daemon: pidfile present (pid={pid}) but process dead — stale")
        return 2
    print(json.dumps(info, indent=2, ensure_ascii=False))
    return 0


def _cmd_start(config: DaemonConfig) -> int:
    daemon = Daemon(config)
    try:
        daemon.start()
    except RuntimeError as exc:
        print(f"concinno-daemon: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_stop(config: DaemonConfig) -> int:
    info = _read_pidfile(config.pidfile)
    if info is None:
        print("concinno-daemon: not running")
        return 0
    # Send shutdown op via client — clean exit.
    try:
        client = DaemonClient(config.pidfile)
        client.connect()
        client.request("shutdown")
        client.close()
    except (OSError, RuntimeError) as exc:
        logger.warning("daemon stop: client request failed: %s", exc)
    # Remove pidfile if still there.
    try:
        if config.pidfile.exists():
            config.pidfile.unlink()
    except OSError:
        pass
    return 0


def main(argv: list[str] | None = None) -> int:
    """``concinno-daemon`` CLI entry point."""
    parser = argparse.ArgumentParser(prog="concinno-daemon")
    parser.add_argument("action", choices=("start", "stop", "status"))
    args = parser.parse_args(argv)
    config = DaemonConfig()
    if args.action == "start":
        return _cmd_start(config)
    if args.action == "stop":
        return _cmd_stop(config)
    return _cmd_status(config)


__all__ = [
    "Daemon",
    "DaemonClient",
    "DaemonConfig",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
