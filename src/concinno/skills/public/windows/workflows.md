# windows 典型 flow（按需讀取）

SKILL.md 是路由器，本檔是實戰範例。照著改參數就能跑。全部範例假設以下前置：

```python
import os, sys; sys.path.insert(0, os.path.expanduser(r'~/.claude/skills/windows'))
import windows as w
```

## Flow 1 — 填表單（snapshot → label → multi_edit）

```python
import windows as w

w.app("Chrome", mode="switch")      # 切到已開的 Chrome
w.wait(0.3)
snap = w.snapshot()                 # UIA 掃 active window
# 看 snap['elements'] 找出要填的 EditControl labels
for e in snap["elements"]:
    if e["type"] == "EditControl":
        print(e["label"], e["name"], e["xy"])

# 假設找到 label 3=name, 5=email, 7=submit button
w.multi_edit([(3, "王晨宣"), (5, "aiking9319319319@gmail.com")])
w.click(label=7)                    # 送出
w.screenshot(path="submitted.png", scale=0.5)  # 視覺驗證
```

**踩雷**：`snapshot()` 後若切了視窗或重新渲染，label 失效 → 再跑一次 `snapshot()`。

## Flow 2 — 瀏覽器 scrape（靜態頁）

```python
import windows as w

md = w.scrape(
    "https://example.com/some-article",
    query="keyword",                    # 只回含關鍵字的段落 ±2 行
)
print(md[:2000])
```

**決策樹**：

```text
頁面內容是 JS 渲染？
  ├─ 否 (docs/blog/wiki) → w.scrape(url)         # trafilatura in-process
  └─ 是 (SPA/React/Vue)  → 用 playwright-cli Skill 走瀏覽器
```

## Flow 3 — 跨視窗自動化（app → switch → shortcut）

```python
import windows as w

# 把剪貼簿內容貼到 Notepad 新檔，存桌面
w.clipboard_set("一段要存檔的文字")
w.app("Notepad", mode="launch"); w.wait(0.6)
w.shortcut("ctrl+v"); w.wait(0.1)
w.shortcut("ctrl+s"); w.wait(0.5)     # Save As 對話框
w.type_text(r"C:\Users\zerox\Desktop\quick_note.txt")
w.shortcut("enter")
```

## Flow 4 — Registry 快速 set/get（50× 快於 PowerShell）

```python
import windows as w

# Enable dark mode (Windows 10/11)
w.registry(
    "set",
    r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
    name="AppsUseLightTheme",
    value=0,
    type="DWord",
)
val = w.registry(
    "get",
    r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
    name="AppsUseLightTheme",
)
print(val)  # → 0
```

## Flow 5 — Process 管理（找殺記憶體怪）

```python
import windows as w

procs = w.process_list(sort_by="memory", limit=10)
for p in procs:
    print(f"{p['memory_mb']:>6} MB  {p['name']}  pid={p['pid']}")

# 殺掉吃最多的 chrome
w.process_kill(name="chrome", force=False)
```

## Flow 6 — Toast 通知（長跑完成提醒）

```python
import windows as w, time

# 長跑任務 ...
time.sleep(30)
w.notify("Training done", "120 epochs, final loss=0.034")
```

## Flow 7 — 截圖傳多模態 LLM（不 OCR）

```python
import windows as w, base64

png = w.screenshot(scale=0.6)        # bytes，縮 60% 省 token
b64 = base64.b64encode(png).decode()
# 餵給 Claude / GPT-4V 的 image content block
# {"type":"image","source":{"type":"base64","media_type":"image/png","data":b64}}
```

**鐵律**：截圖給多模態 LLM，不要先 OCR（aegis-tool-stack 原則 4）。多模態看顏色/位置/排版，OCR 只看文字且常錯。

## Flow 8 — 多顯示器截圖

```python
import windows as w

# display=1 primary, 2 secondary
png1 = w.screenshot(path="primary.png", display=1)
png2 = w.screenshot(path="secondary.png", display=2)

# 或抓特定區域
region_png = w.screenshot(path="crop.png", region=(100, 100, 800, 600))
```

## 反模式（本檔專屬）

1. ❌ 每個 flow 單 tool CLI 串 `&&` — `python windows.py click && python windows.py type` 每次新 process，label state 丟失。**寫成 .py 跑**。
2. ❌ 用 `time.sleep` 等長動作用固定秒數 — 改用 `w.wait(0.3)` 做小步等候 + 截圖輪檢。
3. ❌ `snapshot()` 後馬上拿舊 label — 每次 UI 改變後要重 snapshot。
4. ❌ 對動態頁 `w.scrape(url)` — trafilatura 抓不到 JS 渲染內容，改 playwright-cli。
5. ❌ 複製敏感字串 (token/password) 後忘了清 clipboard — flow 結尾 `w.clipboard_set("")`。
