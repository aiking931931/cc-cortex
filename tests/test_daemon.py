"""Tests for concinno.daemon — in-process tool host + JSON-lines IPC."""

from __future__ import annotations

import json
import os
import socket
import threading
import time

import pytest

from concinno.daemon import (
    Daemon,
    DaemonClient,
    DaemonConfig,
    _process_alive,
    _read_pidfile,
)
from concinno.tools.registry import ToolRegistry

# ── Fakes ──────────────────────────────────────────────────────────────


class _FakeTool:
    is_concurrency_safe = True

    def __init__(self, name: str, description: str, fn=None) -> None:
        self.name = name
        self.description = description
        self._fn = fn or (lambda **kw: f"echo:{kw}")

    def call(self, **kwargs: object) -> object:
        return self._fn(**kwargs)


def _make_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register_core(_FakeTool("Ping", "ping tool", fn=lambda **kw: "pong"))  # noqa: ARG005
    reg.register_core(
        _FakeTool("Add", "add two numbers", fn=lambda a, b, **_: a + b)
    )
    return reg


@pytest.fixture
def running_daemon(tmp_path):
    """Yield a started Daemon bound to a tmp pidfile + socket."""
    config = DaemonConfig(
        pidfile=tmp_path / "daemon.pid",
        socket_path=tmp_path / "daemon.sock",
        registry_factory=_make_registry,
    )
    daemon = Daemon(config)
    daemon.start_background()
    # Wait until the socket file appears (POSIX) or pidfile set (Win TCP).
    for _ in range(50):
        if config.pidfile.exists():
            break
        time.sleep(0.02)
    assert config.pidfile.exists(), "daemon pidfile never created"
    try:
        yield daemon, config
    finally:
        daemon.stop()


# ── Pidfile helpers ────────────────────────────────────────────────────


class TestPidfileHelpers:
    def test_read_missing_returns_none(self, tmp_path):
        assert _read_pidfile(tmp_path / "no.pid") is None

    def test_read_corrupt_returns_none(self, tmp_path):
        p = tmp_path / "bad.pid"
        p.write_text("not json", encoding="utf-8")
        assert _read_pidfile(p) is None

    def test_read_valid(self, tmp_path):
        p = tmp_path / "ok.pid"
        p.write_text(json.dumps({"pid": 12345}), encoding="utf-8")
        assert _read_pidfile(p) == {"pid": 12345}

    def test_process_alive_self(self):
        assert _process_alive(os.getpid()) is True

    def test_process_alive_bogus(self):
        # Very high PID unlikely to exist.
        assert _process_alive(999999999) is False

    def test_process_alive_zero(self):
        assert _process_alive(0) is False


# ── Start / stop lifecycle ─────────────────────────────────────────────


class TestLifecycle:
    def test_pidfile_written(self, running_daemon):
        _daemon, config = running_daemon
        info = _read_pidfile(config.pidfile)
        assert info is not None
        assert info["pid"] == os.getpid()
        assert "transport" in info

    def test_cannot_start_twice(self, running_daemon):
        daemon, _config = running_daemon
        with pytest.raises(RuntimeError, match="already started"):
            daemon.start_background()

    def test_stale_pidfile_reclaimed(self, tmp_path):
        """Simulate a dead previous daemon — new one should start."""
        pid = tmp_path / "daemon.pid"
        # Write a pidfile with a bogus dead pid.
        pid.write_text(
            json.dumps(
                {
                    "pid": 999999999,
                    "transport": {"kind": "tcp", "host": "127.0.0.1", "port": 1},
                }
            ),
            encoding="utf-8",
        )
        config = DaemonConfig(
            pidfile=pid,
            socket_path=tmp_path / "daemon.sock",
            registry_factory=_make_registry,
        )
        daemon = Daemon(config)
        try:
            info = daemon.start_background()
            assert info["pid"] == os.getpid()
        finally:
            daemon.stop()

    def test_stop_removes_pidfile(self, tmp_path):
        config = DaemonConfig(
            pidfile=tmp_path / "daemon.pid",
            socket_path=tmp_path / "daemon.sock",
            registry_factory=_make_registry,
        )
        daemon = Daemon(config)
        daemon.start_background()
        assert config.pidfile.exists()
        daemon.stop()
        assert not config.pidfile.exists()


