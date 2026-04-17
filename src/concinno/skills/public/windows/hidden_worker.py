"""Runs inside a hidden Win32 desktop (via run_in_hidden_desktop).

Spawns a PowerShell WinForms Form (classic Win32, stays in hidden desktop),
performs UIA SetValue + readback + PrintWindow screenshot, writes JSON result.

Parent orchestrator reads the JSON to verify everything worked headlessly.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

import windows as w  # noqa: E402
import uiautomation as uia  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402

PS_SCRIPT = r"""
Add-Type -AssemblyName System.Windows.Forms
$form = New-Object System.Windows.Forms.Form
$form.Text = 'WinOpsHeadlessTarget'
$form.Width = 500
$form.Height = 200
$tb = New-Object System.Windows.Forms.TextBox
$tb.Name = 'MainTextBox'
$tb.Width = 450
$tb.Top = 50
$tb.Left = 20
$form.Controls.Add($tb)
[System.Windows.Forms.Application]::Run($form)
"""


def run(output_json: str) -> None:
    result: dict = {"steps": [], "success": False}
    proc = None
    hwnd = None
    try:
        proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden",
             "-Command", PS_SCRIPT],
        )
        result["steps"].append(f"spawned PS pid={proc.pid}")

        # Wait for the WinForms window to appear
        for _ in range(60):
            hits: list[int] = []

            def _enum(h: int, _: object) -> None:
                title = win32gui.GetWindowText(h)
                if "WinOpsHeadlessTarget" in title and win32gui.IsWindowVisible(h):
                    hits.append(h)

            win32gui.EnumWindows(_enum, None)
            if hits:
                hwnd = hits[0]
                break
            time.sleep(0.3)
        if not hwnd:
            raise TimeoutError("WinForms window did not appear in 18s")
        result["steps"].append(f"hwnd={hwnd}")

        # UIA find + SetValue
        win = uia.WindowControl(searchDepth=1, Name="WinOpsHeadlessTarget")
        if not win.Exists(5.0):
            raise RuntimeError("UIA window not found")
        edit = win.EditControl(searchDepth=99)
        if not edit.Exists(3.0):
            raise RuntimeError("UIA edit control not found")
        result["steps"].append(f"UIA edit: type={edit.ControlTypeName}")

        payload = "HEADLESS 繁體中文 OK 1234"
        vp = edit.GetPattern(uia.PatternId.ValuePattern)
        if vp is None:
            raise RuntimeError("no ValuePattern on edit control")
        vp.SetValue(payload)
        result["steps"].append("SetValue via ValuePattern (0 SendInput)")
        time.sleep(0.4)

        # Readback
        got = vp.Value
        result["payload_sent"] = payload
        result["payload_read"] = got
        result["cjk_match"] = "繁體中文" in got
        result["ascii_match"] = "HEADLESS" in got
        result["steps"].append(f"readback ok len={len(got)}")

        # PrintWindow screenshot (from hidden desktop)
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp.close()
        w.screenshot_window(hwnd, path=tmp.name)
        result["screenshot_path"] = tmp.name
        result["screenshot_bytes"] = os.path.getsize(tmp.name)
        result["steps"].append(f"PrintWindow -> {result['screenshot_bytes']} bytes")

        # Close form
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        time.sleep(0.5)
        result["success"] = True
    except Exception as e:  # noqa: BLE001
        result["error"] = f"{type(e).__name__}: {e}"
        result["traceback"] = traceback.format_exc()
    finally:
        if proc is not None:
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        with open(output_json, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: hidden_worker.py <output.json>", file=sys.stderr)
        sys.exit(2)
    run(sys.argv[1])
