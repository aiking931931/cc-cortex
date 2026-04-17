"""Headless integration test — truly background, zero user interference.

Uses Win32 CreateDesktopW + CreateProcessW (STARTUPINFO.lpDesktop) to spawn
a child Python process on a HIDDEN desktop. The child starts a PowerShell
WinForms window (classic Win32, inherits the hidden desktop), performs UIA
SetValue + readback + PrintWindow screenshot, writes JSON result.

User sees nothing. No focus steal. No mouse/keyboard events. This is
playwright-headless-equivalent on Windows, implemented via stable Win32
API (available since NT 3.5), not the drifty Win11 Virtual Desktop COM.

Runs on every `pytest` invocation because it's interference-free.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

import pytest

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
import windows as w  # noqa: E402


@pytest.fixture(scope="module")
def headless_result() -> dict:
    """Run the hidden-desktop worker once and cache the JSON result."""
    worker = _HERE / "hidden_worker.py"
    assert worker.exists(), f"worker missing: {worker}"

    out = Path(tempfile.NamedTemporaryFile(
        suffix=".json", delete=False,
    ).name)
    cmd = f'"{sys.executable}" "{worker}" "{out}"'

    t0 = time.time()
    rv = w.run_in_hidden_desktop(cmd, timeout_ms=60000)
    elapsed = time.time() - t0

    assert not rv["timed_out"], f"hidden desktop timed out after {elapsed:.1f}s"
    assert rv["returncode"] == 0, (
        f"worker exit code {rv['returncode']}; probably worker crashed"
    )
    assert out.exists() and out.stat().st_size > 0, "worker produced no JSON"

    result = json.loads(out.read_text(encoding="utf-8"))
    result["_elapsed_s"] = elapsed
    return result


def test_headless_worker_succeeded(headless_result):
    assert headless_result["success"] is True, (
        f"worker reported failure: "
        f"{headless_result.get('error')} | {headless_result.get('traceback')}"
    )


def test_headless_uia_set_value_and_readback_ascii(headless_result):
    """UIA ValuePattern.SetValue → readback preserves ASCII."""
    assert headless_result["ascii_match"] is True
    assert "HEADLESS" in headless_result["payload_read"]


def test_headless_uia_set_value_and_readback_cjk(headless_result):
    """UIA ValuePattern.SetValue → readback preserves CJK (no IME needed)."""
    assert headless_result["cjk_match"] is True
    assert "繁體中文" in headless_result["payload_read"]


def test_headless_printwindow_captured_hidden_desktop(headless_result):
    """PrintWindow can capture windows on a hidden Win32 desktop."""
    assert "screenshot_bytes" in headless_result
    assert headless_result["screenshot_bytes"] > 0
    shot = Path(headless_result["screenshot_path"])
    assert shot.exists()
    data = shot.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a valid PNG"


def test_headless_workflow_under_15_seconds(headless_result):
    """Whole headless cycle (desktop + PS WinForms + UIA + shot) is fast."""
    assert headless_result["_elapsed_s"] < 15.0, (
        f"hidden-desktop flow took {headless_result['_elapsed_s']:.1f}s "
        "(expected <15s); spawn overhead has regressed"
    )


def test_headless_steps_trace_present(headless_result):
    steps = headless_result.get("steps", [])
    joined = " | ".join(steps)
    # Every critical step must have been logged
    assert "spawned PS" in joined
    assert "SetValue via ValuePattern" in joined
    assert "PrintWindow" in joined
