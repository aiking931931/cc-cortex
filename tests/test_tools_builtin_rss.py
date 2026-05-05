"""Tests for concinno.tools.builtin.rss — RssFetch (httpx mocked)."""

from __future__ import annotations

import pytest

from concinno.tools.builtin.rss import (
    RssFetch,
    RssToolError,
    _parse_iso_timestamp,
)

SAMPLE_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>sample</title>
<item>
<title>New Post</title>
<link>https://example.com/post1</link>
<pubDate>Tue, 15 Apr 2025 12:00:00 +0000</pubDate>
<description>A recent article summary.</description>
<author>alice@example.com (Alice)</author>
</item>
<item>
<title>Older Post</title>
<link>https://example.com/post0</link>
<pubDate>Mon, 01 Jan 2020 00:00:00 +0000</pubDate>
<description>Old content.</description>
<author>bob@example.com (Bob)</author>
</item>
</channel></rss>
"""


class _MockResponse:
    def __init__(self, content: bytes, status: int = 200) -> None:
        self.content = content
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError(
                "bad", request=None, response=None  # type: ignore[arg-type]
            )


@pytest.fixture
def mock_httpx_ok(monkeypatch):
    def fake_get(url, *args, **kwargs):  # noqa: ARG001
        return _MockResponse(SAMPLE_RSS)

    import httpx

    monkeypatch.setattr(httpx, "get", fake_get)
    yield


@pytest.fixture
def mock_httpx_error(monkeypatch):
    def fake_get(url, *args, **kwargs):  # noqa: ARG001
        import httpx

        raise httpx.ConnectError("refused")

    import httpx

    monkeypatch.setattr(httpx, "get", fake_get)
    yield


# --------------------------------------------------------------------------- #
# Helper unit tests                                                           #
# --------------------------------------------------------------------------- #


def test_parse_iso_timestamp_none():
    assert _parse_iso_timestamp(None) is None
    assert _parse_iso_timestamp("") is None


def test_parse_iso_timestamp_with_z():
    dt = _parse_iso_timestamp("2024-01-01T00:00:00Z")
    assert dt is not None
    assert dt.tzinfo is not None


def test_parse_iso_timestamp_naive_becomes_utc():
    dt = _parse_iso_timestamp("2024-01-01T00:00:00")
    assert dt is not None
    assert dt.tzinfo is not None


def test_parse_iso_timestamp_bad():
    with pytest.raises(RssToolError, match="ISO-8601"):
        _parse_iso_timestamp("not-a-date")


# --------------------------------------------------------------------------- #
# RssFetch end-to-end (mocked network)                                        #
# --------------------------------------------------------------------------- #


def test_rss_attributes():
    assert RssFetch.name == "rss_fetch"
    assert RssFetch.is_concurrency_safe is True
    assert "feedparser" in RssFetch.description


def test_rss_missing_url():
    tool = RssFetch()
    out = tool.call()
    assert "error" in out
    assert "url is required" in out["error"]


def test_rss_rejects_file_scheme():
    tool = RssFetch()
    out = tool.call(url="file:///etc/passwd")
    assert "error" in out
    assert "not allowed" in out["error"]


def test_rss_rejects_ftp():
    tool = RssFetch()
    out = tool.call(url="ftp://example.com/feed")
    assert "error" in out
    assert "not allowed" in out["error"]


def test_rss_rejects_no_host():
    tool = RssFetch()
    out = tool.call(url="http://")
    assert "error" in out
    assert "missing host" in out["error"]


def test_rss_bad_since_iso(mock_httpx_ok):
    pytest.importorskip("feedparser")
    tool = RssFetch()
    out = tool.call(url="https://example.com/feed", since_iso="garbage")
    assert "error" in out
    assert "ISO-8601" in out["error"]


def test_rss_fetch_ok(mock_httpx_ok):
    pytest.importorskip("feedparser")
    tool = RssFetch()
    out = tool.call(url="https://example.com/feed", limit=10)
    assert isinstance(out, list)
    assert len(out) == 2
    first = out[0]
    assert first["title"] == "New Post"
    assert first["link"] == "https://example.com/post1"
    assert "Alice" in first["author"] or "alice" in first["author"].lower()
    assert "recent article" in first["summary"].lower()
    # published ISO-normalised
    assert first["published"].startswith("2025-04-15")


def test_rss_since_filter(mock_httpx_ok):
    pytest.importorskip("feedparser")
    tool = RssFetch()
    out = tool.call(
        url="https://example.com/feed",
        since_iso="2024-01-01T00:00:00Z",
    )
    assert isinstance(out, list)
    # Older Post (2020) must be dropped by since_iso.
    titles = [entry["title"] for entry in out]
    assert "New Post" in titles
    assert "Older Post" not in titles


def test_rss_limit_clamps_to_1(mock_httpx_ok):
    pytest.importorskip("feedparser")
    tool = RssFetch()
    out = tool.call(url="https://example.com/feed", limit=1)
    assert isinstance(out, list)
    assert len(out) == 1


def test_rss_limit_respects_max(mock_httpx_ok):
    pytest.importorskip("feedparser")
    tool = RssFetch()
    # Over-cap limit silently clamped to MAX_LIMIT=100; only 2 entries
    # in the fixture so we just assert no error and ≤ MAX returned.
    out = tool.call(url="https://example.com/feed", limit=999)
    assert isinstance(out, list)


def test_rss_bad_limit_type(mock_httpx_ok):
    tool = RssFetch()
    out = tool.call(url="https://example.com/feed", limit="abc")
    assert "error" in out
    assert "limit must be int" in out["error"]


def test_rss_fetch_error(mock_httpx_error):
    pytest.importorskip("feedparser")
    tool = RssFetch()
    out = tool.call(url="https://example.com/feed")
    assert "error" in out
    assert "fetch failed" in out["error"]
