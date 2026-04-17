---
name: windows
description: In-process Python Windows control — windows-mcp 的無 MCP 替代。18 tools 全覆蓋：screenshot/click/type/snapshot/app/clipboard/registry/powershell/fs/process/scrape/notify。LLM 寫 .py 一次跑完整 flow，0 token RPC 浪費，比 windows-mcp 快 10-100×。觸發詞：windows 控制、桌面自動化、UI 自動化、screenshot、click、snapshot、registry、clipboard、windows-mcp、pywinauto、pyautogui。
triggers:
  - windows 控制
  - 桌面自動化
  - windows-mcp
  - windows
  - screenshot
  - pyautogui
  - pywinauto
  - clipboard
  - registry
user-invocable: true
---

# windows — in-process Windows 控制

> MCP 協議每 tool 一次 JSON-RPC + schema 序列化 + subprocess cold start，18 tools 全吃這層稅。in-process Python 零協議 = 零浪費。LLM 的強項是寫 .py，給一個 `import windows as w` 比給 18 個原子 tool 強 5 倍。這就是 Munio daemon 模式的具體落地。

> **You MUST** 優先 `import windows` 寫 .py flow，而非逐 tool call。
> **You MUST** 呼叫 `w.click(label=N)` 前必先 `w.snapshot()`，label 快取在 `w.STATE.labels`。
> **You MUST** 非 ASCII 字元走 `type_text(..., ime_safe=True)`（預設），CJK/emoji 用 clipboard paste，不用 SendKeys。
> **You MUST** 不用 windows-mcp MCP tools（App/Click/Type/...），除非本模組覆蓋不到。

## 鐵律

1. **in-process 優先**：`python -c "import windows as w; ..."` 或寫成 `.py` 跑。CLI `python windows.py <cmd>` 只作備援。
2. **label state 跨呼叫保留**：同一個 python process 內 `snapshot()` 建立的 label 直到下次 `snapshot()` 都有效。切 process = label 失效。
3. **Registry 用 winreg 不走 PowerShell**：50× 更快，但路徑格式支援 `HKCU:\...` 和 `HKEY_CURRENT_USER\...` 兩種。
4. **Scrape 預設 trafilatura**：靜態頁 60-80% token 省。動態 JS 頁走 `playwright-cli` Skill 而非本模組。
5. **Snapshot 預設 active window only**：全桌面 walk 慢，真的需要才 `active_window_only=False`。

## 用法（三選一）

### A. import + 寫 flow（首選）

```bash
python -c "
import os, sys; sys.path.insert(0, os.path.expanduser(r'~/.claude/skills/windows'))
import windows as w
w.app('notepad', mode='launch'); w.wait(0.5)
w.type_text('Hello 繁體中文', press_enter=True)
w.screenshot(path='out.png', scale=0.5)
"
```

### B. 寫成 .py 跑（多步驟用這個）

```python
# my_flow.py
import os, sys; sys.path.insert(0, os.path.expanduser(r'~/.claude/skills/windows'))
import windows as w

w.app('Calculator', mode='launch'); w.wait(0.8)
snap = w.snapshot()       # label 1..N
# 找「5」按鈕
btn = next(e for e in snap['elements'] if e['name'] == '5')
w.click(label=btn['label'])
```

### C. CLI 單次命令（快速 ad-hoc）

```bash
python ~/.claude/skills/windows/windows.py screenshot --out a.png --scale 0.5
python ~/.claude/skills/windows/windows.py clipboard get
python ~/.claude/skills/windows/windows.py shortcut "ctrl+shift+esc"
```

## 真 headless（classic Win32 app 限定，非 Web — Web 用 Playwright）

用戶當前 desktop 零干擾：**沒視窗跳出、沒搶焦點、沒鍵鼠事件**。pytest 6 test 全綠。
**不等同 Playwright**：Playwright 專注 Web（DOM/JS/network/cookie），本模組專注 Windows 桌面 classic Win32 app。UWP app 會逃離 hidden desktop。

