"""Tests for concinno.tools.builtin.web — WebSearchTool + FetchUrlTool.

All external I/O is mocked: no live Anthropic calls, no real HTTP. The
tests verify:
  * Protocol conformance (runtime_checkable :class:`Tool`).
  * Input validation + error-string contract.
  * Client wiring (model / max_uses / max_tokens kwargs).
  * Output capping / HTML stripping / non-text sentinel.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from concinno.tool_executor import Tool
from concinno.tools.builtin.web import (
    FetchUrlTool,
    WebSearchTool,
    strip_html,
)

# ── Helpers ──────────────────────────────────────────────


def _make_anthropic_mock(texts: list[str]) -> MagicMock:
    """Return a MagicMock that mimics ``anthropic.Anthropic`` with a
    ``messages.create`` returning a response whose ``.content`` is a
    list of blocks each carrying ``.text``.
    """
    client = MagicMock()
    blocks = [MagicMock(text=t) for t in texts]
    resp = MagicMock(content=blocks)
    client.messages.create.return_value = resp
    return client


class _StubResponse:
    """Minimal stand-in for ``httpx.Response`` used in client mocks."""

    def __init__(
        self,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
        encoding: str | None = "utf-8",
        reason: str = "OK",
    ) -> None:
        self.status_code = status
        self.headers = headers or {}
        self.content = body
        self.encoding = encoding
        self.reason_phrase = reason


def _make_httpx_mock(resp: _StubResponse | Exception) -> MagicMock:
    """Return a MagicMock behaving like ``httpx.Client``."""
    client = MagicMock(spec=httpx.Client)
    if isinstance(resp, Exception):
        client.get.side_effect = resp
    else:
        client.get.return_value = resp
    return client


# ── TestProtocolConformance ──────────────────────────────


class TestProtocolConformance:
    def test_web_search_tool_conforms_to_tool_protocol(self):
        assert isinstance(WebSearchTool(client=MagicMock()), Tool)

    def test_fetch_url_tool_conforms_to_tool_protocol(self):
        assert isinstance(FetchUrlTool(client=MagicMock(spec=httpx.Client)), Tool)

    def test_web_search_is_concurrency_safe_true(self):
        assert WebSearchTool.is_concurrency_safe is True

    def test_fetch_url_is_concurrency_safe_true(self):
        assert FetchUrlTool.is_concurrency_safe is True

    def test_web_search_has_name_and_description(self):
        assert WebSearchTool.name == "web_search"
        assert isinstance(WebSearchTool.description, str)
        assert len(WebSearchTool.description) > 0

    def test_fetch_url_has_name_and_description(self):
        assert FetchUrlTool.name == "fetch_url"
        assert isinstance(FetchUrlTool.description, str)
        assert len(FetchUrlTool.description) > 0


# ── TestWebSearchTool ────────────────────────────────────


class TestWebSearchTool:
    def test_success_merges_text_blocks(self):
        client = _make_anthropic_mock(["egalitarian approach", "with nuance"])
        tool = WebSearchTool(client=client)
        out = tool.call(query="AI regulation 2026")
        assert "egalitarian approach" in out
        assert "with nuance" in out

    def test_empty_query_returns_error(self):
        tool = WebSearchTool(client=MagicMock())
        assert tool.call(query="") == "error: query is required"

    def test_whitespace_query_returns_error(self):
        tool = WebSearchTool(client=MagicMock())
        assert tool.call(query="   ") == "error: query is required"

    def test_missing_query_kwarg_returns_error(self):
        tool = WebSearchTool(client=MagicMock())
        assert tool.call() == "error: query is required"

    def test_client_exception_returns_error_string(self):
        client = MagicMock()
        client.messages.create.side_effect = RuntimeError("boom")
        tool = WebSearchTool(client=client)
        out = tool.call(query="anything")
        assert out.startswith("error: web_search failed:")
        assert "RuntimeError" in out
        assert "boom" in out

    def test_max_uses_passed_to_tools_kwarg(self):
        client = _make_anthropic_mock(["ok"])
        tool = WebSearchTool(client=client, max_uses=7)
        tool.call(query="q")
        kwargs = client.messages.create.call_args.kwargs
        assert kwargs["tools"][0]["max_uses"] == 7
        assert kwargs["tools"][0]["type"] == "web_search_20250305"

    def test_model_and_max_tokens_are_honored(self):
        client = _make_anthropic_mock(["ok"])
        tool = WebSearchTool(
            client=client,
            model="claude-sonnet-4-6-test",
            max_tokens=333,
        )
        tool.call(query="q")
        kwargs = client.messages.create.call_args.kwargs
        assert kwargs["model"] == "claude-sonnet-4-6-test"
        assert kwargs["max_tokens"] == 333

    def test_output_capped_at_output_cap(self):
        long_text = "x" * 5000
        client = _make_anthropic_mock([long_text])
        tool = WebSearchTool(client=client, output_cap=100)
        out = tool.call(query="q")
        assert len(out) <= 101  # 100 chars + ellipsis
        assert out.endswith("\u2026")

    def test_empty_response_returns_sentinel(self):
        client = _make_anthropic_mock([])
        tool = WebSearchTool(client=client)
        assert tool.call(query="q") == "[empty search result]"

    def test_non_text_blocks_skipped(self):
        client = MagicMock()
        block_good = MagicMock(text="real text")
        block_no_text = MagicMock(spec=[])  # no .text attr at all
        block_empty_text = MagicMock(text="   ")
        resp = MagicMock(content=[block_no_text, block_good, block_empty_text])
        client.messages.create.return_value = resp
        tool = WebSearchTool(client=client)
        assert tool.call(query="q") == "real text"


# ── TestFetchUrlTool ─────────────────────────────────────


class TestFetchUrlTool:
    def test_success_html_stripped(self):
        body = (
            b"<html><head><title>t</title></head><body>"
            b"<p>hello</p><p>world</p></body></html>"
        )
        resp = _StubResponse(
            headers={"content-type": "text/html; charset=utf-8"},
            body=body,
        )
        client = _make_httpx_mock(resp)
        tool = FetchUrlTool(client=client)
        out = tool.call(url="https://example.com/")
        assert "hello" in out
        assert "world" in out
        assert "<p>" not in out
        assert "<head>" not in out

    def test_404_returns_error(self):
        resp = _StubResponse(status=404, reason="Not Found")
        client = _make_httpx_mock(resp)
        tool = FetchUrlTool(client=client)
        out = tool.call(url="https://example.com/missing")
        assert out.startswith("error: http 404")
        assert "Not Found" in out

    def test_content_length_header_exceeds_cap(self):
        huge = str(10 * 1024 * 1024)
        resp = _StubResponse(headers={"content-length": huge}, body=b"ignored")
        client = _make_httpx_mock(resp)
        tool = FetchUrlTool(client=client, max_bytes=5 * 1024 * 1024)
        out = tool.call(url="https://example.com/big")
        assert out.startswith("error: content-length")
        assert huge in out

    def test_non_text_content_type_returns_sentinel(self):
        resp = _StubResponse(
            headers={"content-type": "image/png"},
            body=b"\x89PNG\r\n",
        )
        client = _make_httpx_mock(resp)
        tool = FetchUrlTool(client=client)
        assert tool.call(url="https://example.com/pic") == "[non-text content: image/png]"

    def test_empty_url_returns_error(self):
        tool = FetchUrlTool(client=_make_httpx_mock(_StubResponse()))
        assert tool.call(url="") == "error: url is required"

    def test_non_http_scheme_rejected(self):
        tool = FetchUrlTool(client=_make_httpx_mock(_StubResponse()))
        assert tool.call(url="ftp://example.com/").startswith("error: url must be http(s)")

    def test_html_strip_removes_script_tags(self):
        out = strip_html("<script>evil()</script><p>good</p>")
        assert "good" in out
        assert "evil" not in out

    def test_default_client_sets_polite_user_agent(self):
        """When ``client=None`` the auto-created ``httpx.Client``
        must identify itself to avoid 403s from Wikimedia and similar
        hosts that reject the bare ``python-httpx/X`` default UA.

        Regression guard for the GAIA #7 Mercedes Sosa smoke where
        ``fetch_url`` on ``en.wikipedia.org`` returned 403 until the
        anchor guidance started routing traffic through this tool.
        """
        captured_kwargs: dict[str, object] = {}

        class _SpyClient:
            def __init__(self, **kwargs):
                captured_kwargs.update(kwargs)

            def get(self, _url):
                class _R:
                    status_code = 200
                    headers = {"content-type": "text/plain"}
                    reason_phrase = "ok"
                    text = "body"
                    content = b"body"
                    encoding = "utf-8"
                return _R()

            def close(self):
                pass

        import httpx as _httpx

        real_client = _httpx.Client
        _httpx.Client = _SpyClient  # type: ignore[assignment]
        try:
            tool = FetchUrlTool()
            tool.call(url="https://example.com/")
        finally:
            _httpx.Client = real_client  # type: ignore[assignment]

        headers = captured_kwargs.get("headers")
        assert isinstance(headers, dict)
        ua = headers.get("User-Agent", "")
        assert "concinno" in ua.lower()
        # URL pointer so Wikimedia operators can trace the tool
        assert "http" in ua.lower()

    def test_html_strip_removes_style_tags(self):
        out = strip_html("<style>body{color:red}</style><p>visible</p>")
        assert "visible" in out
        assert "color:red" not in out

    def test_http_error_wrapped(self):
        client = _make_httpx_mock(httpx.ConnectError("refused"))
        tool = FetchUrlTool(client=client)
        out = tool.call(url="https://example.com/")
        assert out.startswith("error: http ConnectError")
        assert "refused" in out

    def test_plain_text_passthrough(self):
        resp = _StubResponse(
            headers={"content-type": "text/plain"},
            body=b"simple plain text body",
        )
        client = _make_httpx_mock(resp)
        tool = FetchUrlTool(client=client)
        assert tool.call(url="https://example.com/") == "simple plain text body"

    def test_output_capped_at_max_output_chars(self):
        body = ("a" * 20000).encode("utf-8")
        resp = _StubResponse(headers={"content-type": "text/plain"}, body=body)
        client = _make_httpx_mock(resp)
        tool = FetchUrlTool(client=client, max_output_chars=50)
        out = tool.call(url="https://example.com/")
        assert len(out) <= 51
        assert out.endswith("\u2026")

    def test_json_content_type_not_treated_as_non_text(self):
        body = b'{"k": "v"}'
        resp = _StubResponse(
            headers={"content-type": "application/json"},
            body=body,
        )
        client = _make_httpx_mock(resp)
        tool = FetchUrlTool(client=client)
        out = tool.call(url="https://example.com/x.json")
        assert out == '{"k": "v"}'

    def test_body_size_exceeds_cap_without_content_length_header(self):
        body = b"x" * 2048
        resp = _StubResponse(headers={"content-type": "text/plain"}, body=body)
        client = _make_httpx_mock(resp)
        tool = FetchUrlTool(client=client, max_bytes=1024)
        out = tool.call(url="https://example.com/")
        assert out.startswith("error: body size")


# ── TestStripHtmlFallback ────────────────────────────────


class TestStripHtmlFallback:
    def test_valid_html_extracted(self):
        assert "hello" in strip_html("<p>hello</p>")

    def test_plain_text_passthrough(self):
        # No HTML tags → parser still returns the text.
        assert strip_html("just words") == "just words"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
