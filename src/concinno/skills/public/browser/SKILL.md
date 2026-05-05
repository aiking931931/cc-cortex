---
name: browser
description: In-process Playwright Python browser automation — playwright-cli 的 daemon 替代。browser session 跨 turn 保留，headless=True 原生無干擾。DOM query/click/fill/screenshot/eval 全 in-process。觸發詞：瀏覽器、browser、Playwright、web 自動化、網頁截圖、DOM、headless browser。
triggers:
  - 瀏覽器自動化
  - browser
  - playwright
  - web 自動化
  - 網頁截圖
  - DOM
  - headless browser
user-invocable: true
---

# browser — in-process Playwright Python daemon

> Playwright CLI 每次起 Node process + Chromium cold launch = 3-5s 稅。in-process Python API browser 只 launch 一次，後續 navigate/screenshot/click = 50-200ms 函式呼叫。跟 windows Skill 同架構哲學：daemon 持有 state，0 RPC。

> **You MUST** 優先 `import browser as b` 寫 .py flow。
> **You MUST** 不用 `playwright-cli` CLI subprocess，除非本模組不支援。
> **You MUST** 用完後 `b.close()`（或 context manager `with b.session():`）釋放 Chromium。

## 鐵律

1. **Browser session 跨 turn 保留** — launch 一次，多次 navigate/screenshot/click
2. **headless=True 預設** — 原生 Chromium headless，用戶零干擾
3. **DOM selector > 座標點擊** — CSS/XPath/aria selector，不用像素座標
4. **JS eval 是第一公民** — `b.eval("document.title")` 直接取值
5. **Web 用 browser，桌面用 windows** — 不混用

## 用法

```python
import os, sys
sys.path.insert(0, os.path.expanduser(r'~/.claude/skills/browser'))
import browser as b

b.launch()                               # Chromium headless, 一次性
b.goto("http://localhost:3000")
b.screenshot(path="home.png")
b.click("button:has-text('Login')")
b.fill("input[name='email']", "test@example.com")
b.fill("input[name='password']", "secret")
b.click("button[type='submit']")
b.wait_for("text=Dashboard")             # auto-wait
b.screenshot(path="dashboard.png")
title = b.eval("document.title")
b.close()
```

## 與 playwright-cli 對照

| playwright-cli | browser Skill |
|---|---|
| `playwright-cli open` | `b.launch()` |
| `playwright-cli goto URL` | `b.goto(url)` |
| `playwright-cli click e3` | `b.click("selector")` |
| `playwright-cli fill e5 text` | `b.fill("selector", text)` |
| `playwright-cli screenshot` | `b.screenshot(path=)` |
| `playwright-cli eval "..."` | `b.eval("...")` |
| `playwright-cli snapshot` | `b.snapshot()` |
| `playwright-cli close` | `b.close()` |

## 依賴

```bash
pip install playwright
playwright install chromium
```

## 按需讀取

| 場景 | 讀這個 |
|---|---|
| 桌面 app 控制（非 Web） | `~/.claude/skills/windows/SKILL.md` |
| 選型對照（MCP vs in-process） | `~/.claude/skills/aegis-tool-stack/SKILL.md` |
