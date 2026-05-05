"""Tests for concinno.full_mode_services — GUI auto-launch / stop on
handoff-mode transitions.

We never launch a real subprocess here — the unit tests patch
``subprocess.Popen`` and ``_port_bound`` so pidfile bookkeeping and
decision logic are validated in isolation.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def fms(tmp_path, monkeypatch):
    """Isolate ``~/.concinno`` to a tmp dir so tests don't mutate user state."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows
    import sys
    sys.modules.pop("concinno.full_mode_services", None)
    from concinno import full_mode_services
    # Route pidfile + log dir into tmp_path so the test is hermetic.
    monkeypatch.setattr(full_mode_services, "_state_dir",
                        lambda: tmp_path / ".concinno")
    (tmp_path / ".concinno").mkdir(parents=True, exist_ok=True)
    return full_mode_services


class TestOptOut:
    def test_autolaunch_disabled_env(self, fms, monkeypatch):
        monkeypatch.setenv("CONCINNO_FULL_MODE_AUTOLAUNCH_GUI", "0")
        res = fms.launch_gui()
        assert res["status"] == "skipped"
        assert "CONCINNO_FULL_MODE_AUTOLAUNCH_GUI" in res["reason"]

    def test_services_globally_off(self, fms, monkeypatch):
        monkeypatch.setenv("CONCINNO_FULL_MODE_SERVICES", "off")
        res = fms.launch_gui()
        assert res["status"] == "skipped"

    def test_force_bypasses_optout(self, fms, monkeypatch):
        monkeypatch.setenv("CONCINNO_FULL_MODE_AUTOLAUNCH_GUI", "0")
        with patch.object(fms, "_port_bound", return_value=True), \
             patch("subprocess.Popen") as popen:
            popen.return_value = MagicMock(pid=12345)
            res = fms.launch_gui(force=True)
            # already-running since _port_bound is True
            assert res["status"] == "already-running"


class TestLaunchGui:
    def test_already_running_when_port_bound(self, fms):
        with patch.object(fms, "_port_bound", return_value=True):
            res = fms.launch_gui()
            assert res["status"] == "already-running"
            assert res["port"] == 8400

    def test_spawns_when_port_free(self, fms, monkeypatch):
        monkeypatch.setenv("CONCINNO_FULL_MODE_AUTOLAUNCH_GUI", "1")
        monkeypatch.setenv("CONCINNO_FULL_MODE_SERVICES", "on")
        # Port free until after Popen is called, then bound.
        calls = {"n": 0}

        def fake_port_bound(host, port, timeout=0.2):
            calls["n"] += 1
            return calls["n"] > 1
        with patch.object(fms, "_port_bound", side_effect=fake_port_bound), \
             patch("subprocess.Popen") as popen:
            popen.return_value = MagicMock(pid=99999)
            res = fms.launch_gui()
            assert res["status"] == "launched"
            assert res["pid"] == 99999
            assert res["url"] == "http://127.0.0.1:8400"

    def test_launch_writes_pidfile(self, fms, monkeypatch):
        monkeypatch.setenv("CONCINNO_FULL_MODE_AUTOLAUNCH_GUI", "1")
        monkeypatch.setenv("CONCINNO_FULL_MODE_SERVICES", "on")
        calls = {"n": 0}

        def fake_port_bound(host, port, timeout=0.2):
            calls["n"] += 1
            return calls["n"] > 1
        with patch.object(fms, "_port_bound", side_effect=fake_port_bound), \
             patch("subprocess.Popen") as popen:
            popen.return_value = MagicMock(pid=77777)
            fms.launch_gui()
            info = fms._read_pidfile()
            assert info["pid"] == 77777
            assert info["port"] == 8400

    def test_failed_when_port_never_binds(self, fms, monkeypatch):
        monkeypatch.setenv("CONCINNO_FULL_MODE_AUTOLAUNCH_GUI", "1")
        monkeypatch.setenv("CONCINNO_FULL_MODE_SERVICES", "on")
        with patch.object(fms, "_port_bound", return_value=False), \
             patch("subprocess.Popen") as popen:
            popen.return_value = MagicMock(pid=11111)
            res = fms.launch_gui()
            assert res["status"] == "failed"
            assert "port not bound" in res["reason"]


class TestStopGui:
    def test_not_tracked_when_pidfile_absent(self, fms):
        res = fms.stop_gui()
        assert res["status"] == "not-tracked"

    def test_already_stopped_when_pid_dead(self, fms):
        fms._write_pidfile({"pid": 999999, "host": "127.0.0.1",
                            "port": 8400, "started_at": 0})
        with patch.object(fms, "_pid_alive", return_value=False):
            res = fms.stop_gui()
            assert res["status"] == "already-stopped"
            assert fms._read_pidfile() is None

    def test_stops_live_pid(self, fms, monkeypatch):
        fms._write_pidfile({"pid": 55555, "host": "127.0.0.1",
                            "port": 8400, "started_at": 0})
        alive_calls = {"n": 0}

        def fake_alive(pid):
            alive_calls["n"] += 1
            return alive_calls["n"] == 1  # live first, dead after kill
        monkeypatch.setattr(fms, "_pid_alive", fake_alive)
        with patch("subprocess.run") as run:
            run.return_value = MagicMock(returncode=0)
            res = fms.stop_gui()
            assert res["status"] == "stopped"
            assert res["pid"] == 55555
            assert fms._read_pidfile() is None


class TestEnsureServicesForMode:
    def test_full_launches_gui(self, fms):
        with patch.object(fms, "launch_gui",
                          return_value={"status": "launched", "pid": 1}) as m:
            rep = fms.ensure_services_for_mode("full")
            m.assert_called_once()
            assert rep["services"]["gui"]["status"] == "launched"

    def test_phase_stops_gui(self, fms):
        with patch.object(fms, "stop_gui",
                          return_value={"status": "not-tracked"}) as m:
            rep = fms.ensure_services_for_mode("phase")
            m.assert_called_once()
            assert rep["services"]["gui"]["status"] == "not-tracked"

    def test_save_token_stops_gui(self, fms):
        with patch.object(fms, "stop_gui",
                          return_value={"status": "already-stopped"}):
            rep = fms.ensure_services_for_mode("save-token")
            assert rep["services"]["gui"]["status"] == "already-stopped"

    def test_services_globally_off_skips(self, fms, monkeypatch):
        monkeypatch.setenv("CONCINNO_FULL_MODE_SERVICES", "off")
        with patch.object(fms, "launch_gui") as launch, \
             patch.object(fms, "stop_gui") as stop:
            rep = fms.ensure_services_for_mode("full")
            launch.assert_not_called()
            stop.assert_not_called()
            assert "_note" in rep["services"]


class TestGuiIsRunning:
    def test_pidfile_with_alive_pid(self, fms):
        fms._write_pidfile({"pid": 1234, "host": "127.0.0.1", "port": 8400,
                            "started_at": 0})
        with patch.object(fms, "_pid_alive", return_value=True):
            assert fms.gui_is_running() is True

    def test_port_bound_without_pidfile(self, fms):
        with patch.object(fms, "_port_bound", return_value=True):
            assert fms.gui_is_running() is True

    def test_nothing_running(self, fms):
        with patch.object(fms, "_port_bound", return_value=False), \
             patch.object(fms, "_pid_alive", return_value=False):
            assert fms.gui_is_running() is False
