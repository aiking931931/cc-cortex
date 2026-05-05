"""Tests for concinno.tools.builtin.web_fetch_full.

Mocks Playwright at the module-import boundary so unit tests run without
chromium / network. The smoke / live integration test against a real URL
is gated by ``RUN_PLAYWRIGHT_LIVE=1`` and skipped otherwise.
"""

from __future__ import annotations

import os
import sys
import types
from unittest.mock import MagicMock

import pytest

from concinno.tool_executor import Tool
from concinno.tools.builtin.web_fetch_full import (
    WebFetchFullTool,
    web_fetch_full,
)

# ── Helpers — build a Playwright stub graph ────────────────


def _build_pw_stub(
    *,
    html: str = "<html><body>hello world</body></html>",
    title: str = "Stub Page",
    final_url: str = "https://example.com/landed",
    screenshot_bytes: bytes = b"\x89PNG\r\n\x1a\nfake-png",
    raise_on_goto: type[BaseException] | None = None,
    raise_on_screenshot: bool = False,
) -> tuple[MagicMock, MagicMock]:
    """Build a (sync_playwright, TimeoutError) pair driving the stub.

    Returns the sync_playwright callable + the MagicMock representing
    the final ``page`` so tests can assert on it.
    """
    page = MagicMock()
    page.content.return_value = html
    page.title.return_value = title
    page.url = final_url
    if raise_on_screenshot:
        page.screenshot.side_effect = RuntimeError("screenshot boom")
    else:
        page.screenshot.return_value = screenshot_bytes

    pw_timeout = type("PWTimeoutError", (Exception,), {})
    if raise_on_goto is not None:
        page.goto.side_effect = raise_on_goto("nav timeout")
    else:
        page.goto.return_value = None

    context = MagicMock()
    context.new_page.return_value = page

    browser = MagicMock()
    browser.new_context.return_value = context

    chromium = MagicMock()
    chromium.launch.return_value = browser

    p_obj = MagicMock()
    p_obj.chromium = chromium

    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=p_obj)
    cm.__exit__ = MagicMock(return_value=False)

    sync_pw = MagicMock(return_value=cm)
    return sync_pw, pw_timeout, page


def _install_pw_stub(monkeypatch, sync_pw, pw_timeout):
    """Inject a fake ``playwright.sync_api`` module."""
    fake_mod = types.ModuleType("playwright.sync_api")
    fake_mod.sync_playwright = sync_pw
    fake_mod.TimeoutError = pw_timeout
    fake_pkg = types.ModuleType("playwright")
    fake_pkg.sync_api = fake_mod
    monkeypatch.setitem(sys.modules, "playwright", fake_pkg)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_mod)


# ── TestProtocolConformance ────────────────────────────────


class TestProtocolConformance:
    def test_tool_protocol(self):
        assert isinstance(WebFetchFullTool(), Tool)

    def test_is_concurrency_safe_true(self):
        assert WebFetchFullTool.is_concurrency_safe is True

    def test_name_and_description(self):
        assert WebFetchFullTool.name == "web_fetch_full"
        assert isinstance(WebFetchFullTool.description, str)
        assert len(WebFetchFullTool.description) > 0


# ── TestUrlValidation ──────────────────────────────────────


class TestUrlValidation:
    def test_empty_url_returns_error(self):
        out = web_fetch_full("")
        assert out["error"] == "error: url is required"
        assert out["text"] == ""
        assert out["screenshot_b64"] is None

    def test_non_http_url_returns_error(self):
        out = web_fetch_full("file:///etc/passwd")
        assert out["error"].startswith("error: url must be http(s)")
        assert out["screenshot_b64"] is None

    def test_tool_call_empty_url_returns_error(self):
        tool = WebFetchFullTool()
        out = tool.call(url="")
        assert out["error"] == "error: url is required"


# ── TestSuccessfulFetch ────────────────────────────────────


