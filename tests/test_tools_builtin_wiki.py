"""Tests for :class:`concinno.tools.builtin.wiki.FetchWikipediaSectionTool`.

Covers
------
- Tool protocol conformance (name / description / is_concurrency_safe).
- URL canonicalisation (spaces → underscores, percent-encoding).
- Section lookup (exact match / prefix match / substring match / miss).
- HTTP 404 / 5xx error shaping.
- Polite User-Agent on the auto-created client (Wikimedia politeness).
- Max-output cap.
- Empty subject / empty section rejection.
"""

from __future__ import annotations

import json
from typing import Any

from concinno.tools.builtin.wiki import FetchWikipediaSectionTool


# ──────────────────────── Test fixtures ──────────────────────────


def _make_wiki_response(
    *,
    status: int = 200,
    body: dict | None = None,
    reason: str = "OK",
) -> Any:
    class _R:
        status_code = status
        reason_phrase = reason
        headers: dict = {"content-type": "application/json"}

        @property
        def content(self) -> bytes:
            if body is None:
                return b"{}"
            return json.dumps(body).encode("utf-8")

    return _R()


class _StubClient:
    """Minimal httpx.Client surface the tool uses.

    Accepts either a single response (served for every GET) or a
    sequence of responses (served in order to match the two-call
    TOC + section-text pattern ``call()`` now uses).
    """

    def __init__(self, response: Any) -> None:
        if isinstance(response, list):
            self._responses = list(response)
            self._single: Any = None
        else:
            self._responses = None
            self._single = response
        self.last_url: str | None = None
        self.urls: list[str] = []

    def get(self, url: str) -> Any:
        self.last_url = url
        self.urls.append(url)
        if self._responses is not None:
            return self._responses.pop(0)
        return self._single

    def close(self) -> None:
        pass


# Sample Wikipedia mobile-sections JSON (legacy — kept for back-compat
# tests on the ``_iter_sections`` / ``_find_section`` helper-level).
_SAMPLE_WIKI_BODY: dict = {
    "lead": {
        "sections": [{"id": 0, "line": "", "text": "<p>lead</p>"}],
    },
    "remaining": {
        "sections": [
            {"id": 1, "line": "Biography", "text": "<p>bio</p>"},
            {"id": 2, "line": "Discography", "text": "<p>disco</p>"},
            {
                "id": 3,
                "line": "Studio albums",
                "text": (
                    "<ul>"
                    "<li>Misa Criolla (2000)</li>"
                    "<li>Corazón Libre (2005)</li>"
                    "<li>Cantora, un Viaje Íntimo (2009)</li>"
                    "</ul>"
                ),
            },
            {
                "id": 4,
                "line": "Live albums",
                "text": "<p>live list</p>",
            },
        ],
    },
}


# Sample action=parse responses (new API shape).
_SAMPLE_TOC_BODY: dict = {
    "parse": {
        "title": "Mercedes Sosa",
        "pageid": 476992,
        "sections": [
            {"index": "1", "line": "Biography", "toclevel": 1},
            {"index": "2", "line": "Discography", "toclevel": 1},
            {"index": "3", "line": "Studio albums", "toclevel": 2},
            {"index": "4", "line": "Live albums", "toclevel": 2},
        ],
    },
}

_SAMPLE_SECTION_HTML: dict = {
    "parse": {
        "title": "Mercedes Sosa",
        "text": {
            "*": (
                "<table class=\"wikitable\">"
                "<tr><td>2000</td><td>Misa Criolla</td></tr>"
                "<tr><td>2005</td><td>Corazón Libre</td></tr>"
                "<tr><td>2009</td><td>Cantora, un Viaje Íntimo</td></tr>"
                "</table>"
            ),
        },
    },
}


# ──────────────────────── Protocol ──────────────────────────


class TestProtocolConformance:
    def test_name(self):
        assert FetchWikipediaSectionTool.name == "fetch_wikipedia_section"

    def test_description_mentions_single_call(self):
        desc = FetchWikipediaSectionTool.description.lower()
        assert "single tool call" in desc or "one tool call" in desc

    def test_is_concurrency_safe(self):
        assert FetchWikipediaSectionTool.is_concurrency_safe is True


# ──────────────────────── URL build ──────────────────────────


class TestUrlBuild:
    def test_subject_is_passed_to_mediawiki_api(self):
        url = FetchWikipediaSectionTool._build_url("Mercedes Sosa")
        assert "page=Mercedes+Sosa" in url or "page=Mercedes%20Sosa" in url

    def test_mediawiki_action_parse_endpoint(self):
        url = FetchWikipediaSectionTool._build_url("Foo")
        assert url.startswith("https://en.wikipedia.org/w/api.php?")
        assert "action=parse" in url

    def test_parenthesised_disambig_preserved(self):
        url = FetchWikipediaSectionTool._build_url(
            "Mercedes Sosa (singer)",
        )
        assert "singer" in url


# ──────────────────────── Section match ──────────────────────────


