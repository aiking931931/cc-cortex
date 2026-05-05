"""pytest for windows module (in-process winops).

Strategy:
- Test all PURE Python logic (fs / registry / scrape helpers / state / aliases).
- Mock pyautogui / win32gui / notifications for INTERACTIVE functions so tests
  don't actually move the mouse or spawn GUI on CI.
- Smoke-test actual I/O on safe surfaces: clipboard roundtrip, screenshot bytes,
  process_list returns non-empty, HKCU test-only registry rt.

Run:
    pytest e:/Cursor/.claude/skills/windows/test_windows.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# make `import windows` resolve to the sibling windows.py, not stdlib
_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
import windows as w  # noqa: E402


# ----------------------------------------------------------------------
# Singleton state
# ----------------------------------------------------------------------
def test_state_singleton_has_labels_and_last_snapshot():
    assert hasattr(w.STATE, "labels")
    assert hasattr(w.STATE, "last_snapshot")
    assert isinstance(w.STATE.labels, dict)


def test_state_labels_clear_and_set():
    w.STATE.labels.clear()
    w.STATE.labels[42] = (100, 200)
    assert w.STATE.labels[42] == (100, 200)
    w.STATE.labels.clear()
    assert w.STATE.labels == {}


# ----------------------------------------------------------------------
# Registry path parsing (pure)
# ----------------------------------------------------------------------
def test_parse_reg_path_hkcu_abbrev():
    import winreg

    hive, sub = w._parse_reg_path(r"HKCU:\Software\TestKey")
    assert hive == winreg.HKEY_CURRENT_USER
    assert sub == r"Software\TestKey"


def test_parse_reg_path_full_hive_name():
    import winreg

    hive, sub = w._parse_reg_path(r"HKEY_LOCAL_MACHINE\System\Foo")
    assert hive == winreg.HKEY_LOCAL_MACHINE
    assert sub == r"System\Foo"


def test_parse_reg_path_forward_slashes():
    import winreg

    hive, sub = w._parse_reg_path("HKCU:/Software/Test")
    assert hive == winreg.HKEY_CURRENT_USER
    assert sub == r"Software\Test"


def test_parse_reg_path_unknown_hive_raises():
    with pytest.raises(ValueError, match="unknown hive"):
        w._parse_reg_path(r"BOGUS:\Foo")


# ----------------------------------------------------------------------
# Registry real HKCU roundtrip (safe test key)
# ----------------------------------------------------------------------
_TEST_REG = r"HKCU:\Software\AiKing\windows_skill_test"


def test_registry_set_get_list_delete_roundtrip():
    # set String
    assert w.registry("set", _TEST_REG, name="hello", value="世界") is True
    assert w.registry("get", _TEST_REG, name="hello") == "世界"

    # set DWord
    w.registry("set", _TEST_REG, name="count", value=42, type="DWord")
    assert w.registry("get", _TEST_REG, name="count") == 42

    # list
    items = w.registry("list", _TEST_REG)
    names = {it["name"] for it in items}
    assert {"hello", "count"} <= names

    # delete single value
    w.registry("delete", _TEST_REG, name="hello", dangerous=True)
    items = w.registry("list", _TEST_REG)
    assert "hello" not in {it["name"] for it in items}

    # cleanup: delete the whole test key
    w.registry("delete", _TEST_REG, name="count", dangerous=True)
    w.registry("delete", _TEST_REG, dangerous=True)


# ----------------------------------------------------------------------
# FileSystem (tmp_path isolated)
# ----------------------------------------------------------------------
def test_fs_write_read_roundtrip(tmp_path):
    p = tmp_path / "nested" / "file.txt"
    n = w.fs_write(p, "line1\nline2\n")
    assert n > 0
    assert w.fs_read(p) == "line1\nline2\n"


def test_fs_write_append(tmp_path):
    p = tmp_path / "log.txt"
    w.fs_write(p, "A\n")
    w.fs_write(p, "B\n", append=True)
    assert w.fs_read(p) == "A\nB\n"


def test_fs_read_offset_limit(tmp_path):
    p = tmp_path / "multi.txt"
    w.fs_write(p, "a\nb\nc\nd\ne\n")
    assert w.fs_read(p, offset=2, limit=2) == "c\nd\n"


def test_fs_list_glob_and_recursive(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "b.md").write_text("y")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.txt").write_text("z")

    flat = w.fs_list(tmp_path, pattern="*.txt")
    assert len(flat) == 1 and flat[0].endswith("a.txt")

    deep = w.fs_list(tmp_path, pattern="*.txt", recursive=True)
    assert len(deep) == 2


def test_fs_list_skips_hidden_by_default(tmp_path):
    (tmp_path / ".secret").write_text("x")
    (tmp_path / "public.txt").write_text("y")
    names = [Path(p).name for p in w.fs_list(tmp_path)]
    assert ".secret" not in names
    assert "public.txt" in names
    names_hidden = [Path(p).name for p in w.fs_list(tmp_path, show_hidden=True)]
    assert ".secret" in names_hidden


def test_fs_search_content_match(tmp_path):
    (tmp_path / "a.py").write_text("import numpy as np\n")
    (tmp_path / "b.py").write_text("import pandas\n")
    (tmp_path / "c.py").write_text("print('hello')\n")

    hits = w.fs_search(tmp_path, "*.py", content="import numpy")
    assert len(hits) == 1 and hits[0].endswith("a.py")

    all_py = w.fs_search(tmp_path, "*.py", content=None)
    assert len(all_py) == 3


def test_fs_copy_move_delete(tmp_path):
    src = tmp_path / "src.txt"
    src.write_text("hello")
    dst = tmp_path / "dst.txt"

    w.fs_copy(src, dst)
    assert dst.read_text() == "hello"
    assert src.exists()

    moved = tmp_path / "moved.txt"
    w.fs_move(dst, moved)
    assert not dst.exists() and moved.exists()

    w.fs_delete(moved, dangerous=True)
    assert not moved.exists()


def test_fs_copy_overwrite_flag(tmp_path):
    src = tmp_path / "s.txt"
    dst = tmp_path / "d.txt"
    src.write_text("new")
    dst.write_text("old")
    with pytest.raises(FileExistsError):
        w.fs_copy(src, dst)
    w.fs_copy(src, dst, overwrite=True)
    assert dst.read_text() == "new"


def test_fs_delete_dir_tree(tmp_path):
    d = tmp_path / "to_kill"
    d.mkdir()
    (d / "inner.txt").write_text("x")
    w.fs_delete(d, dangerous=True)
    assert not d.exists()


def test_fs_info_shapes(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("hi")
    info = w.fs_info(p)
    assert info["exists"] is True
    assert info["is_file"] is True and info["is_dir"] is False
    assert info["size"] == 2

    missing = w.fs_info(tmp_path / "nope")
    assert missing == {"exists": False}


# ----------------------------------------------------------------------
# Scrape helpers (pure)
# ----------------------------------------------------------------------
def test_filter_by_query_no_query_returns_full():
    assert w._filter_by_query("abc\n123", None) == "abc\n123"


def test_filter_by_query_hit_returns_context_window():
    text = "\n".join(f"line{i}" for i in range(10))
    out = w._filter_by_query(text, "line5")
    assert "line5" in out
    # context ±2 lines → line3..line7
    assert "line3" in out and "line7" in out


def test_filter_by_query_case_insensitive():
    out = w._filter_by_query("Hello World\nBye", "hello")
    assert "Hello World" in out


def test_filter_by_query_miss_returns_full():
    out = w._filter_by_query("a\nb\nc", "xyz_not_present")
    assert out == "a\nb\nc"


# ----------------------------------------------------------------------
# Clipboard roundtrip (real pyperclip)
# ----------------------------------------------------------------------
def test_clipboard_roundtrip_ascii():
    original = w.clipboard_get()
    try:
        w.clipboard_set("winops-test-ascii-123")
        assert w.clipboard_get() == "winops-test-ascii-123"
    finally:
        if original:
            w.clipboard_set(original)


def test_clipboard_roundtrip_cjk():
    original = w.clipboard_get()
    try:
        payload = "繁體中文測試 🎯"
        w.clipboard_set(payload)
        assert w.clipboard_get() == payload
    finally:
        if original:
            w.clipboard_set(original)


# ----------------------------------------------------------------------
# Screenshot returns valid PNG bytes
# ----------------------------------------------------------------------
def test_screenshot_returns_bytes():
    png = w.screenshot(scale=0.2)
    assert isinstance(png, bytes)
    # PNG magic signature
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_screenshot_writes_file(tmp_path):
    out = tmp_path / "shot.png"
    path = w.screenshot(path=out, scale=0.2)
    assert Path(path).exists()
    assert Path(path).stat().st_size > 1000  # non-trivial PNG


# ----------------------------------------------------------------------
# Process list returns something
# ----------------------------------------------------------------------
def test_process_list_non_empty():
    procs = w.process_list(limit=5)
    assert len(procs) > 0
    assert all({"pid", "name", "memory_mb"} <= set(p) for p in procs)


def test_process_list_sort_by_memory_descending():
    procs = w.process_list(sort_by="memory", limit=10)
    mem_values = [p["memory_mb"] for p in procs]
    assert mem_values == sorted(mem_values, reverse=True)


def test_process_list_sort_by_name_ascending():
    procs = w.process_list(sort_by="name", limit=10)
    names = [p["name"].lower() for p in procs]
    assert names == sorted(names)


# ----------------------------------------------------------------------
# Window list returns live data
# ----------------------------------------------------------------------
def test_window_list_returns_list():
    out = w.window_list()
    assert isinstance(out, list)
    # pytest runner itself is a process but may not have a visible window on
    # headless; don't enforce len > 0, just shape.
    for item in out:
        assert {"hwnd", "title", "bounds"} <= set(item)
        assert len(item["bounds"]) == 4


# ----------------------------------------------------------------------
# PowerShell wrapper
# ----------------------------------------------------------------------
def test_powershell_echo_roundtrip():
    r = w.powershell("Write-Output 'winops-ps-test'", dangerous=True)
    assert r["status"] == "ok"
    assert r["returncode"] == 0
    assert "winops-ps-test" in r["stdout"]


def test_powershell_nonzero_exit():
    r = w.powershell("exit 7", dangerous=True)
    assert r["status"] == "error"
    assert r["returncode"] == 7


def test_powershell_timeout():
    r = w.powershell("Start-Sleep -Seconds 5", timeout=1, dangerous=True)
    assert r["status"] == "timeout"


# ----------------------------------------------------------------------
# Shortcut key-alias resolution (mocked pyautogui to avoid firing keys)
# ----------------------------------------------------------------------
def test_shortcut_alias_normalization():
    with patch.object(w, "pyautogui", create=True):
        pass  # marker
    fake = MagicMock()
    with patch.dict(sys.modules, {"pyautogui": fake}):
        # force reload of pyautogui reference inside w.shortcut
        w.shortcut("ctrl+c")
        fake.hotkey.assert_called_with("ctrl", "c")
        fake.reset_mock()

        w.shortcut("win+d")
        fake.hotkey.assert_called_with("winleft", "d")
        fake.reset_mock()

        w.shortcut("alt-tab")
        fake.hotkey.assert_called_with("alt", "tab")
        fake.reset_mock()

        w.shortcut("control+shift+esc")
        fake.hotkey.assert_called_with("ctrl", "shift", "escape")


# ----------------------------------------------------------------------
# type_text IME-safe routing (CJK goes through clipboard paste)
# ----------------------------------------------------------------------
def test_type_text_ascii_uses_typewrite():
    fake_pg = MagicMock()
    with patch.dict(sys.modules, {"pyautogui": fake_pg}):
        w.type_text("hello world", ime_safe=True)
    fake_pg.typewrite.assert_called_once()
    fake_pg.hotkey.assert_not_called()


def test_type_text_cjk_uses_clipboard_paste():
    fake_pg = MagicMock()
    with patch.dict(sys.modules, {"pyautogui": fake_pg}):
        # preserve real clipboard
        prev = w.clipboard_get()
        try:
            w.type_text("你好", ime_safe=True)
        finally:
            w.clipboard_set(prev if prev else "")
    fake_pg.hotkey.assert_any_call("ctrl", "v")
    fake_pg.typewrite.assert_not_called()


def test_type_text_ime_safe_false_forces_typewrite():
    fake_pg = MagicMock()
    with patch.dict(sys.modules, {"pyautogui": fake_pg}):
        w.type_text("你好", ime_safe=False)
    fake_pg.typewrite.assert_called_once()
    fake_pg.hotkey.assert_not_called()


def test_type_text_press_enter():
    fake_pg = MagicMock()
    with patch.dict(sys.modules, {"pyautogui": fake_pg}):
        w.type_text("x", press_enter=True)
    fake_pg.press.assert_called_with("enter")


# ----------------------------------------------------------------------
# click label lookup
# ----------------------------------------------------------------------
def test_click_label_raises_when_missing():
    w.STATE.labels.clear()
    fake_pg = MagicMock()
    with patch.dict(sys.modules, {"pyautogui": fake_pg}):
        with pytest.raises(KeyError, match="label 5"):
            w.click(label=5)
    fake_pg.click.assert_not_called()


def test_click_label_uses_cached_coord():
    w.STATE.labels.clear()
    w.STATE.labels[7] = (123, 456)
    fake_pg = MagicMock()
    with patch.dict(sys.modules, {"pyautogui": fake_pg}):
        xy = w.click(label=7)
    assert xy == (123, 456)
    fake_pg.click.assert_called_with(
        123, 456, clicks=1, interval=0.05, button="left"
    )


def test_click_requires_xy_or_label():
    fake_pg = MagicMock()
    with patch.dict(sys.modules, {"pyautogui": fake_pg}):
        with pytest.raises(ValueError, match="provide"):
            w.click()


# ----------------------------------------------------------------------
# scroll direction routing
# ----------------------------------------------------------------------
def test_scroll_vertical_down():
    fake_pg = MagicMock()
    with patch.dict(sys.modules, {"pyautogui": fake_pg}):
        w.scroll(3, direction="down")
    fake_pg.scroll.assert_called_with(-3)
    fake_pg.hscroll.assert_not_called()


def test_scroll_horizontal_right():
    fake_pg = MagicMock()
    with patch.dict(sys.modules, {"pyautogui": fake_pg}):
        w.scroll(5, direction="right")
    fake_pg.hscroll.assert_called_with(5)
    fake_pg.scroll.assert_not_called()


# ----------------------------------------------------------------------
# Process kill safety (mocked psutil — don't actually kill)
# ----------------------------------------------------------------------
def test_process_kill_requires_pid_or_name():
    with pytest.raises(ValueError, match="pid= or name="):
        w.process_kill()


# ----------------------------------------------------------------------
# wait delegates to time.sleep
# ----------------------------------------------------------------------
def test_wait_delegates_to_sleep():
    with patch("time.sleep") as mock_sleep:
        w.wait(0.5)
    mock_sleep.assert_called_with(0.5)