# ── IPC roundtrips ─────────────────────────────────────────────────────


class TestDispatch:
    def test_ping(self, running_daemon):
        _daemon, config = running_daemon
        with DaemonClient(config.pidfile) as client:
            resp = client.request("ping")
        assert resp == {"ok": True, "result": "pong"}

    def test_list(self, running_daemon):
        _daemon, config = running_daemon
        with DaemonClient(config.pidfile) as client:
            resp = client.request("list")
        assert resp["ok"] is True
        assert set(resp["result"]["core"]) == {"Ping", "Add"}
        assert resp["result"]["deferred"] == []

    def test_call_success(self, running_daemon):
        _daemon, config = running_daemon
        with DaemonClient(config.pidfile) as client:
            resp = client.request("call", name="Add", kwargs={"a": 2, "b": 40})
        assert resp == {"ok": True, "result": 42}

    def test_call_unknown_tool(self, running_daemon):
        _daemon, config = running_daemon
        with DaemonClient(config.pidfile) as client:
            resp = client.request("call", name="Nope", kwargs={})
        assert resp["ok"] is False
        assert "unknown tool" in resp["error"]

    def test_call_missing_name(self, running_daemon):
        _daemon, config = running_daemon
        with DaemonClient(config.pidfile) as client:
            resp = client.request("call")
        assert resp["ok"] is False

    def test_unknown_op(self, running_daemon):
        _daemon, config = running_daemon
        with DaemonClient(config.pidfile) as client:
            resp = client.request("bogus")
        assert resp["ok"] is False
        assert "unknown op" in resp["error"]

    def test_search_select(self, running_daemon):
        _daemon, config = running_daemon
        with DaemonClient(config.pidfile) as client:
            resp = client.request("search", query="select:Ping", max_results=1)
        assert resp["ok"] is True
        # Ping is core — search returns it.
        assert len(resp["result"]) == 1
        assert resp["result"][0]["name"] == "Ping"

    def test_bad_json_gets_error(self, running_daemon):
        _daemon, config = running_daemon
        info = _read_pidfile(config.pidfile)
        assert info is not None
        transport = info["transport"]
        if transport["kind"] == "unix":
            if not hasattr(socket, "AF_UNIX"):
                pytest.skip("no AF_UNIX on this platform")
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(transport["path"])
        else:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((transport["host"], int(transport["port"])))
        try:
            sock.sendall(b"{ not json\n")
            data = sock.recv(4096)
        finally:
            sock.close()
        resp = json.loads(data.decode("utf-8").strip())
        assert resp["ok"] is False
        assert "bad json" in resp["error"]


# ── Concurrent requests ────────────────────────────────────────────────


class TestConcurrency:
    def test_many_parallel_pings(self, running_daemon):
        _daemon, config = running_daemon
        results: list[dict] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker() -> None:
            try:
                with DaemonClient(config.pidfile) as client:
                    resp = client.request("ping")
                with lock:
                    results.append(resp)
            except BaseException as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert errors == []
        assert len(results) == 10
        assert all(r == {"ok": True, "result": "pong"} for r in results)


# ── Shutdown op ────────────────────────────────────────────────────────


class TestShutdownOp:
    def test_shutdown_exits_daemon(self, tmp_path):
        config = DaemonConfig(
            pidfile=tmp_path / "daemon.pid",
            socket_path=tmp_path / "daemon.sock",
            registry_factory=_make_registry,
        )
        daemon = Daemon(config)
        daemon.start_background()
        try:
            with DaemonClient(config.pidfile) as client:
                resp = client.request("shutdown")
            assert resp["ok"] is True
            # Accept thread should exit shortly.
            for _ in range(50):
                if daemon._accept_thread is None or not daemon._accept_thread.is_alive():
                    break
                time.sleep(0.05)
        finally:
            daemon.stop()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