class TestSectionMatch:
    def test_exact_match(self):
        m = FetchWikipediaSectionTool._find_section(
            _SAMPLE_WIKI_BODY, "Studio albums",
        )
        assert m is not None and m["line"] == "Studio albums"

    def test_case_insensitive(self):
        m = FetchWikipediaSectionTool._find_section(
            _SAMPLE_WIKI_BODY, "studio albums",
        )
        assert m is not None and m["line"] == "Studio albums"

    def test_prefix_match(self):
        m = FetchWikipediaSectionTool._find_section(
            _SAMPLE_WIKI_BODY, "Studio",
        )
        assert m is not None and m["line"] == "Studio albums"

    def test_substring_match(self):
        # "albums" appears in both Studio+Live; exact-first fall-through
        # then startswith, then substring — substring picks the first
        # in insertion order.
        m = FetchWikipediaSectionTool._find_section(
            _SAMPLE_WIKI_BODY, "albums",
        )
        assert m is not None
        assert m["line"] in ("Studio albums", "Live albums")

    def test_no_match_returns_none(self):
        m = FetchWikipediaSectionTool._find_section(
            _SAMPLE_WIKI_BODY, "Awards",
        )
        assert m is None

    def test_list_headers_flattens_lead_and_remaining(self):
        headers = FetchWikipediaSectionTool._list_headers(
            _SAMPLE_WIKI_BODY,
        )
        assert "Biography" in headers
        assert "Studio albums" in headers
        assert "Live albums" in headers


# ──────────────────────── End-to-end call ──────────────────────────


class TestEndToEndCall:
    def test_happy_path_returns_section_text(self):
        # Two-call pattern: TOC first, then section HTML.
        responses = [
            _make_wiki_response(body=_SAMPLE_TOC_BODY),
            _make_wiki_response(body=_SAMPLE_SECTION_HTML),
        ]
        client = _StubClient(responses)
        tool = FetchWikipediaSectionTool(client=client)
        out = tool.call(subject="Mercedes Sosa", section="Studio albums")
        assert "Misa Criolla" in out
        assert "Corazón Libre" in out
        assert "Cantora" in out
        # HTML stripped
        assert "<table>" not in out
        assert "<tr>" not in out
        # Two API calls made (TOC + section), second with section=<idx>
        assert len(client.urls) == 2
        assert "section=3" in client.urls[1]

    def test_missing_subject_error(self):
        tool = FetchWikipediaSectionTool(client=_StubClient(None))
        assert (
            tool.call(subject="", section="Studio albums")
            == "error: subject is required"
        )

    def test_missing_section_error(self):
        tool = FetchWikipediaSectionTool(client=_StubClient(None))
        assert (
            tool.call(subject="Foo", section="")
            == "error: section is required"
        )

    def test_missingtitle_returns_friendly_error(self):
        # MediaWiki returns ``{"error": {"code": "missingtitle", ...}}``
        # in the JSON payload (HTTP 200) for unknown titles.
        body = {
            "error": {
                "code": "missingtitle",
                "info": "The page you specified doesn't exist.",
            },
        }
        resp = _make_wiki_response(body=body)
        tool = FetchWikipediaSectionTool(client=_StubClient(resp))
        out = tool.call(subject="NoSuchPerson", section="Studio albums")
        assert "NoSuchPerson" in out
        assert "not found" in out.lower()

    def test_section_not_found_lists_available(self):
        resp = _make_wiki_response(body=_SAMPLE_TOC_BODY)
        tool = FetchWikipediaSectionTool(client=_StubClient(resp))
        out = tool.call(
            subject="Mercedes Sosa", section="Non-existent Section",
        )
        assert "section 'Non-existent Section' not found" in out
        assert "Studio albums" in out  # available headers listed

    def test_http_5xx_error_shaped(self):
        resp = _make_wiki_response(status=503, reason="Service Unavailable")
        tool = FetchWikipediaSectionTool(client=_StubClient(resp))
        out = tool.call(subject="Foo", section="Bar")
        assert out.startswith("error: http 503")

    def test_output_capped(self):
        huge_section_html = "<p>" + ("x" * 20000) + "</p>"
        toc_body = {
            "parse": {
                "sections": [
                    {"index": "1", "line": "Big", "toclevel": 1},
                ],
            },
        }
        section_body = {"parse": {"text": {"*": huge_section_html}}}
        client = _StubClient([
            _make_wiki_response(body=toc_body),
            _make_wiki_response(body=section_body),
        ])
        tool = FetchWikipediaSectionTool(
            client=client, max_output_chars=100,
        )
        out = tool.call(subject="X", section="Big")
        assert len(out) <= 101
        assert out.endswith("…")


# ──────────────────────── Polite UA ──────────────────────────


class TestDefaultClientUserAgent:
    """When ``client=None`` the auto-created ``httpx.Client`` must
    carry a polite User-Agent identifying the tool to Wikimedia
    operators — the REST API returns 403 for the bare
    ``python-httpx/X`` default.
    """

    def test_default_client_sets_user_agent(self):
        captured_kwargs: dict[str, object] = {}

        class _SpyClient:
            def __init__(self, **kwargs):
                captured_kwargs.update(kwargs)

            def get(self, _url):
                return _make_wiki_response(body=_SAMPLE_WIKI_BODY)

            def close(self):
                pass

        import httpx as _httpx

        real = _httpx.Client
        _httpx.Client = _SpyClient  # type: ignore[assignment]
        try:
            tool = FetchWikipediaSectionTool()
            tool.call(subject="Foo", section="Biography")
        finally:
            _httpx.Client = real  # type: ignore[assignment]

        headers = captured_kwargs.get("headers")
        assert isinstance(headers, dict)
        ua = headers.get("User-Agent", "")
        assert "concinno" in ua.lower()
        assert "http" in ua.lower()
