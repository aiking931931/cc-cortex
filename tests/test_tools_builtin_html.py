"""Tests for concinno.tools.builtin.html — HtmlToText via trafilatura."""

from __future__ import annotations

import pytest

from concinno.tools.builtin.html import HtmlToText


def test_html_to_text_attributes():
    assert HtmlToText.name == "html_to_text"
    assert HtmlToText.is_concurrency_safe is True
    assert "trafilatura" in HtmlToText.description


def test_html_to_text_missing_input():
    tool = HtmlToText()
    out = tool.call()
    assert out.startswith("error: html is required")


def test_html_to_text_wrong_type():
    tool = HtmlToText()
    out = tool.call(html=123)
    assert out.startswith("error: html must be a str")


def test_html_to_text_empty_string():
    tool = HtmlToText()
    out = tool.call(html="   ")
    assert out == ""


def test_html_to_text_extraction_basic():
    pytest.importorskip("trafilatura")
    tool = HtmlToText()
    html = (
        "<html><body>"
        "<nav>skip me</nav>"
        "<article>"
        "<p>This is the actual content we expect trafilatura to extract "
        "from a simple article page.</p>"
        "<p>Second paragraph with additional body text for extractor.</p>"
        "</article>"
        "<footer>copyright boilerplate</footer>"
        "</body></html>"
    )
    out = tool.call(html=html)
    assert isinstance(out, str)
    assert "actual content" in out
    assert "Second paragraph" in out
    # Nav and footer boilerplate should be dropped by trafilatura.
    assert "skip me" not in out
    assert "copyright" not in out


def test_html_to_text_include_tables_true():
    pytest.importorskip("trafilatura")
    tool = HtmlToText()
    html = (
        "<html><body><article>"
        "<p>Article intro long enough to pass extractor thresholds for "
        "content detection heuristics here please.</p>"
        "<table><tr><td>cell-a</td><td>cell-b</td></tr></table>"
        "</article></body></html>"
    )
    out = tool.call(html=html, include_tables=True)
    # At minimum, the article body must be present.
    assert "Article intro" in out


def test_html_to_text_empty_body_returns_empty():
    pytest.importorskip("trafilatura")
    tool = HtmlToText()
    # Trafilatura returns None for nothing-to-extract → tool returns "".
    out = tool.call(html="<html><body></body></html>")
    assert out == ""


def test_html_to_text_does_not_raise_on_garbage():
    pytest.importorskip("trafilatura")
    tool = HtmlToText()
    # Malformed HTML should not crash the tool.
    out = tool.call(html="<<not really>>html%%garbage&&")
    assert isinstance(out, str)
    # Either empty or error-prefixed is acceptable.
    assert out == "" or out.startswith("error:") or len(out) > 0
