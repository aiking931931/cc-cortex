"""windows — in-process Windows automation.

Drop-in replacement for windows-mcp (18 tools) with zero MCP overhead.
Philosophy (from aegis-tool-stack Skill):
    in-process Python > subprocess > MCP.
    Code-writing > atomic tool-call chains.

Primary usage (LLM writes a .py or `python -c`):

    import os, sys; sys.path.insert(0, os.path.expanduser(r"~/.claude/skills/windows"))
    import windows as w
    w.app("notepad", mode="launch")
    w.wait(0.5)
    w.type_text("Hello 繁體中文")
    png = w.screenshot(scale=0.5)
    snap = w.snapshot()           # populates w.STATE.labels
    w.click(label=3)

CLI fallback:
    python windows.py screenshot --out out.png
    python windows.py click 100 200 --clicks 2
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Literal

__all__ = [
    "STATE", "screenshot", "screenshot_window",
    "click", "move", "scroll",
    "type_text", "shortcut",
    "clipboard_get", "clipboard_set",
    "app", "window_list",
    "process_list", "process_kill",
    "registry",
    "powershell",
    "fs_read", "fs_write", "fs_list", "fs_search",
    "fs_copy", "fs_move", "fs_delete", "fs_info",
    "notify",
    "scrape",
    "snapshot",
    "multi_click", "multi_edit",
    "wait",
    "uia_find", "uia_click", "uia_set_value", "uia_get_value",
    "run_in_hidden_desktop",
]

def _guard_destructive(op_name: str, dangerous: bool = False) -> None:
    if dangerous:
        return
    if os.environ.get("WINDOWS_ALLOW_DESTRUCTIVE") == "1":
        return
    raise PermissionError(
        f"{op_name}() is destructive. "
        f"Set env WINDOWS_ALLOW_DESTRUCTIVE=1 "
        f"or pass dangerous=True."
    )


# ---------------------------------------------------------------------------
# Singleton state (daemon-style label → coord cache)
# ---------------------------------------------------------------------------
class _State:
    labels: dict[int, tuple[int, int]]
    last_snapshot: dict | None

    def __init__(self) -> None:
        self.labels = {}
        self.last_snapshot = None


STATE = _State()


# ---------------------------------------------------------------------------
# Screenshot (mss — zero-copy, 10× faster than PIL.ImageGrab)
# ---------------------------------------------------------------------------
def screenshot(
    path: str | Path | None = None,
    *,
    display: int = 1,
    region: tuple[int, int, int, int] | None = None,
    scale: float = 1.0,
) -> bytes | str:
    """Capture screen.

    display: 1 = primary, 2 = secondary, ... (mss convention)
    region: (left, top, width, height) in physical pixels, None = full monitor
    scale: <1.0 downscales to save multimodal-LLM tokens
    Returns str path if `path` given, else PNG bytes.
    """
    import mss
    from PIL import Image

    with mss.mss() as sct:
        if region is not None:
            bbox = {"left": region[0], "top": region[1],
                    "width": region[2], "height": region[3]}
        else:
            bbox = sct.monitors[display]
        raw = sct.grab(bbox)
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

    if scale != 1.0:
        w, h = img.size
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    if path is not None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        img.save(str(path), "PNG")
        return str(path)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Mouse
# ---------------------------------------------------------------------------
def click(
    x: int | None = None,
    y: int | None = None,
    *,
    label: int | None = None,
    button: Literal["left", "right", "middle"] = "left",
    clicks: int = 1,
    interval: float = 0.05,
) -> tuple[int, int]:
    """Click at (x,y) or at a label from last snapshot()."""
    import pyautogui

    if label is not None:
        if label not in STATE.labels:
            raise KeyError(
                f"label {label} not found; call snapshot() first "
                f"(available: {sorted(STATE.labels)[:10]}...)"
            )
        x, y = STATE.labels[label]
    if x is None or y is None:
        raise ValueError("provide (x, y) OR label=N")
    pyautogui.click(x, y, clicks=clicks, interval=interval, button=button)
    return x, y


def move(
    x: int,
    y: int,
    *,
    drag: bool = False,
    duration: float = 0.1,
) -> None:
    import pyautogui

    if drag:
        pyautogui.dragTo(x, y, duration=duration, button="left")
    else:
        pyautogui.moveTo(x, y, duration=duration)


def scroll(
    amount: int,
    *,
    direction: Literal["up", "down", "left", "right"] = "down",
    x: int | None = None,
    y: int | None = None,
) -> None:
    import pyautogui

    if x is not None and y is not None:
        pyautogui.moveTo(x, y)
    sign = 1 if direction in ("up", "right") else -1
    if direction in ("left", "right"):
        pyautogui.hscroll(sign * abs(amount))
    else:
        pyautogui.scroll(sign * abs(amount))


# ---------------------------------------------------------------------------
# Keyboard (IME-safe for CJK)
# ---------------------------------------------------------------------------
def type_text(
    text: str,
    *,
    interval: float = 0.0,
    ime_safe: bool = True,
    press_enter: bool = False,
) -> None:
    """Type text. Non-ASCII defaults to clipboard-paste (CJK-safe, IME-safe)."""
    import pyautogui

    is_ascii = text.isascii()
    if (not is_ascii) and ime_safe:
        old = clipboard_get()
        try:
            clipboard_set(text)
            time.sleep(0.03)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.05)
        finally:
            if old is not None:
                clipboard_set(old)
    else:
        pyautogui.typewrite(text, interval=interval)
    if press_enter:
        pyautogui.press("enter")


_KEY_ALIAS = {
    "win": "winleft", "windows": "winleft", "cmd": "winleft",
    "control": "ctrl", "option": "alt", "esc": "escape",
    "return": "enter",
}


def shortcut(combo: str) -> None:
    """Press a key combo like 'ctrl+c' or 'win+d' or 'alt+tab'."""
    import pyautogui

    parts = combo.replace("-", "+").split("+")
    keys = [_KEY_ALIAS.get(k.strip().lower(), k.strip().lower()) for k in parts]
    pyautogui.hotkey(*keys)


# ---------------------------------------------------------------------------
# Clipboard
# ---------------------------------------------------------------------------
def clipboard_get() -> str:
    import pyperclip

    return pyperclip.paste()


def clipboard_set(text: str) -> None:
    import pyperclip

    pyperclip.copy(text)


# ---------------------------------------------------------------------------
# App / window
# ---------------------------------------------------------------------------
def app(
    name: str,
    *,
    mode: Literal["launch", "switch", "resize", "close"] = "launch",
    window_pos: tuple[int, int] | None = None,
    window_size: tuple[int, int] | None = None,
) -> str:
    """Launch, switch, resize or close an app window.

    `name` is matched case-insensitively against visible window titles
    (for switch/resize/close) or passed to PowerShell Start-Process (launch).
    """
    if mode == "launch":
        subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", f"Start-Process '{name}'"],
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return f"Launched {name}"

    import win32con
    import win32gui

    matches: list[tuple[int, str]] = []

    def _enum(hwnd: int, _: Any) -> None:
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if title and name.lower() in title.lower():
            matches.append((hwnd, title))

    win32gui.EnumWindows(_enum, None)
    if not matches:
        raise ValueError(f"no visible window matches '{name}'")
    hwnd, title = matches[0]

    if mode == "switch":
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
        return f"Switched to '{title}'"
    if mode == "resize":
        if window_pos is None or window_size is None:
            raise ValueError("resize needs window_pos=(x,y) and window_size=(w,h)")
        win32gui.MoveWindow(hwnd, *window_pos, *window_size, True)
        return f"Resized '{title}' → {window_pos}+{window_size}"
    if mode == "close":
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        return f"Closed '{title}'"
    raise ValueError(f"unknown mode {mode!r}")


def window_list() -> list[dict]:
    """List all visible top-level windows: [{hwnd, title, bounds}, ...]"""
    import win32gui

    out: list[dict] = []

    def _enum(hwnd: int, _: Any) -> None:
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        out.append({
            "hwnd": hwnd, "title": title,
            "bounds": [left, top, right, bottom],
        })

    win32gui.EnumWindows(_enum, None)
    return out


# ---------------------------------------------------------------------------
# Process
# ---------------------------------------------------------------------------
def process_list(
    *,
    sort_by: Literal["memory", "name"] = "memory",
    limit: int = 20,
) -> list[dict]:
    import psutil

    procs: list[dict] = []
    for p in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            info = p.info
            mem = info.get("memory_info")
            procs.append({
                "pid": info["pid"],
                "name": info["name"] or "",
                "memory_mb": (mem.rss // (1024 * 1024)) if mem else 0,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    keyfns = {
        "memory": lambda x: -x["memory_mb"],
        "name": lambda x: x["name"].lower(),
    }
    procs.sort(key=keyfns[sort_by])
    return procs[:limit]


def process_kill(
    *,
    pid: int | None = None,
    name: str | None = None,
    force: bool = False,
) -> int:
    """Kill by pid OR by name substring (case-insensitive). Returns count killed."""
    import psutil

    targets: list[psutil.Process] = []
    if pid is not None:
        targets.append(psutil.Process(pid))
    elif name:
        for p in psutil.process_iter(["name"]):
            pn = p.info.get("name") or ""
            if name.lower() == pn.lower():
                targets.append(p)
    else:
        raise ValueError("provide pid= or name=")
    if len(targets) > 5 and not force:
        raise RuntimeError(
            f"process_kill matched {len(targets)} processes for "
            f"name={name!r}; use force=True to kill >5"
        )
    killed = 0
    for p in targets:
        try:
            p.kill() if force else p.terminate()
            killed += 1
        except psutil.Error:
            continue
    return killed


# ---------------------------------------------------------------------------
# Registry (winreg in-process — 50× faster than PowerShell)
# ---------------------------------------------------------------------------
_REG_HIVES = {
    "HKCU": "HKEY_CURRENT_USER", "HKLM": "HKEY_LOCAL_MACHINE",
    "HKCR": "HKEY_CLASSES_ROOT", "HKU": "HKEY_USERS",
    "HKCC": "HKEY_CURRENT_CONFIG",
}


def _parse_reg_path(path: str) -> tuple[int, str]:
    import winreg

    clean = path.replace(":", "").replace("/", "\\").lstrip("\\")
    head, _, rest = clean.partition("\\")
    hive_name = _REG_HIVES.get(head.upper(), head.upper())
    if not hasattr(winreg, hive_name):
        raise ValueError(f"unknown hive {head!r}")
    return getattr(winreg, hive_name), rest


def registry(
    mode: Literal["get", "set", "delete", "list"],
    path: str,
    *,
    name: str | None = None,
    value: Any = None,
    type: Literal[
        "String", "DWord", "QWord", "Binary",
        "MultiString", "ExpandString",
    ] = "String",
    dangerous: bool = False,
) -> Any:
    """Registry ops. Accepts 'HKCU:\\Software\\X' or 'HKEY_CURRENT_USER\\...'."""
    import winreg

    hive, sub = _parse_reg_path(path)
    type_map = {
        "String": winreg.REG_SZ, "DWord": winreg.REG_DWORD,
        "QWord": winreg.REG_QWORD, "Binary": winreg.REG_BINARY,
        "MultiString": winreg.REG_MULTI_SZ,
        "ExpandString": winreg.REG_EXPAND_SZ,
    }

    if mode == "get":
        with winreg.OpenKey(hive, sub) as k:
            v, _ = winreg.QueryValueEx(k, name or "")
            return v
    if mode == "set":
        with winreg.CreateKey(hive, sub) as k:
            winreg.SetValueEx(k, name or "", 0, type_map[type], value)
        return True
    if mode == "delete":
        _guard_destructive("registry_delete", dangerous)
        if name:
            with winreg.OpenKey(hive, sub, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, name)
        else:
            winreg.DeleteKey(hive, sub)
        return True
    if mode == "list":
        with winreg.OpenKey(hive, sub) as k:
            vals: list[dict] = []
            i = 0
            while True:
                try:
                    n, v, _ = winreg.EnumValue(k, i)
                    vals.append({"name": n, "value": v})
                    i += 1
                except OSError:
                    break
            return vals
    raise ValueError(f"unknown mode {mode!r}")


# ---------------------------------------------------------------------------
# PowerShell
# ---------------------------------------------------------------------------
def powershell(
    command: str, *, timeout: int = 60, dangerous: bool = False,
) -> dict:
    """Run a PowerShell command. Destructive gate: dangerous=True or env."""
    _guard_destructive("powershell", dangerous)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", command],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout, env=env,
        )
        return {
            "status": "ok" if r.returncode == 0 else "error",
            "returncode": r.returncode,
            "stdout": r.stdout,
            "stderr": r.stderr,
        }
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "returncode": -1,
                "stdout": "", "stderr": f"timeout after {timeout}s"}


# ---------------------------------------------------------------------------
# FileSystem (pathlib + shutil)
# ---------------------------------------------------------------------------
def fs_read(
    path: str | Path,
    *,
    encoding: str = "utf-8",
    offset: int = 0,
    limit: int | None = None,
) -> str:
    p = Path(path)
    with p.open("r", encoding=encoding, errors="replace") as f:
        for _ in range(offset):
            f.readline()
        if limit is None:
            return f.read()
        lines: list[str] = []
        for _ in range(limit):
            line = f.readline()
            if not line:
                break
            lines.append(line)
        return "".join(lines)


def fs_write(
    path: str | Path,
    content: str,
    *,
    append: bool = False,
    encoding: str = "utf-8",
) -> int:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a" if append else "w", encoding=encoding) as f:
        return f.write(content)


def fs_list(
    path: str | Path = ".",
    *,
    pattern: str = "*",
    recursive: bool = False,
    show_hidden: bool = False,
) -> list[str]:
    p = Path(path)
    it = p.rglob(pattern) if recursive else p.glob(pattern)
    return [str(x) for x in it if show_hidden or not x.name.startswith(".")]


def fs_search(
    root: str | Path,
    pattern: str,
    *,
    content: str | None = None,
) -> list[str]:
    """Glob files + optional substring content match."""
    p = Path(root)
    out: list[str] = []
    for f in p.rglob(pattern):
        if not f.is_file():
            continue
        if content is None:
            out.append(str(f))
            continue
        try:
            if content in f.read_text(encoding="utf-8", errors="ignore"):
                out.append(str(f))
        except OSError:
            continue
    return out


def fs_copy(src: str | Path, dst: str | Path, *, overwrite: bool = False) -> str:
    s, d = Path(src), Path(dst)
    if d.exists() and not overwrite:
        raise FileExistsError(dst)
    if s.is_dir():
        shutil.copytree(s, d, dirs_exist_ok=overwrite)
    else:
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, d)
    return str(d)


def fs_move(src: str | Path, dst: str | Path) -> str:
    shutil.move(str(src), str(dst))
    return str(dst)


def fs_delete(
    path: str | Path, *, dangerous: bool = False,
) -> None:
    _guard_destructive("fs_delete", dangerous)
    p = Path(path)
    if p.is_dir():
        shutil.rmtree(p)
    elif p.exists():
        p.unlink()


def fs_info(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        return {"exists": False}
    st = p.stat()
    return {
        "exists": True,
        "is_file": p.is_file(),
        "is_dir": p.is_dir(),
        "size": st.st_size,
        "mtime": st.st_mtime,
    }


# ---------------------------------------------------------------------------
# Notification (toast)
# ---------------------------------------------------------------------------
def notify(title: str, message: str = "", *, duration: str = "short") -> None:
    """Windows toast. Prefers win11toast, falls back to PowerShell WinRT."""
    try:
        from win11toast import toast as _toast  # type: ignore

        _toast(title, message, duration=duration)
        return
    except ImportError:
        pass
    t = title.replace("'", "''")
    m = message.replace("'", "''")
    ns = "Windows.UI.Notifications"
    ps = "; ".join([
        f"[void][{ns}.ToastNotificationManager, {ns},"
        " ContentType=WindowsRuntime]",
        f"$tpl=[{ns}.ToastNotificationManager]::GetTemplateContent("
        f"[{ns}.ToastTemplateType]::ToastText02)",
        f"$tpl.SelectSingleNode(\"//text[@id='1']\").AppendChild("
        f"$tpl.CreateTextNode('{t}')) | Out-Null",
        f"$tpl.SelectSingleNode(\"//text[@id='2']\").AppendChild("
        f"$tpl.CreateTextNode('{m}')) | Out-Null",
        f"$t=[{ns}.ToastNotification]::new($tpl)",
        f"[{ns}.ToastNotificationManager]::"
        "CreateToastNotifier('windows').Show($t)",
    ])
    powershell(ps, timeout=10)


# ---------------------------------------------------------------------------
# Scrape (httpx + trafilatura)
# ---------------------------------------------------------------------------
def scrape(
    url: str,
    *,
    query: str | None = None,
    prefer: Literal["trafilatura", "markdownify", "raw"] = "trafilatura",
    timeout: int = 30,
) -> str:
    """Fetch URL and return cleaned text. trafilatura → markdownify → raw HTML."""
    import httpx

    r = httpx.get(
        url, follow_redirects=True, timeout=timeout,
        headers={"User-Agent": "Mozilla/5.0 windows/1.0"},
    )
    r.raise_for_status()
    html = r.text

    if prefer == "trafilatura":
        try:
            import trafilatura  # type: ignore

            extracted = trafilatura.extract(
                html, include_links=True, include_tables=True
            )
            if extracted:
                return _filter_by_query(extracted, query)
        except ImportError:
            pass

    if prefer in ("trafilatura", "markdownify"):
        try:
            from markdownify import markdownify  # type: ignore

            return _filter_by_query(markdownify(html, heading_style="ATX"), query)
        except ImportError:
            pass

    return _filter_by_query(html, query)


def _filter_by_query(text: str, query: str | None) -> str:
    if not query:
        return text
    q = query.lower()
    lines = text.splitlines()
    hits: list[str] = []
    for i, ln in enumerate(lines):
        if q in ln.lower():
            ctx = lines[max(0, i - 2): i + 3]
            hits.append("\n".join(ctx))
    return "\n---\n".join(hits) if hits else text


# ---------------------------------------------------------------------------
# Snapshot (uiautomation — lazy, only when labels needed)
# ---------------------------------------------------------------------------
_INTERACTIVE = {
    "ButtonControl", "EditControl", "HyperlinkControl", "CheckBoxControl",
    "RadioButtonControl", "ComboBoxControl", "ListItemControl",
    "TreeItemControl", "MenuItemControl", "TabItemControl", "SliderControl",
}


def snapshot(
    *,
    active_window_only: bool = True,
    include_invisible: bool = False,
    max_depth: int = 15,
    capture_image: bool = False,
) -> dict:
    """Walk UIA tree, populate STATE.labels for click(label=N)."""
    try:
        import uiautomation as uia  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "snapshot() needs `pip install uiautomation`"
        ) from e

    STATE.labels.clear()
    elements: list[dict] = []

    if active_window_only:
        fg = uia.GetForegroundControl()
        roots = [fg] if fg else []
    else:
        roots = [
            c for c in uia.GetRootControl().GetChildren()
            if c.IsWindowPatternAvailable()
        ]

    counter = 0

    def _walk(ctrl, depth: int = 0) -> None:
        nonlocal counter
        if depth > max_depth:
            return
        try:
            rect = ctrl.BoundingRectangle
            if not include_invisible:
                if rect.width() <= 0 or rect.height() <= 0:
                    return
            if ctrl.ControlTypeName in _INTERACTIVE:
                cx = (rect.left + rect.right) // 2
                cy = (rect.top + rect.bottom) // 2
                counter += 1
                STATE.labels[counter] = (cx, cy)
                elements.append({
                    "label": counter,
                    "name": (ctrl.Name or "")[:80],
                    "type": ctrl.ControlTypeName,
                    "xy": [cx, cy],
                    "bounds": [rect.left, rect.top, rect.right, rect.bottom],
                })
            for child in ctrl.GetChildren():
                _walk(child, depth + 1)
        except Exception:
            return

    for root in roots:
        if root is not None:
            _walk(root)

    out: dict = {"elements": elements, "count": len(elements)}
    if capture_image:
        out["image_png_bytes"] = len(screenshot())  # size marker; raw bytes on demand
    STATE.last_snapshot = out
    return out


# ---------------------------------------------------------------------------
# Multi-ops
# ---------------------------------------------------------------------------
def multi_click(
    targets: list[int | tuple[int, int]],
    *,
    hold_ctrl: bool = False,
) -> None:
    """Click multiple labels or (x,y) tuples. hold_ctrl=True for multi-select."""
    import pyautogui

    if hold_ctrl:
        pyautogui.keyDown("ctrl")
    try:
        for t in targets:
            if isinstance(t, int):
                click(label=t)
            else:
                click(t[0], t[1])
            time.sleep(0.05)
    finally:
        if hold_ctrl:
            pyautogui.keyUp("ctrl")


def multi_edit(
    targets: list[tuple[int | tuple[int, int], str]],
) -> None:
    """Fill multiple fields: [(label_or_xy, text), ...]"""
    for target, text in targets:
        if isinstance(target, int):
            click(label=target)
        else:
            click(target[0], target[1])
        time.sleep(0.08)
        # clear then type
        import pyautogui
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.03)
        type_text(text, ime_safe=True)


# ---------------------------------------------------------------------------
# Wait
# ---------------------------------------------------------------------------
def wait(seconds: float) -> None:
    time.sleep(seconds)


# ---------------------------------------------------------------------------
# UIA pattern ops (headless — no focus steal, no SendInput)
# ---------------------------------------------------------------------------
_UIA_TYPE_MAP = {
    "Button": "ButtonControl", "Edit": "EditControl",
    "ComboBox": "ComboBoxControl", "CheckBox": "CheckBoxControl",
    "RadioButton": "RadioButtonControl", "ListItem": "ListItemControl",
    "TreeItem": "TreeItemControl", "MenuItem": "MenuItemControl",
    "TabItem": "TabItemControl", "Text": "TextControl",
    "Document": "DocumentControl", "Pane": "PaneControl",
    "Window": "WindowControl", "Hyperlink": "HyperlinkControl",
}


def _uia_root(window_title: str | None):
    import uiautomation as uia

    if not window_title:
        return uia.GetRootControl()
    win = uia.WindowControl(searchDepth=1, SubName=window_title)
    if not win.Exists(0.5):
        raise TimeoutError(
            f"window with title containing {window_title!r} not found"
        )
    return win


def uia_find(
    *,
    name: str | None = None,
    control_type: str | None = None,
    window_title: str | None = None,
    automation_id: str | None = None,
    timeout: float = 3.0,
):
    """Find a UIA control by name / type / automation_id / parent window."""
    import uiautomation as uia

    deadline = time.time() + timeout
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            root = _uia_root(window_title)
            kwargs: dict = {"searchFromControl": root}
            if name:
                kwargs["Name"] = name
            if automation_id:
                kwargs["AutomationId"] = automation_id
            if control_type:
                cls_name = _UIA_TYPE_MAP.get(
                    control_type, f"{control_type}Control"
                )
                cls = getattr(uia, cls_name, None)
                if cls is None:
                    raise ValueError(f"unknown control_type {control_type!r}")
                ctrl = cls(**kwargs)
            else:
                ctrl = uia.Control(**kwargs)
            if ctrl.Exists(0.1):
                return ctrl
        except Exception as e:  # noqa: BLE001
            last_err = e
        time.sleep(0.15)
    raise TimeoutError(
        f"UIA element not found "
        f"(name={name!r} type={control_type!r} win={window_title!r}): "
        f"{last_err}"
    )


def _get_pattern(ctrl, pattern_name: str):
    """Return UIA pattern object or None. Works with uiautomation 2.x
    where GetPattern(PatternId) is the canonical API."""
    import uiautomation as uia

    pid = getattr(uia.PatternId, pattern_name, None)
    if pid is None:
        return None
    try:
        return ctrl.GetPattern(pid)
    except Exception:  # noqa: BLE001
        return None


def uia_click(
    *,
    name: str | None = None,
    control_type: str | None = None,
    window_title: str | None = None,
    automation_id: str | None = None,
    timeout: float = 3.0,
) -> str:
    """Click via UIA Invoke pattern. NO focus steal, NO SendInput."""
    ctrl = uia_find(
        name=name, control_type=control_type,
        window_title=window_title, automation_id=automation_id,
        timeout=timeout,
    )
    inv = _get_pattern(ctrl, "InvokePattern")
    if inv is not None:
        inv.Invoke()
        return ctrl.Name
    tog = _get_pattern(ctrl, "TogglePattern")
    if tog is not None:
        tog.Toggle()
        return ctrl.Name
    ctrl.Click(simulateMove=False)
    return ctrl.Name


def uia_set_value(
    value: str,
    *,
    name: str | None = None,
    control_type: str = "Edit",
    window_title: str | None = None,
    automation_id: str | None = None,
    timeout: float = 3.0,
) -> bool:
    """Set value via UIA Value / LegacyIAccessible pattern. No SendInput."""
    ctrl = uia_find(
        name=name, control_type=control_type,
        window_title=window_title, automation_id=automation_id,
        timeout=timeout,
    )
    vp = _get_pattern(ctrl, "ValuePattern")
    if vp is not None:
        vp.SetValue(value)
        return True
    try:
        lp = ctrl.GetLegacyIAccessiblePattern()
        if lp is not None:
            lp.SetValue(value)
            return True
    except Exception:  # noqa: BLE001
        pass
    raise RuntimeError(
        f"control {ctrl.Name!r} "
        f"({ctrl.ControlTypeName}) has no Value / LegacyIAccessible pattern"
    )


def uia_get_value(
    *,
    name: str | None = None,
    control_type: str = "Edit",
    window_title: str | None = None,
    automation_id: str | None = None,
    timeout: float = 3.0,
) -> str:
    """Read value via UIA Value / Text pattern."""
    ctrl = uia_find(
        name=name, control_type=control_type,
        window_title=window_title, automation_id=automation_id,
        timeout=timeout,
    )
    vp = _get_pattern(ctrl, "ValuePattern")
    if vp is not None:
        return vp.Value
    tp = _get_pattern(ctrl, "TextPattern")
    if tp is not None:
        return tp.DocumentRange.GetText(-1)
    return ctrl.Name


# ---------------------------------------------------------------------------
# Screenshot specific window by HWND (works non-foreground)
# ---------------------------------------------------------------------------
def screenshot_window(
    hwnd: int | None = None,
    *,
    window_title: str | None = None,
    path: str | Path | None = None,
) -> bytes | str:
    """PrintWindow-based capture. Works for non-foreground / non-current
    desktop windows (PW_RENDERFULLCONTENT, Win 8.1+)."""
    from ctypes import windll

    import win32gui
    import win32ui
    from PIL import Image

    if hwnd is None:
        if not window_title:
            raise ValueError("provide hwnd= or window_title=")
        matches = [
            x for x in window_list()
            if window_title.lower() in x["title"].lower()
        ]
        if not matches:
            raise ValueError(f"no window matches {window_title!r}")
        hwnd = matches[0]["hwnd"]

    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        raise RuntimeError(f"window has zero size: {width}x{height}")

    hdc = win32gui.GetWindowDC(hwnd)
    mfc = win32ui.CreateDCFromHandle(hdc)
    save_dc = mfc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(mfc, width, height)
    save_dc.SelectObject(bmp)
    ok = windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)
    info = bmp.GetInfo()
    bits = bmp.GetBitmapBits(True)
    img = Image.frombuffer(
        "RGB", (info["bmWidth"], info["bmHeight"]),
        bits, "raw", "BGRX", 0, 1,
    )
    win32gui.DeleteObject(bmp.GetHandle())
    save_dc.DeleteDC()
    mfc.DeleteDC()
    win32gui.ReleaseDC(hwnd, hdc)

    if not ok:
        raise RuntimeError(f"PrintWindow failed for hwnd {hwnd}")

    if path is not None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        img.save(str(path), "PNG")
        return str(path)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Virtual Desktop context (pyvda)
# ---------------------------------------------------------------------------
class _VirtualDesktopCtx:
    """Create a fresh virtual desktop. Optionally move windows onto it.

    Usage (headless — user's desktop untouched):
        w.app("notepad", mode="launch"); time.sleep(0.8)
        hwnds = [x["hwnd"] for x in w.window_list()
                 if "notepad" in x["title"].lower()]
        with w.virtual_desktop() as vd:
            for h in hwnds:
                vd.move_window(h)
            # act on notepad via UIA — hwnd is on bg desktop, not visible
            w.uia_set_value("hello", window_title="Notepad")
            png = w.screenshot_window(hwnds[0])
    """

    def __init__(self, switch: bool = False):
        self.switch = switch
        self._vd = None
        self._original = None

    def __enter__(self):
        from pyvda import VirtualDesktop

        self._original = VirtualDesktop.current()
        self._vd = VirtualDesktop.create()
        if self.switch:
            self._vd.go()
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._vd is not None:
                self._vd.remove()
        except Exception:
            try:
                if self._original is not None:
                    self._original.go()
            except Exception:
                pass

    def move_window(self, hwnd: int) -> None:
        from pyvda import AppView

        AppView(hwnd=hwnd).move(self._vd)

    @property
    def number(self) -> int:
        return self._vd.number if self._vd else -1


def virtual_desktop(switch: bool = False) -> _VirtualDesktopCtx:
    """Virtual-desktop context manager (pyvda). Creates new desktop, moves
    windows to it, destroys on exit. Default does NOT switch focus.

    NOTE: Win11 24H2+ (build 26100+) may fail with COMError due to GUID
    drift in IVirtualDesktopManagerInternal. For guaranteed invisibility,
    use run_in_hidden_desktop() instead — pure Win32, no COM.
    """
    return _VirtualDesktopCtx(switch=switch)


# ---------------------------------------------------------------------------
# Hidden Win32 Desktop (CreateDesktopW) — true headless, user never sees it
# ---------------------------------------------------------------------------
def run_in_hidden_desktop(
    cmdline: str,
    *,
    desktop_name: str = "winops_bg",
    timeout_ms: int = 60000,
    cwd: str | None = None,
) -> dict:
    """Run a process in a fresh hidden Win32 Desktop object.

    Unlike pyvda virtual desktops (which are shell/COM and drift with Win11
    builds), CreateDesktopW is stable Win32 API since NT 3.5. Windows
    spawned by the child process are confined to the hidden desktop — user
    must explicitly SwitchDesktop() to see them, which we never do.

    Caveat: UWP apps (os.startfile on modern notepad/calc) break out of the
    hidden desktop because they run in ApplicationFrameHost under the
    default desktop. For UWP targets, use virtual_desktop() instead.
    Classic Win32 apps (PowerShell WinForms, mspaint, regedit, python GUI)
    stay confined.

    Returns: {"returncode": int, "timed_out": bool, "pid": int}
    """
    import ctypes
    from ctypes import byref, c_void_p, create_unicode_buffer, sizeof
    from ctypes.wintypes import BOOL, BYTE, DWORD, HANDLE, LPWSTR, WORD

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    DESKTOP_ALL_ACCESS = 0x01FF
    INFINITE = 0xFFFFFFFF

    CreateDesktopW = user32.CreateDesktopW
    CreateDesktopW.restype = HANDLE
    CreateDesktopW.argtypes = [LPWSTR, LPWSTR, c_void_p, DWORD, DWORD, c_void_p]
    CloseDesktop = user32.CloseDesktop
    CloseDesktop.restype = BOOL
    CloseDesktop.argtypes = [HANDLE]

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", DWORD), ("lpReserved", LPWSTR), ("lpDesktop", LPWSTR),
            ("lpTitle", LPWSTR), ("dwX", DWORD), ("dwY", DWORD),
            ("dwXSize", DWORD), ("dwYSize", DWORD),
            ("dwXCountChars", DWORD), ("dwYCountChars", DWORD),
            ("dwFillAttribute", DWORD), ("dwFlags", DWORD),
            ("wShowWindow", WORD), ("cbReserved2", WORD),
            ("lpReserved2", ctypes.POINTER(BYTE)),
            ("hStdInput", HANDLE), ("hStdOutput", HANDLE),
            ("hStdError", HANDLE),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", HANDLE), ("hThread", HANDLE),
            ("dwProcessId", DWORD), ("dwThreadId", DWORD),
        ]

    CreateProcessW = kernel32.CreateProcessW
    CreateProcessW.restype = BOOL
    CreateProcessW.argtypes = [
        LPWSTR, LPWSTR, c_void_p, c_void_p, BOOL, DWORD,
        c_void_p, LPWSTR, ctypes.POINTER(STARTUPINFOW),
        ctypes.POINTER(PROCESS_INFORMATION),
    ]
    WaitForSingleObject = kernel32.WaitForSingleObject
    GetExitCodeProcess = kernel32.GetExitCodeProcess
    GetExitCodeProcess.argtypes = [HANDLE, ctypes.POINTER(DWORD)]
    CloseHandle = kernel32.CloseHandle

    hdesk = CreateDesktopW(desktop_name, None, None, 0, DESKTOP_ALL_ACCESS, None)
    if not hdesk:
        err = ctypes.get_last_error() or ctypes.GetLastError()
        raise OSError(f"CreateDesktopW failed (err={err})")

    try:
        si = STARTUPINFOW()
        si.cb = sizeof(STARTUPINFOW)
        si.lpDesktop = f"WinSta0\\{desktop_name}"
        pi = PROCESS_INFORMATION()
        cmd_buf = create_unicode_buffer(cmdline)
        ok = CreateProcessW(
            None, cmd_buf, None, None, False, 0,
            None, cwd, byref(si), byref(pi),
        )
        if not ok:
            err = ctypes.GetLastError()
            raise OSError(f"CreateProcessW failed (err={err}) cmd={cmdline!r}")
        try:
            wait_ms = INFINITE if timeout_ms <= 0 else timeout_ms
            rv = WaitForSingleObject(pi.hProcess, wait_ms)
            timed_out = rv == 0x00000102  # WAIT_TIMEOUT
            exit_code = DWORD()
            if not timed_out:
                GetExitCodeProcess(pi.hProcess, byref(exit_code))
                code = exit_code.value
            else:
                kernel32.TerminateProcess(pi.hProcess, 1)
                code = -1
            return {
                "returncode": code,
                "timed_out": timed_out,
                "pid": pi.dwProcessId,
            }
        finally:
            CloseHandle(pi.hProcess)
            CloseHandle(pi.hThread)
    finally:
        CloseDesktop(hdesk)


# ---------------------------------------------------------------------------
# CLI entry (dict-dispatch to keep function short + flat)
# ---------------------------------------------------------------------------
def _dump(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, default=str, indent=2))


def _h_screenshot(a: Any) -> None:
    r = screenshot(path=a.out, scale=a.scale, display=a.display)
    print(r if isinstance(r, str) else f"<PNG {len(r)} bytes>")


def _h_click(a: Any) -> None:
    print(click(a.x, a.y, button=a.button, clicks=a.clicks))


def _h_type(a: Any) -> None:
    type_text(a.text, press_enter=a.enter)


def _h_clipboard(a: Any) -> None:
    if a.mode == "get":
        sys.stdout.write(clipboard_get())
    else:
        clipboard_set(a.text)


def _h_process(a: Any) -> None:
    if a.action == "list":
        _dump(process_list())
    else:
        print(process_kill(pid=a.pid, name=a.name, force=a.force))


def _h_reg(a: Any) -> None:
    _dump(registry(a.mode, a.path, name=a.name, value=a.value, type=a.type))


_HANDLERS: dict[str, Any] = {
    "screenshot": _h_screenshot,
    "click": _h_click,
    "type": _h_type,
    "shortcut": lambda a: shortcut(a.combo),
    "move": lambda a: move(a.x, a.y, drag=a.drag),
    "scroll": lambda a: scroll(a.amount, direction=a.direction),
    "wait": lambda a: wait(a.seconds),
    "clipboard": _h_clipboard,
    "app": lambda a: print(app(a.name, mode=a.mode)),
    "windows": lambda a: _dump(window_list()),
    "ps": lambda a: _dump(powershell(a.command)),
    "process": _h_process,
    "scrape": lambda a: sys.stdout.write(scrape(a.url, query=a.query)),
    "notify": lambda a: notify(a.title, a.message),
    "reg": _h_reg,
    "snapshot": lambda a: _dump(snapshot(active_window_only=not a.all)),
}


def _build_argparser():
    import argparse

    ap = argparse.ArgumentParser(
        "windows",
        description="In-process Windows control (windows-mcp replacement)",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("screenshot")
    p.add_argument("--out")
    p.add_argument("--scale", type=float, default=1.0)
    p.add_argument("--display", type=int, default=1)

    p = sub.add_parser("click")
    p.add_argument("x", type=int)
    p.add_argument("y", type=int)
    p.add_argument("--button", default="left")
    p.add_argument("--clicks", type=int, default=1)

    p = sub.add_parser("type")
    p.add_argument("text")
    p.add_argument("--enter", action="store_true")

    p = sub.add_parser("shortcut")
    p.add_argument("combo")

    p = sub.add_parser("move")
    p.add_argument("x", type=int)
    p.add_argument("y", type=int)
    p.add_argument("--drag", action="store_true")

    p = sub.add_parser("scroll")
    p.add_argument("amount", type=int)
    p.add_argument("--direction", default="down")

    p = sub.add_parser("wait")
    p.add_argument("seconds", type=float)

    p = sub.add_parser("clipboard")
    p.add_argument("mode", choices=["get", "set"])
    p.add_argument("--text", default="")

    p = sub.add_parser("app")
    p.add_argument("name")
    p.add_argument("--mode", default="launch",
                   choices=["launch", "switch", "resize", "close"])

    sub.add_parser("windows")

    p = sub.add_parser("ps")
    p.add_argument("command")

    p = sub.add_parser("process")
    p.add_argument("action", choices=["list", "kill"])
    p.add_argument("--pid", type=int)
    p.add_argument("--name")
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("scrape")
    p.add_argument("url")
    p.add_argument("--query", default=None)

    p = sub.add_parser("notify")
    p.add_argument("title")
    p.add_argument("--message", default="")

    p = sub.add_parser("reg")
    p.add_argument("mode", choices=["get", "set", "delete", "list"])
    p.add_argument("path")
    p.add_argument("--name")
    p.add_argument("--value")
    p.add_argument("--type", default="String")

    p = sub.add_parser("snapshot")
    p.add_argument("--all", action="store_true",
                   help="walk all top-level windows (slower)")

    return ap


def _cli() -> None:
    args = _build_argparser().parse_args()
    _HANDLERS[args.cmd](args)


if __name__ == "__main__":
    _cli()
