"""Unit tests for ``concinno.marketplace.installer``.

Covers package-name validation, version validation, subprocess wiring,
timeout handling, and concurrent-install lock semantics. The real
``subprocess.run`` is never invoked — every test injects a stub
runner.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from concinno.marketplace.installer import (
    InstallError,
    install_pkg,
    uninstall_pkg,
)


class _FakeProc:
    def __init__(self, rc: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


def _ok_runner(args: list[str], **_kw: Any) -> _FakeProc:
    return _FakeProc(rc=0, stdout="installed ok", stderr="")


def _fail_runner(args: list[str], **_kw: Any) -> _FakeProc:
    return _FakeProc(rc=1, stdout="", stderr="boom")


def _timeout_runner(args: list[str], **_kw: Any) -> _FakeProc:
    raise subprocess.TimeoutExpired(cmd=args, timeout=180)


def test_install_rejects_arbitrary_package(tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))  # type: ignore[arg-type]
    with pytest.raises(InstallError, match="concinno-skills"):
        install_pkg("requests", runner=_ok_runner, skip_lock=True)


def test_install_rejects_shell_metacharacter(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))  # type: ignore[arg-type]
    with pytest.raises(InstallError):
        install_pkg("concinno-skills-x; rm -rf /", runner=_ok_runner,
                    skip_lock=True)


def test_install_rejects_bad_version(tmp_path: Path,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))  # type: ignore[arg-type]
    with pytest.raises(InstallError, match="version"):
        install_pkg("concinno-skills-memory", version="1.0; cat /etc/passwd",
                    runner=_ok_runner, skip_lock=True)


def test_install_happy_path_invokes_runner(tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))  # type: ignore[arg-type]
    captured = {}

    def runner(args: list[str], **kw: Any) -> _FakeProc:
        captured["args"] = args
        captured["kw"] = kw
        return _FakeProc(rc=0)

    result = install_pkg("concinno-skills-memory", version="0.2.0",
                         runner=runner, skip_lock=True)
    assert result.ok is True
    args = captured["args"]
    # No shell injection: args is a list, no shell=True.
    assert isinstance(args, list)
    assert "concinno-skills-memory==0.2.0" in args
    assert "pip" in args
    assert captured["kw"].get("shell") is False


def test_install_failure_propagates_stderr(tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))  # type: ignore[arg-type]
    result = install_pkg("concinno-skills-memory", runner=_fail_runner,
                         skip_lock=True)
    assert result.ok is False
    assert result.stderr == "boom"
    assert result.return_code == 1


def test_install_timeout_returns_failure(tmp_path: Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))  # type: ignore[arg-type]
    result = install_pkg("concinno-skills-memory", runner=_timeout_runner,
                         skip_lock=True)
    assert result.ok is False
    assert "timed out" in result.stderr


def test_uninstall_happy_path(tmp_path: Path,
                              monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))  # type: ignore[arg-type]
    captured = {}

    def runner(args: list[str], **kw: Any) -> _FakeProc:
        captured["args"] = args
        return _FakeProc(rc=0)

    result = uninstall_pkg("concinno-skills-memory", runner=runner,
                           skip_lock=True)
    assert result.ok is True
    assert "uninstall" in captured["args"]
    assert "concinno-skills-memory" in captured["args"]


def test_lock_blocks_concurrent_install(tmp_path: Path,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))  # type: ignore[arg-type]
    # Pre-create the lock dir to simulate "another install in progress".
    lock_dir = tmp_path / ".concinno" / "marketplace.lock"
    lock_dir.parent.mkdir(parents=True, exist_ok=True)
    lock_dir.mkdir()
    with pytest.raises(InstallError, match="in progress"):
        install_pkg("concinno-skills-memory", runner=_ok_runner)


def test_stale_lock_is_recovered(tmp_path: Path,
                                 monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))  # type: ignore[arg-type]
    lock_dir = tmp_path / ".concinno" / "marketplace.lock"
    lock_dir.parent.mkdir(parents=True, exist_ok=True)
    lock_dir.mkdir()
    # Backdate mtime past the stale TTL (60s).
    import os
    old = lock_dir.stat().st_mtime - 3600
    os.utime(lock_dir, (old, old))
    # Should succeed via stale-lock recovery.
    result = install_pkg("concinno-skills-memory", runner=_ok_runner)
    assert result.ok is True