```python
import windows as w

# 1. UIA pattern 直呼（0 SendInput）
w.uia_set_value("Hello 繁體中文", window_title="MyForm", control_type="Edit")
val = w.uia_get_value(window_title="MyForm", control_type="Edit")
w.uia_click(name="Submit", control_type="Button", window_title="MyForm")

# 2. 截非 foreground 窗（z-order / desktop 無關）
png = w.screenshot_window(hwnd=12345, path="out.png")

# 3. Hidden Win32 Desktop — 子 process 完全隔離
result = w.run_in_hidden_desktop(
    f'"{sys.executable}" "worker.py" "{out_json}"',
    timeout_ms=60000,
)
```

**限制**：UWP apps（Win11 notepad/calc）AppContainer 會跑回 Default desktop；
hidden desktop 只困 classic Win32 target（PowerShell WinForms / mspaint /
regedit / 自寫 tkinter）。**為什麼不用 pyvda**：Win11 build 26100+ 的
private COM GUID 漂移，`MoveViewToDesktop` COMError。`CreateDesktopW` 是
NT 3.5 起 stable Win32，不會壞。

## 18 tools 對照表（windows-mcp → windows）

| windows-mcp | windows |
|---|---|
| Screenshot | `w.screenshot(path=, scale=, region=)` |
| Snapshot | `w.snapshot(active_window_only=)` |
| Click | `w.click(x, y / label=N, button=, clicks=)` |
| Type | `w.type_text(text, ime_safe=True, press_enter=)` |
| Shortcut | `w.shortcut("ctrl+c")` |
| Move | `w.move(x, y, drag=)` |
| Scroll | `w.scroll(amount, direction=)` |
| Wait | `w.wait(seconds)` |
| Clipboard | `w.clipboard_get() / clipboard_set(text)` |
| App | `w.app(name, mode=launch/switch/resize/close)` |
| PowerShell | `w.powershell(cmd, timeout=)` |
| Process | `w.process_list() / process_kill(pid=/name=)` |
| Registry | `w.registry("get/set/delete/list", path, name=, value=, type=)` |
| FileSystem | `w.fs_read/write/list/search/copy/move/delete/info` |
| Notification | `w.notify(title, message)` |
| Scrape | `w.scrape(url, query=)` |
| MultiSelect | `w.multi_click([1,3,5], hold_ctrl=True)` |
| MultiEdit | `w.multi_edit([(1,'a'),(2,'b')])` |
| — | `w.window_list()` （**新增**，列所有可見窗） |

## 依賴安裝

```bash
pip install pyautogui pyperclip mss Pillow psutil httpx pywin32
# 可選：
pip install uiautomation           # snapshot()
pip install trafilatura markdownify # scrape() 更乾淨
pip install win11toast              # notify() 更穩
```

## 按需讀取

| 場景 | 讀這個 |
|---|---|
| 填表單/多輸入框 / 多選 / 瀏覽器 scrape 流程 | `~/.claude/skills/windows/workflows.md` |
| 研究報告（18 tool 詳細語義、踩雷） | 本次 session 的 research agent 報告（已沉澱在本 SKILL.md 上表） |
| 選型對照（MCP vs CLI vs in-process） | `.claude/skills/munio-tool-stack/SKILL.md` |

## 反模式

1. ❌ `mcp__windows-mcp__*` 逐 tool 呼叫 — 每次 4× token + RPC latency
2. ❌ 在 bash 逐條 `windows.py click / windows.py type` 串命令 — 每次新 process 丟 label state
3. ❌ 用 `pyautogui.typewrite` 打中文 — 會送出亂碼，必走 `ime_safe=True`（預設）
4. ❌ `snapshot(active_window_only=False)` 當常用預設 — 10+ 視窗時 3-8 秒
5. ❌ Registry 走 `powershell(Get-ItemProperty)` — 慢 50×，用 `w.registry()`
