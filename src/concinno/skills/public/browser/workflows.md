# browser 典型 flow（按需讀取）

所有範例假設：

```python
import os, sys
sys.path.insert(0, os.path.expanduser(r'~/.claude/skills/browser'))
import browser as b
b.launch(headless=True)
```

## Flow 1 — Login 表單自動填

```python
b.goto("http://localhost:3000/login")
b.fill("input[name='email']", "test@example.com")
b.fill("input[name='password']", "secret123")
b.click("button[type='submit']")
b.wait_for("text=Dashboard", timeout=5000)
b.screenshot(path="after_login.png")
```

## Flow 2 — 雙 viewport 截圖（桌面 + 手機）

```python
b.goto("http://localhost:3000")
b.screenshot(path="desktop.png", full_page=True)

b.close()
b.launch(headless=True, viewport={"width": 375, "height": 812})
b.goto("http://localhost:3000")
b.screenshot(path="mobile.png", full_page=True)
```

## Flow 3 — DOM diff 驗證操作效果（Skyvern pattern）

```python
b.goto("http://localhost:3000/settings")
snap_before = b.snapshot()
b.click("button:has-text('Save')")
b.wait_for_load()
snap_after = b.snapshot()

changes = b.dom_diff(snap_before, snap_after)
if not changes:
    print("Save had no effect — retry")
else:
    print(f"{len(changes)} DOM changes detected")
```

## Flow 4 — 批次表單 + 驗證（UFO³ pattern）

```python
result = b.batch_actions([
    ("fill", "#name", "王晨宣"),
    ("fill", "#email", "ai@example.com"),
    ("select", "#role", "admin"),
    ("click", "button[type='submit']"),
], verify_after="text=Success", screenshot_after="submitted.png")

print(f"steps={result['steps']} verified={result['verified']}")
```

## Flow 5 — Cookie 管理

```python
b.goto("https://example.com")
b.set_cookies([{
    "name": "session", "value": "abc123",
    "domain": "example.com", "path": "/",
}])
b.goto("https://example.com/dashboard")
print(b.cookies())
b.clear_cookies()
```

## Flow 6 — JS eval 提取結構化數據

```python
b.goto("https://example.com/products")
data = b.eval_js("""() => {
    return [...document.querySelectorAll('.product')].map(el => ({
        name: el.querySelector('h2')?.textContent,
        price: el.querySelector('.price')?.textContent,
    }))
}""")
print(f"extracted {len(data)} products")
```

## 反模式

1. ❌ 每個 flow 都 `b.launch()` + `b.close()` — browser 只 launch 一次
2. ❌ 用 `b.screenshot()` 驗 DOM 變化 — 改 `b.dom_diff()`（省 token）
3. ❌ 用 `windows.scrape(url)` 爬動態 JS 頁 — 改 `b.goto()` + `b.content()`
4. ❌ 用固定 `time.sleep` 等頁面載入 — 改 `b.wait_for()` / `b.wait_for_load()`
