"""pytest for browser Skill (in-process Playwright daemon).

Run: pytest ~/.claude/skills/browser/test_browser.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
import browser as b  # noqa: E402

HTML = "data:text/html,"


def _page(body: str) -> str:
    return HTML + body.replace(" ", "%20").replace('"', "%22")


@pytest.fixture(scope="module", autouse=True)
def _browser_session():
    b.launch(headless=True)
    yield
    b.close()


# -- Lifecycle --
def test_launch_idempotent():
    b.launch(headless=True)  # second call = no-op
    assert b._browser is not None


# -- Navigation --
def test_goto_and_url():
    b.goto(_page("<h1>Nav</h1>"))
    assert b.url().startswith("data:")


def test_title_on_data_page():
    b.goto(_page("<title>T</title>"))
    assert b.title() == "T"


def test_content_returns_html():
    b.goto(_page("<p>hello</p>"))
    assert "<p>hello</p>" in b.content()


# -- Interaction --
def test_fill_and_readback():
    b.goto(_page('<input id="x"/>'))
    b.fill("#x", "Hi 繁體")
    assert b.eval_js('document.getElementById("x").value') == "Hi 繁體"


def test_click_button():
    b.goto(_page('<button onclick="document.title=\'clicked\'">Go</button>'))
    b.click("button")
    assert b.title() == "clicked"


def test_check_uncheck():
    b.goto(_page('<input type="checkbox" id="c"/>'))
    b.check("#c")
    assert b.eval_js('document.getElementById("c").checked') is True
    b.uncheck("#c")
    assert b.eval_js('document.getElementById("c").checked') is False


def test_press_key():
    b.goto(_page('<input id="k"/>'))
    b.click("#k")
    b.press("a")
    assert b.eval_js('document.getElementById("k").value') == "a"


def test_type_text_slow():
    b.goto(_page('<input id="t"/>'))
    b.type_text("#t", "abc", delay=10)
    assert b.eval_js('document.getElementById("t").value') == "abc"


def test_select_option():
    b.goto(_page(
        '<select id="s"><option value="a">A</option>'
        '<option value="b">B</option></select>'
    ))
    b.select_option("#s", "b")
    assert b.eval_js('document.getElementById("s").value') == "b"


# -- Capture --
def test_screenshot_returns_png_bytes():
    b.goto(_page("<h1>Shot</h1>"))
    data = b.screenshot()
    assert isinstance(data, bytes)
    assert data[:8] == b"\x89PNG\r\n\x1a\n"


def test_screenshot_writes_file(tmp_path):
    b.goto(_page("<h1>File</h1>"))
    out = tmp_path / "s.png"
    path = b.screenshot(path=out)
    assert Path(path).exists()
    assert Path(path).stat().st_size > 500


def test_eval_js_returns_value():
    b.goto(_page("<h1>Eval</h1>"))
    assert b.eval_js("1 + 2") == 3


def test_eval_js_with_arg():
    b.goto(_page("<div></div>"))
    result = b.eval_js("(x) => x * 2", 21)
    assert result == 42


# -- Snapshot --
def test_snapshot_returns_structured_text():
    b.goto(_page("<h1>Snap</h1><button>OK</button>"))
    s = b.snapshot()
    assert isinstance(s, str)
    assert len(s) > 5
    assert "heading" in s.lower() or "button" in s.lower() or "h1" in s.lower()


# -- Wait --
def test_wait_for_visible():
    b.goto(_page("<div id='d'>visible</div>"))
    b.wait_for("#d", timeout=2000)  # should not raise


def test_wait_for_timeout():
    b.goto(_page("<div></div>"))
    with pytest.raises(Exception):
        b.wait_for("#nonexistent", timeout=500)


# -- Cookies --
def test_cookies_roundtrip():
    b.goto("about:blank")
    b.clear_cookies()
    b.set_cookies([{
        "name": "test", "value": "123",
        "url": "https://example.com",
    }])
    found = [c for c in b.cookies() if c["name"] == "test"]
    assert len(found) == 1
    assert found[0]["value"] == "123"
    b.clear_cookies()
    assert len([c for c in b.cookies() if c["name"] == "test"]) == 0


# -- Multi-page --
def test_new_page_and_pages_count():
    initial = b.pages()
    b.new_page()
    assert b.pages() == initial + 1


# -- SOTA: dom_diff --
def test_dom_diff_detects_change():
    b.goto(_page("<h1>Before</h1>"))
    snap1 = b.snapshot()
    b.eval_js('document.querySelector("h1").textContent = "After"')
    snap2 = b.snapshot()
    changes = b.dom_diff(snap1, snap2)
    assert len(changes) >= 1
    joined = " ".join(changes)
    assert "Before" in joined or "After" in joined


def test_dom_diff_no_change():
    b.goto(_page("<p>static</p>"))
    s = b.snapshot()
    assert b.dom_diff(s, s) == []


# -- SOTA: verify_action --
def test_verify_action_true():
    b.goto(_page("<div id='ok'>here</div>"))
    assert b.verify_action("#ok", timeout=2000) is True


def test_verify_action_false():
    b.goto(_page("<p>empty</p>"))
    assert b.verify_action("#missing", timeout=500) is False


# -- SOTA: batch_actions --
def test_batch_actions_fills_and_verifies():
    b.goto(_page('<input id="a"/><input id="b"/>'))
    result = b.batch_actions([
        ("fill", "#a", "hello"),
        ("fill", "#b", "world"),
    ], verify_after="#b")
    assert result["steps"] == 2
    assert result["verified"] is True
    assert b.eval_js('document.getElementById("a").value') == "hello"
    assert b.eval_js('document.getElementById("b").value') == "world"


def test_batch_actions_unknown_raises():
    with pytest.raises(ValueError, match="unknown action"):
        b.batch_actions([("bogus",)])
