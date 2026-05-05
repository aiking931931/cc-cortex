"""Tests for ``concinno.auto_update.tier2_self_update`` + CLI wiring.

Coverage:
  * detect_pip_install_method returns a known string token
  * get_latest_pypi_version mocks PyPI JSON, applies pre-release filter,
    handles network error, handles yanked-only releases
  * self_update dry-run does not spawn
  * self_update spawn path uses correct OS-specific flags (mocked)
  * self_update aborts on editable install
  * self_update aborts when current_version == target with no spawn
  * is_concinno_in_use returns sane shape (no psutil installed branch)
  * CLI ``concinno self-update --dry-run`` smoke
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from concinno.auto_update import tier2_self_update as mod

# ── detect_pip_install_method ─────────────────────────────────────


class TestDetectInstallMethod:
    def test_returns_valid_token(self) -> None:
        method = mod.detect_pip_install_method()
        assert method in {
            "editable",
            "pip-venv",
            "pip-user",
            "pip-system",
            "unknown",
        }


# ── get_latest_pypi_version ───────────────────────────────────────


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_a: object) -> None:
        return None


class TestGetLatestPyPIVersion:
    def test_picks_latest_stable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = json.dumps(
            {
                "releases": {
                    "2.34.0": [{"yanked": False}],
                    "2.35.0": [{"yanked": False}],
                    "2.35.1": [{"yanked": False}],
                    "2.36.0a1": [{"yanked": False}],  # pre-release excluded
                }
            }
        ).encode("utf-8")

        def fake_open(url: str, timeout: int = 5) -> _FakeResponse:
            assert "concinno" in url
            return _FakeResponse(payload)

        monkeypatch.setattr(mod.urllib.request, "urlopen", fake_open)
        assert mod.get_latest_pypi_version("concinno") == "2.35.1"

    def test_pre_release_flag_includes_alpha(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = json.dumps(
            {
                "releases": {
                    "2.35.1": [{"yanked": False}],
                    "2.36.0a1": [{"yanked": False}],
                }
            }
        ).encode("utf-8")

        monkeypatch.setattr(
            mod.urllib.request,
            "urlopen",
            lambda *_a, **_k: _FakeResponse(payload),
        )
        assert mod.get_latest_pypi_version("concinno", pre_release=True) == "2.36.0a1"

    def test_network_error_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*_a: object, **_k: object) -> None:
            raise mod.urllib.error.URLError("boom")

        monkeypatch.setattr(mod.urllib.request, "urlopen", boom)
        assert mod.get_latest_pypi_version("concinno") is None

    def test_skips_yanked_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = json.dumps(
            {
                "releases": {
                    "2.35.0": [{"yanked": True}],  # all yanked
                    "2.34.0": [{"yanked": False}],
                }
            }
        ).encode("utf-8")
        monkeypatch.setattr(
            mod.urllib.request,
            "urlopen",
            lambda *_a, **_k: _FakeResponse(payload),
        )
        assert mod.get_latest_pypi_version("concinno") == "2.34.0"

    def test_pre_release_classifier_directly(self) -> None:
        assert mod._is_pre_release("2.36.0a1") is True
        assert mod._is_pre_release("2.36.0rc2") is True
        assert mod._is_pre_release("2.35.1") is False
        assert mod._is_pre_release("1.0.0") is False


# ── is_concinno_in_use ────────────────────────────────────────────


class TestIsInUse:
    def test_returns_sane_tuple(self) -> None:
        in_use, evidence = mod.is_concinno_in_use()
        assert isinstance(in_use, bool)
        assert isinstance(evidence, list)
        # Each evidence entry must be a string.
        for line in evidence:
            assert isinstance(line, str)


# ── self_update flow ──────────────────────────────────────────────


class TestSelfUpdate:
    def test_dry_run_does_not_spawn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Force pip-system + known latest so flow reaches dry-run gate.
        monkeypatch.setattr(mod, "detect_pip_install_method", lambda: "pip-system")
        monkeypatch.setattr(
            mod, "get_latest_pypi_version",
            lambda *_a, **_k: "9.99.99",
        )
        spawn_called: list = []
        monkeypatch.setattr(
            mod,
            "_spawn_detached",
            lambda *a, **k: (spawn_called.append((a, k)) or (12345, None)),
        )
        monkeypatch.setattr(
            mod, "is_concinno_in_use", lambda: (False, [])
        )

        result = mod.self_update(dry_run=True, skip_in_use_check=True)
        assert spawn_called == []
        assert result.dry_run is True
        assert result.helper_spawned is False
        assert result.helper_pid is None
        assert result.target_version == "9.99.99"
        assert result.error is None

    def test_editable_install_aborts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mod, "detect_pip_install_method", lambda: "editable")
        result = mod.self_update(dry_run=False, skip_in_use_check=True)
        assert result.error is not None
        assert "editable" in result.error
        assert result.helper_spawned is False

    def test_pypi_fetch_failure_aborts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mod, "detect_pip_install_method", lambda: "pip-user")
        monkeypatch.setattr(mod, "get_latest_pypi_version", lambda *_a, **_k: None)
        result = mod.self_update(skip_in_use_check=True)
        assert result.error is not None
        assert "PyPI" in result.error
        assert result.helper_spawned is False

    def test_already_on_target_no_spawn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import concinno

        monkeypatch.setattr(mod, "detect_pip_install_method", lambda: "pip-user")
        spawn_called: list = []
        monkeypatch.setattr(
            mod, "_spawn_detached",
            lambda *a, **k: (spawn_called.append((a, k)) or (1, None)),
        )
        result = mod.self_update(
            target_version=concinno.__version__,
            skip_in_use_check=True,
        )
        assert spawn_called == []
        assert result.helper_spawned is False
        assert result.error is None

    def test_spawn_path_invokes_helper(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mod, "detect_pip_install_method", lambda: "pip-user")
        monkeypatch.setattr(
            mod, "get_latest_pypi_version", lambda *_a, **_k: "99.0.0"
        )
        captured: dict = {}

        def fake_spawn(target: str) -> tuple:
            captured["target"] = target
            return 4242, "/tmp/log.txt"

        monkeypatch.setattr(mod, "_spawn_detached", fake_spawn)
        result = mod.self_update(skip_in_use_check=True)
        assert captured.get("target") == "99.0.0"
        assert result.helper_spawned is True
        assert result.helper_pid == 4242
        assert result.log_path == "/tmp/log.txt"
        assert result.error is None

    def test_in_use_blocks_on_windows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mod, "detect_pip_install_method", lambda: "pip-user")
        monkeypatch.setattr(
            mod, "get_latest_pypi_version", lambda *_a, **_k: "99.0.0"
        )
        monkeypatch.setattr(
            mod, "is_concinno_in_use", lambda: (True, ["PID 1: claude.exe"])
        )
        monkeypatch.setattr(mod.platform, "system", lambda: "Windows")
        spawn_called: list = []
        monkeypatch.setattr(
            mod, "_spawn_detached",
            lambda *a, **k: (spawn_called.append((a, k)) or (1, None)),
        )
        result = mod.self_update()
        assert spawn_called == []
        assert result.error is not None
        assert "in use" in result.error.lower()
        assert result.in_use_warning == ["PID 1: claude.exe"]


# ── _spawn_detached OS-flag verification ──────────────────────────


class TestSpawnDetachedFlags:
    """Verify the OS-specific subprocess flags are correct.

    We monkey-patch ``subprocess.Popen`` and inspect the kwargs passed.
    """

    def test_windows_flags(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(mod.platform, "system", lambda: "Windows")
        seen: dict = {}

        class _FakeProc:
            pid = 7777

        def fake_popen(argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            return _FakeProc()

        monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
        pid, log_path = mod._spawn_detached("9.0.0")
        assert pid == 7777
        assert log_path is None  # Windows uses DEVNULL, no log path
        kwargs = seen["kwargs"]
        flags = kwargs.get("creationflags", 0)
        assert flags & mod._DETACHED_PROCESS
        assert flags & mod._CREATE_NEW_PROCESS_GROUP
        assert kwargs.get("close_fds") is True
        assert kwargs.get("stdin") == subprocess.DEVNULL
        assert kwargs.get("stdout") == subprocess.DEVNULL
        assert kwargs.get("stderr") == subprocess.DEVNULL
        argv = seen["argv"]
        assert argv[0] == sys.executable
        assert "-m" in argv and "pip" in argv
        assert "concinno==9.0.0" in argv

    def test_posix_flags(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(mod.platform, "system", lambda: "Linux")
        # Redirect log path to tmp.
        monkeypatch.setattr(
            mod, "_open_helper_log",
            lambda: (tmp_path / "log", open(tmp_path / "log", "ab", buffering=0)),
        )
        seen: dict = {}

        class _FakeProc:
            pid = 8888

        def fake_popen(argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            return _FakeProc()

        monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
        pid, log_path = mod._spawn_detached("9.0.0")
        assert pid == 8888
        assert log_path is not None
        kwargs = seen["kwargs"]
        assert kwargs.get("start_new_session") is True
        assert kwargs.get("close_fds") is True
        assert kwargs.get("stdin") == subprocess.DEVNULL
        # stdout is the log file handle, stderr redirects to stdout.
        assert kwargs.get("stderr") == subprocess.STDOUT
        # No Windows creationflags should be set on POSIX path.
        assert "creationflags" not in kwargs


# ── CLI smoke ─────────────────────────────────────────────────────


class TestCLISmoke:
    def test_dry_run_returns_zero(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from concinno.cli import self_update_cmd as cli_mod

        monkeypatch.setattr(mod, "get_latest_pypi_version", lambda *_a, **_k: "9.99.99")
        monkeypatch.setattr(mod, "is_concinno_in_use", lambda: (False, []))

        ns = MagicMock()
        ns.version = None
        ns.pre = False
        ns.skip_in_use_check = True
        ns.dry_run = True

        rc = cli_mod.cmd_self_update(ns)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Current:" in out
        assert "Latest:" in out
        assert "dry-run" in out

    def test_error_path_returns_one(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from concinno.cli import self_update_cmd as cli_mod

        monkeypatch.setattr(mod, "detect_pip_install_method", lambda: "editable")

        ns = MagicMock()
        ns.version = None
        ns.pre = False
        ns.skip_in_use_check = True
        ns.dry_run = False

        rc = cli_mod.cmd_self_update(ns)
        captured = capsys.readouterr()
        assert rc == 1
        assert "editable" in captured.err
