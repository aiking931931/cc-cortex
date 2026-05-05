"""browser — in-process Playwright Python daemon.

Browser session 跨 turn 保留，headless=True 原生。
取代 playwright-cli subprocess（省 Node startup + Chromium cold launch 稅）。

Usage:
    import os, sys
    sys.path.insert(0, os.path.expanduser(r'~/.claude/skills/browser'))
    import browser as b

    b.launch()
    b.goto("http://localhost:3000")
    b.screenshot(path="out.png")
    b.click("button:has-text('Login')")
    b.close()
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any

__all__ = [
    "launch", "close", "session",
    "goto", "click", "dblclick", "fill", "select_option",
    "check", "uncheck", "press", "type_text",
    "screenshot", "pdf",
    "eval_js", "content", "url", "title",
    "wait_for", "wait_for_url", "wait_for_load",
    "snapshot", "locator",
    "cookies", "set_cookies", "clear_cookies",
    "new_page", "pages",
]

_pw = None
_browser = None
_page = None


def _ensure_page():
    if _page is None:
        raise RuntimeError("call launch() first")
    return _page


# ------------------------------------------------------------------
# Lifecycle
# ------------------------------------------------------------------
def launch(
    *,
    headless: bool = True,
    slow_mo: int = 0,
    viewport: dict | None = None,
) -> None:
    """Launch Chromium (once). Subsequent calls are no-op if already running."""
    global _pw, _browser, _page
    if _browser is not None:
        return
    from playwright.sync_api import sync_playwright

    _pw = sync_playwright().start()
    _browser = _pw.chromium.launch(headless=headless, slow_mo=slow_mo)
    ctx_opts: dict = {}
    if viewport:
        ctx_opts["viewport"] = viewport
    context = _browser.new_context(**ctx_opts)
    _page = context.new_page()


def close() -> None:
    """Close browser + Playwright. Safe to call multiple times."""
    global _pw, _browser, _page
    try:
        if _browser:
            _browser.close()
    except Exception:
        pass
    try:
        if _pw:
            _pw.stop()
    except Exception:
        pass
    _pw = _browser = _page = None


@contextmanager
def session(*, headless: bool = True, **kwargs):
    """Context manager: launch on enter, close on exit."""
    try:
        launch(headless=headless, **kwargs)
        yield
    finally:
        close()


# ------------------------------------------------------------------
# Navigation
# ------------------------------------------------------------------
def goto(
    url_str: str,
    *,
    wait_until: str = "domcontentloaded",
    timeout: float = 30000,
) -> None:
    _ensure_page().goto(url_str, wait_until=wait_until, timeout=timeout)


def url() -> str:
    return _ensure_page().url


def title() -> str:
    return _ensure_page().title()


def content() -> str:
    return _ensure_page().content()


# ------------------------------------------------------------------
# Interaction
# ------------------------------------------------------------------
def click(selector: str, *, timeout: float = 5000) -> None:
    _ensure_page().click(selector, timeout=timeout)


def dblclick(selector: str, *, timeout: float = 5000) -> None:
    _ensure_page().dblclick(selector, timeout=timeout)


def fill(selector: str, value: str, *, timeout: float = 5000) -> None:
    _ensure_page().fill(selector, value, timeout=timeout)


def select_option(
    selector: str,
    value: str | list[str],
    *,
    timeout: float = 5000,
) -> list[str]:
    return _ensure_page().select_option(selector, value, timeout=timeout)


def check(selector: str, *, timeout: float = 5000) -> None:
    _ensure_page().check(selector, timeout=timeout)


def uncheck(selector: str, *, timeout: float = 5000) -> None:
    _ensure_page().uncheck(selector, timeout=timeout)


def press(key: str, *, selector: str | None = None) -> None:
    page = _ensure_page()
    if selector:
        page.press(selector, key)
    else:
        page.keyboard.press(key)


def type_text(
    selector: str,
    text: str,
    *,
    delay: float = 50,
    timeout: float = 5000,
) -> None:
    _ensure_page().type(selector, text, delay=delay, timeout=timeout)


# ------------------------------------------------------------------
# Wait
# ------------------------------------------------------------------
def wait_for(
    selector_or_text: str,
    *,
    state: str = "visible",
    timeout: float = 10000,
) -> None:
    page = _ensure_page()
    if selector_or_text.startswith("text="):
        page.locator(selector_or_text).wait_for(
            state=state, timeout=timeout,
        )
    else:
        page.wait_for_selector(
            selector_or_text, state=state, timeout=timeout,
        )


def wait_for_url(
    url_pattern: str, *, timeout: float = 10000,
) -> None:
    _ensure_page().wait_for_url(url_pattern, timeout=timeout)


def wait_for_load(
    state: str = "domcontentloaded", *, timeout: float = 30000,
) -> None:
    _ensure_page().wait_for_load_state(state, timeout=timeout)


# ------------------------------------------------------------------
# Capture
# ------------------------------------------------------------------
def screenshot(
    path: str | Path | None = None,
    *,
    full_page: bool = False,
    selector: str | None = None,
) -> bytes | str:
    """Page or element screenshot. Returns path if given, else PNG bytes."""
    page = _ensure_page()
    if selector:
        loc = page.locator(selector)
        data = loc.screenshot()
    else:
        data = page.screenshot(full_page=full_page)

    if path is not None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(data)
        return str(path)
    return data


def pdf(path: str | Path) -> str:
    """Save page as PDF (headless Chromium only)."""
    data = _ensure_page().pdf()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(data)
    return str(path)


# ------------------------------------------------------------------
# JavaScript
# ------------------------------------------------------------------
def eval_js(expression: str, arg: Any = None) -> Any:
    if arg is not None:
        return _ensure_page().evaluate(expression, arg)
    return _ensure_page().evaluate(expression)


# ------------------------------------------------------------------
# Accessibility snapshot (like playwright-cli snapshot)
# ------------------------------------------------------------------
def snapshot() -> str:
    """ARIA snapshot of the page (structured text, no pixel data)."""
    page = _ensure_page()
    try:
        return page.locator(":root").aria_snapshot()
    except (AttributeError, Exception):
        return page.evaluate("""() => {
            const walk = (el, depth=0) => {
                const tag = el.tagName?.toLowerCase() || '';
                const role = el.getAttribute?.('role') || '';
                const text = el.textContent?.trim().slice(0, 80) || '';
                const id = el.id ? '#'+el.id : '';
                const pre = '  '.repeat(depth);
                let out = pre + tag + id;
                if (role) out += '[role='+role+']';
                if (['INPUT','SELECT','TEXTAREA','BUTTON','A']
                    .includes(el.tagName) && text)
                    out += ' "'+text.slice(0,40)+'"';
                out += '\\n';
                for (const c of el.children || [])
                    out += walk(c, depth+1);
                return out;
            };
            return walk(document.body);
        }""")


# ------------------------------------------------------------------
# Validator helpers (偷自 Skyvern / UFO³ — agent 層 building blocks)
# ------------------------------------------------------------------
def dom_diff(before: str, after: str) -> list[str]:
    """Compare two ARIA snapshots, return changed lines.

    Usage (Validator pattern):
        snap1 = b.snapshot()
        b.click("button")
        snap2 = b.snapshot()
        changes = b.dom_diff(snap1, snap2)
        if not changes:
            raise RuntimeError("click had no effect — retry or switch strategy")
    """
    before_lines = set(before.strip().splitlines())
    after_lines = set(after.strip().splitlines())
    added = after_lines - before_lines
    removed = before_lines - after_lines
    out: list[str] = []
    for line in sorted(removed):
        out.append(f"- {line.strip()}")
    for line in sorted(added):
        out.append(f"+ {line.strip()}")
    return out


def verify_action(
    expected: str,
    *,
    timeout: float = 5000,
    screenshot_on_fail: str | None = None,
) -> bool:
    """After an action, verify expected element/text appeared.

    Returns True if found, False + optional failure screenshot if not.
    Designed for Skyvern-style Validator Agent pattern.
    """
    page = _ensure_page()
    try:
        if expected.startswith("text="):
            page.locator(expected).wait_for(
                state="visible", timeout=timeout,
            )
        else:
            page.wait_for_selector(
                expected, state="visible", timeout=timeout,
            )
        return True
    except Exception:
        if screenshot_on_fail:
            screenshot(path=screenshot_on_fail)
        return False


def batch_actions(
    actions: list[tuple[str, ...]],
    *,
    verify_after: str | None = None,
    screenshot_after: str | None = None,
) -> dict:
    """Execute multiple actions in batch (UFO³ speculative multi-action).

    Each action = (method_name, *args). Example:
        b.batch_actions([
            ("click", "button.next"),
            ("fill", "#search", "query"),
            ("press", "Enter"),
        ], verify_after="text=Results", screenshot_after="results.png")

    Returns: {"steps": int, "verified": bool, "screenshot": str|None}
    """
    dispatch = {
        "click": click, "dblclick": dblclick,
        "fill": fill, "press": press,
        "type": type_text, "check": check, "uncheck": uncheck,
        "select": select_option, "goto": goto,
    }
    steps = 0
    for action in actions:
        name, *args = action
        fn = dispatch.get(name)
        if fn is None:
            raise ValueError(f"unknown action {name!r}")
        fn(*args)
        steps += 1

    verified = True
    if verify_after:
        verified = verify_action(verify_after)

    shot = None
    if screenshot_after:
        shot = screenshot(path=screenshot_after)

    return {"steps": steps, "verified": verified, "screenshot": shot}


# ------------------------------------------------------------------
# Locator (for chaining)
# ------------------------------------------------------------------
def locator(selector: str):
    """Return a Playwright Locator for advanced chaining."""
    return _ensure_page().locator(selector)


# ------------------------------------------------------------------
# Cookies
# ------------------------------------------------------------------
def cookies() -> list[dict]:
    return _ensure_page().context.cookies()


def set_cookies(cookie_list: list[dict]) -> None:
    _ensure_page().context.add_cookies(cookie_list)


def clear_cookies() -> None:
    _ensure_page().context.clear_cookies()


# ------------------------------------------------------------------
# Multi-page
# ------------------------------------------------------------------
def new_page() -> None:
    """Open a new tab and switch to it."""
    global _page
    if _browser is None:
        raise RuntimeError("call launch() first")
    ctx = _browser.contexts[0] if _browser.contexts else _browser.new_context()
    _page = ctx.new_page()


def pages() -> int:
    """Number of open pages."""
    if _browser is None:
        return 0
    ctx = _browser.contexts[0] if _browser.contexts else None
    return len(ctx.pages) if ctx else 0
