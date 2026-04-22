"""concinno.tools.builtin.rss — RSS / Atom feed fetcher.

@module rss
@responsibility A single :class:`RssFetch` tool wrapping ``feedparser``
    (BSD-2, 2.4k stars) for agent-grade feed ingestion. feedparser
    handles the parsing zoo (RSS 2.0, Atom 1.0, RDF, iTunes extensions),
    we add:

    * URL scheme allowlist (``http``, ``https`` only — ``file://``
      rejected to avoid local path traversal disguised as feed).
    * ``limit`` cap (1 <= limit <= 100) — prevents a million-entry
      firehose from saturating the prompt.
    * ``since_iso`` filter — trim older entries before returning.
    * Network fetch via ``httpx`` (already in core deps) so we reuse the
      same TLS / proxy config as other Concinno network code.

@dependencies feedparser (optional, ``[rss]`` extras), httpx (core dep).

@exports RssFetch, RssToolError
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse


class RssToolError(ValueError):
    """Raised for caller-visible RssFetch misuse. Caught inside ``call``
    and returned as an error payload."""


#: Hard upper bound on ``limit`` to stop abuse. The model is unlikely to
#: need more than ~50 entries for any practical summarisation task.
MAX_LIMIT = 100

#: Network timeout for the feed fetch. Short enough that a hung feed
#: doesn't stall the agent loop; long enough that a slow-but-alive feed
#: still completes.
FETCH_TIMEOUT_SECONDS = 15

_ALLOWED_SCHEMES = frozenset({"http", "https"})


def _parse_iso_timestamp(value: str | None) -> datetime | None:
    """Parse ``since_iso`` if provided. Returns timezone-aware UTC.

    Accepts ISO-8601 with or without ``Z`` suffix. Any malformed input
    raises :class:`RssToolError` with a caller-friendly message.
    """
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise RssToolError(
            f"since_iso must be str, got {type(value).__name__}"
        )
    cleaned = value.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError as exc:
        raise RssToolError(
            f"since_iso not ISO-8601 ({value!r}): {exc}"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _entry_datetime(entry: Any) -> datetime | None:
    """Best-effort datetime from a feedparser entry.

    feedparser exposes ``published_parsed`` / ``updated_parsed`` as
    ``time.struct_time``; we normalise to timezone-aware UTC so the
    since-filter comparison is honest.
    """
    for attr in ("published_parsed", "updated_parsed"):
        tm = getattr(entry, attr, None) or (
            entry.get(attr) if isinstance(entry, dict) else None
        )
        if tm is None:
            continue
        try:
            return datetime(*tm[:6], tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
    return None


def _get_attr(entry: Any, key: str, default: str = "") -> str:
    """Read ``key`` from an entry (object or dict) and stringify."""
    value = getattr(entry, key, None)
    if value is None and isinstance(entry, dict):
        value = entry.get(key)
    if value is None:
        return default
    return str(value)


def _get_author(entry: Any) -> str:
    """Extract author with feedparser's multiple possible locations."""
    for key in ("author", "creator", "publisher"):
        value = _get_attr(entry, key, "")
        if value:
            return value
    # Fallback: authors[0].name
    authors = getattr(entry, "authors", None)
    if authors is None and isinstance(entry, dict):
        authors = entry.get("authors")
    if authors and isinstance(authors, (list, tuple)) and authors:
        first = authors[0]
        if isinstance(first, dict):
            return str(first.get("name") or first.get("email") or "")
    return ""


class RssFetch:
    """Fetch + parse an RSS / Atom feed.

    Attributes:
        name: ``"rss_fetch"`` — what the LLM refers to in tool calls.
        description: Short summary shown to the LLM.
        is_concurrency_safe: ``True`` — each call creates its own
            ``httpx`` client and feedparser has no global mutable state
            we rely on.
    """

    name: str = "rss_fetch"
    description: str = (
        "Fetch + parse an RSS / Atom feed via feedparser. "
        "Params: url(str) — http(s) only, file:// rejected; "
        "limit(int=20, max=100) — entries returned; "
        "since_iso(str|None) — ISO-8601 cutoff, entries older dropped. "
        "Returns list[{title, link, published, summary, author}]."
    )
    is_concurrency_safe: bool = True

    def call(self, **kwargs: Any) -> list[dict[str, str]] | dict[str, str]:
        url = kwargs.get("url", None)
        limit_raw = kwargs.get("limit", 20)
        since_iso = kwargs.get("since_iso", None)

        if not url or not isinstance(url, str):
            return {"error": "url is required (non-empty str)"}
        parsed = urlparse(url)
        if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
            return {
                "error": (
                    f"url scheme {parsed.scheme!r} not allowed; "
                    "use http or https"
                )
            }
        if not parsed.netloc:
            return {"error": f"url missing host: {url!r}"}

        try:
            limit = int(limit_raw)
        except (TypeError, ValueError):
            return {
                "error": (
                    f"limit must be int, got {type(limit_raw).__name__}"
                )
            }
        limit = max(1, min(limit, MAX_LIMIT))

        try:
            since_dt = _parse_iso_timestamp(since_iso)
        except RssToolError as exc:
            return {"error": str(exc)}

        try:
            import feedparser  # type: ignore[import-not-found]
        except ImportError as exc:
            return {
                "error": (
                    "feedparser not installed. "
                    "Run: pip install 'concinno[rss]' "
                    f"(details: {exc})"
                )
            }

        try:
            import httpx
        except ImportError as exc:  # pragma: no cover — httpx is core dep
            return {"error": f"httpx not available: {exc}"}

        try:
            response = httpx.get(
                url,
                timeout=FETCH_TIMEOUT_SECONDS,
                follow_redirects=True,
                headers={"User-Agent": "concinno-rss/2.15"},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            return {"error": f"fetch failed: {exc}"}

        # feedparser.parse accepts bytes directly — no temp file.
        parsed_feed = feedparser.parse(response.content)
        entries = getattr(parsed_feed, "entries", []) or []

        results: list[dict[str, str]] = []
        for entry in entries:
            entry_dt = _entry_datetime(entry)
            if since_dt is not None:
                if entry_dt is None:
                    # Without a parseable date we can't compare; drop
                    # rather than include by accident.
                    continue
                if entry_dt < since_dt:
                    continue

            published = ""
            if entry_dt is not None:
                published = entry_dt.isoformat()
            else:
                # feedparser also exposes a raw string — use it when
                # struct_time parsing failed.
                published = _get_attr(entry, "published") or _get_attr(
                    entry, "updated"
                )

            results.append(
                {
                    "title": _get_attr(entry, "title"),
                    "link": _get_attr(entry, "link"),
                    "published": published,
                    "summary": _get_attr(entry, "summary")
                    or _get_attr(entry, "description"),
                    "author": _get_author(entry),
                }
            )
            if len(results) >= limit:
                break

        return results


__all__ = ["RssFetch", "RssToolError"]
