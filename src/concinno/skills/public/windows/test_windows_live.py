"""Live UI integration tests — actually launch apps, click, type, screenshot.

These cost real foreground focus (mouse/keyboard moves). Opt-in only:

    WINDOWS_LIVE=1 pytest .claude/skills/windows/test_windows_live.py -v

Skipped by default so CI and routine `pytest` runs don't steal your focus.

Strategy:
- Each test is self-contained: launches app → interacts → asserts → cleans up.
- Uses clipboard & snapshot for assertion, NOT just "did it run without crash".
- If you want true background (no stolen focus): run under a separate
  Windows virtual desktop (pyvda) or an RDP session that stays logged in
  after disconnect.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
import windows as w  # noqa: E402

pytestmark = pytest.mark.skipif(
    os.environ.get("WINDOWS_LIVE") != "1",
    reason="live UI tests disabled; set WINDOWS_LIVE=1 to enable",
)


def _kill_notepad() -> None:
    try:
        w.process_kill(name="notepad.exe", force=True)
    except Exception:
        pass
    time.sleep(0.3)


def test_notepad_type_copy_screenshot_roundtrip(tmp_path):
    """Real flow: launch Notepad → type CJK → Ctrl+A+C → assert clipboard.

    Then screenshot and assert non-empty PNG. Finally Alt+F4 + discard.
    """
    _kill_notepad()
    original_clip = w.clipboard_get()

    try:
        # Launch
        w.app("notepad", mode="launch")
        time.sleep(1.2)  # notepad takes time to present the edit area

        # Verify window exists
        wins = [win for win in w.window_list()
                if "notepad" in win["title"].lower() or "記事本" in win["title"]]
        assert len(wins) >= 1, f"notepad window not found; windows={[w_['title'] for w_ in w.window_list()]}"

        # Click into the window to ensure focus (title bar center -> then body)
        win = wins[0]
        left, top, right, bottom = win["bounds"]
        # click the body, not the title bar
        body_x = (left + right) // 2
        body_y = top + int((bottom - top) * 0.5)
        w.click(body_x, body_y)
        time.sleep(0.2)

        # Type mixed ASCII + CJK (IME-safe paste path)
        payload = "windows-skill live test 繁體中文 1234"
        w.clipboard_set("")  # clear
        time.sleep(0.05)
        w.type_text(payload, ime_safe=True)
        time.sleep(0.3)

        # Select all + copy
        w.shortcut("ctrl+a")
        time.sleep(0.15)
        w.shortcut("ctrl+c")
        time.sleep(0.25)

        got = w.clipboard_get()
        assert payload in got, f"typed text not in clipboard: got={got!r}"

        # Screenshot and verify PNG header + non-trivial size
        shot = tmp_path / "notepad.png"
        path = w.screenshot(path=shot, scale=0.5)
        assert Path(path).exists()
        data = Path(path).read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        assert len(data) > 5000, f"png suspiciously small: {len(data)}"

    finally:
        # Alt+F4 closes, then press 'n' (Don't Save) if prompt appears
        w.shortcut("alt+f4")
        time.sleep(0.4)
        w.shortcut("alt+n")  # 英文: Don't Save ; 中文: 不存檔
        time.sleep(0.2)
        # Last resort: kill
        _kill_notepad()
        if original_clip:
            w.clipboard_set(original_clip)


def test_snapshot_returns_labeled_interactive_elements():
    """Launch Calculator, snapshot, assert we got labeled buttons."""
    try:
        w.app("calc", mode="launch")
        time.sleep(1.5)

        snap = w.snapshot(active_window_only=True)
        assert snap["count"] > 0, "snapshot returned no elements"
        # At least one label → coord mapping populated
        assert len(w.STATE.labels) > 0

        # Calculator should have numeric buttons (name "5" or similar)
        # Accept either English ("Five") or Chinese locale names
        found_digit = any(
            e["name"].strip() in "0123456789" or
            e["type"] == "ButtonControl"
            for e in snap["elements"]
        )
        assert found_digit, f"no button-looking element; elements={snap['elements'][:5]}"
    finally:
        w.process_kill(name="CalculatorApp.exe", force=True)
        w.process_kill(name="Calculator.exe", force=True)
        time.sleep(0.3)


def test_fs_registry_live_combined_flow(tmp_path):
    """Pure-data live flow: no GUI. Confirms the skill works even under
    background / no-foreground scenarios."""
    # write → read → search
    (tmp_path / "a.py").write_text("import pandas\nx = 1\n")
    (tmp_path / "b.py").write_text("import numpy\ny = 2\n")
    hits = w.fs_search(tmp_path, "*.py", content="pandas")
    assert len(hits) == 1

    # registry → set → snapshot → delete
    key = r"HKCU:\Software\AiKing\windows_live_test"
    w.registry("set", key, name="live", value="ok")
    assert w.registry("get", key, name="live") == "ok"
    w.registry("delete", key, name="live")
    w.registry("delete", key)

    # screenshot fresh (confirms mss works even without foreground window change)
    png = w.screenshot(scale=0.3)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
