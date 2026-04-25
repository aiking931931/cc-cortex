"""Tests for ``concinno.core.subprocess_safe``."""

from __future__ import annotations

import subprocess
import sys

import pytest

from concinno.core import subprocess_safe


# ── Constants ──────────────────────────────────────────────────────────────


def test_create_no_window_constant_value():
    # 0x08000000 is the documented Windows ``CREATE_NO_WINDOW`` magic.
    # Hard-coded here so non-Windows test runs don't need stdlib symbol.
    assert subprocess_safe.CREATE_NO_WINDOW == 0x08000000


def test_flags_zero_on_non_windows():
    if sys.platform == "win32":
        pytest.skip("Windows-specific test")
    assert subprocess_safe.FLAGS == 0


def test_flags_create_no_window_on_windows():
    if sys.platform != "win32":
        pytest.skip("Non-Windows skips win32-only assertion")
    assert subprocess_safe.FLAGS == 0x08000000


# ── _inject_flags ──────────────────────────────────────────────────────────


def test_inject_flags_no_op_on_non_windows():
    if sys.platform == "win32":
        pytest.skip("Test asserts non-windows behaviour")
    out = subprocess_safe._inject_flags({})
    assert "creationflags" not in out


def test_inject_flags_adds_flag_on_windows_when_absent():
    if sys.platform != "win32":
        pytest.skip("Windows-only path")
    out = subprocess_safe._inject_flags({})
    assert out["creationflags"] == 0x08000000


def test_inject_flags_or_with_existing_creationflags_on_windows():
    if sys.platform != "win32":
        pytest.skip("Windows-only path")
    # Caller supplied DETACHED_PROCESS (0x00000008); we OR it, not replace.
    out = subprocess_safe._inject_flags({"creationflags": 0x00000008})
    assert out["creationflags"] == (0x08000000 | 0x00000008)


def test_inject_flags_skips_when_startupinfo_present():
    if sys.platform != "win32":
        pytest.skip("Windows-only path")
    si = subprocess.STARTUPINFO()
    out = subprocess_safe._inject_flags({"startupinfo": si})
    # Caller knows what they're doing — never override.
    assert "creationflags" not in out
    assert out["startupinfo"] is si


# ── run / Popen integration ────────────────────────────────────────────────


def test_run_returns_completed_process():
    cmd = [sys.executable, "-c", "print('hi')"]
    result = subprocess_safe.run(cmd, capture_output=True, text=True, timeout=10)
    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode == 0
    assert "hi" in result.stdout


def test_popen_returns_popen():
    cmd = [sys.executable, "-c", "print('hi')"]
    proc = subprocess_safe.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    out, _ = proc.communicate(timeout=10)
    assert proc.returncode == 0
    assert b"hi" in out


def test_run_forwards_kwargs():
    # cwd, env, timeout, etc must pass through unchanged.
    cmd = [sys.executable, "-c", "import os; print(os.getcwd())"]
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        result = subprocess_safe.run(
            cmd, capture_output=True, text=True, cwd=tmp, timeout=10,
        )
        # Resolve symlinks both sides — Windows tmp paths may differ in case.
        import os
        assert os.path.realpath(result.stdout.strip()).lower() == \
            os.path.realpath(tmp).lower()


def test_run_does_not_clobber_explicit_creationflags(monkeypatch):
    if sys.platform != "win32":
        pytest.skip("Windows-only check")

    captured: dict = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args=args[0], returncode=0)

    monkeypatch.setattr(subprocess_safe._subprocess, "run", fake_run)
    subprocess_safe.run(
        ["echo", "x"], creationflags=0x00000008,
    )
    # Both bits set: caller's DETACHED_PROCESS plus our CREATE_NO_WINDOW.
    assert captured["creationflags"] == (0x08000000 | 0x00000008)


def test_popen_skips_flag_when_startupinfo_supplied(monkeypatch):
    if sys.platform != "win32":
        pytest.skip("Windows-only check")

    captured: dict = {}

    class _FakePopen:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(subprocess_safe._subprocess, "Popen", _FakePopen)
    si = subprocess.STARTUPINFO()
    subprocess_safe.Popen(["echo", "x"], startupinfo=si)
    assert "creationflags" not in captured
    assert captured["startupinfo"] is si


def test_module_dunder_all():
    expected = {"run", "Popen", "CREATE_NO_WINDOW", "FLAGS"}
    assert set(subprocess_safe.__all__) == expected