class TestSuccessfulFetch:
    def test_static_html_returns_text_and_screenshot(self, monkeypatch, tmp_path):
        sync_pw, pw_timeout, page = _build_pw_stub(
            html="<html><body><h1>hi</h1><p>world</p></body></html>",
            title="My Page",
            final_url="https://example.com/here",
        )
        _install_pw_stub(monkeypatch, sync_pw, pw_timeout)

        save = str(tmp_path / "shot.png")
        out = web_fetch_full(
            "https://example.com",
            screenshot=True,
            save_screenshot_to=save,
        )

        assert out["error"] is None
        assert out["title"] == "My Page"
        assert out["final_url"] == "https://example.com/here"
        assert "hi" in out["text"]
        assert "world" in out["text"]
        assert out["screenshot_b64"] is not None
        assert out["screenshot_path"] == save
        assert os.path.isfile(save)

    def test_screenshot_off_returns_no_b64(self, monkeypatch):
        sync_pw, pw_timeout, page = _build_pw_stub()
        _install_pw_stub(monkeypatch, sync_pw, pw_timeout)
        out = web_fetch_full("https://example.com", screenshot=False)
        assert out["error"] is None
        assert out["screenshot_b64"] is None
        assert out["screenshot_path"] is None
        # Confirm screenshot was never asked of the page.
        page.screenshot.assert_not_called()

    def test_text_capped_at_text_cap(self, monkeypatch):
        long = "<p>" + ("x" * 50_000) + "</p>"
        sync_pw, pw_timeout, _ = _build_pw_stub(html=long)
        _install_pw_stub(monkeypatch, sync_pw, pw_timeout)

        out = web_fetch_full(
            "https://example.com",
            screenshot=False,
            text_cap=200,
        )
        assert out["error"] is None
        assert len(out["text"]) <= 201  # 200 + ellipsis
        assert out["text"].endswith("…")


# ── TestFailures ───────────────────────────────────────────


class TestFailures:
    def test_navigation_timeout_returns_partial(self, monkeypatch):
        sync_pw, pw_timeout, _ = _build_pw_stub(
            html="<html><body>partial</body></html>",
            raise_on_goto=None,
        )
        # Override goto to raise the same timeout class web_fetch_full
        # imports (we already injected pw_timeout via _install_pw_stub).
        _install_pw_stub(monkeypatch, sync_pw, pw_timeout)

        # Re-grab page via the chromium chain to attach the side effect.
        cm = sync_pw()
        page = cm.__enter__().chromium.launch().new_context().new_page()
        page.goto.side_effect = pw_timeout("simulated")
        sync_pw.reset_mock()  # forget the construction call

        out = web_fetch_full(
            "https://example.com",
            screenshot=False,
            timeout_ms=1000,
        )
        assert out["error"] is not None
        assert "navigation timeout" in out["error"]
        # Partial page text still surfaces.
        assert "partial" in out["text"]

    def test_screenshot_failure_keeps_text(self, monkeypatch):
        sync_pw, pw_timeout, _ = _build_pw_stub(
            html="<p>still there</p>",
            raise_on_screenshot=True,
        )
        _install_pw_stub(monkeypatch, sync_pw, pw_timeout)

        out = web_fetch_full("https://example.com", screenshot=True)
        assert out["error"] is not None
        assert "screenshot failed" in out["error"]
        assert "still there" in out["text"]
        assert out["screenshot_b64"] is None

    def test_playwright_missing_returns_install_hint(self, monkeypatch):
        # Force ImportError on the lazy import.
        monkeypatch.setitem(sys.modules, "playwright", None)
        monkeypatch.setitem(sys.modules, "playwright.sync_api", None)

        out = web_fetch_full("https://example.com")
        assert out["error"] is not None
        assert "playwright" in out["error"].lower()


# ── Live smoke (opt-in) ────────────────────────────────────


@pytest.mark.skipif(
    os.environ.get("RUN_PLAYWRIGHT_LIVE") != "1",
    reason="set RUN_PLAYWRIGHT_LIVE=1 to enable live network test",
)
class TestLiveSmoke:
    """Real headless chromium against a stable static page.

    Skipped by default because (a) chromium binary may be absent on CI,
    (b) network IO slows the suite.
    """

    def test_example_com_returns_real_text(self, tmp_path):
        save = str(tmp_path / "live.png")
        out = web_fetch_full(
            "https://example.com",
            screenshot=True,
            save_screenshot_to=save,
        )
        assert out["error"] is None or out["error"].startswith("warn:")
        assert "Example Domain" in (out["text"] + out["title"])
        assert os.path.isfile(save)
