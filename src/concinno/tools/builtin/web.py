"""concinno.tools.builtin.web — WebSearch / FetchUrl network-read tools.

@module web
@responsibility Two read-only network tools conforming to the Concinno
    :class:`Tool` protocol (sync ``call(**kwargs)``):

    * :class:`WebSearchTool` — proxy a query through Anthropic's first-class
      ``web_search_20250305`` tool and return the model's merged text summary.
    * :class:`FetchUrlTool` — HTTP GET a single URL, strip HTML when the
      content-type is HTML, and return a capped text body.
@dependencies stdlib only at import time (``html.parser``). ``httpx`` and
    ``anthropic`` are hard Concinno deps (see ``pyproject.toml``) but the
    anthropic client is still constructed lazily inside :meth:`_get_client`
    so unit tests can run without credentials and callers who do not use
    WebSearch never pay construction cost.
@exports WebSearchTool, FetchUrlTool, strip_html

Design notes
------------
Ported from Sancio (``projects/persona-api/src/persona/tools/``) to become
the canonical location for network-read tools in the Concinno library.

Differences from the Sancio originals:

* ``execute(args: dict)`` -> ``call(**kwargs)`` — Concinno's Tool protocol
  uses sync kwargs, not async dict-args. FetchUrl therefore uses
  ``httpx.Client`` (sync) rather than ``httpx.AsyncClient``.
* ``schema()`` is removed — Concinno does not use the OpenAI function-call
  schema shape; executors introspect ``name`` / ``description`` directly.
* ``description`` is a class attribute (string), not a method.
* All error paths return a short string beginning with ``"error: ..."``
  so the multi-step ``ToolExecutor`` loop can observe-then-retry rather
  than raise (matches the Sancio contract verbatim).

HTML stripping is stdlib-only (``html.parser``) — avoid pulling BeautifulSoup
or lxml into Concinno's dep graph. Good enough for agent fact-finding; not a
structured-extraction substitute.
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# Constants — mirror Sancio defaults so consumers see the same limits.
# ---------------------------------------------------------------------------

#: Default Anthropic model for WebSearch. Pinned to Claude Sonnet 4.6 to
#: match Sancio's production config; callers may override via constructor.
_DEFAULT_MODEL = "claude-sonnet-4-6"

#: Number of ``web_search_20250305`` invocations per ``messages.create`` turn.
_DEFAULT_MAX_USES = 3

#: Output token budget for the summarisation reply.
_DEFAULT_MAX_TOKENS = 1500

#: Hard cap on the returned text length (characters), to keep the agent's
#: observation budget tight.
_DEFAULT_OUTPUT_CAP = 4000

#: Hard cap on fetched response body (bytes). 5 MB matches Sancio.
_FETCH_MAX_BYTES = 5 * 1024 * 1024

#: Default HTTP timeout in seconds.
_FETCH_TIMEOUT_S = 15.0

#: Output character cap for ``FetchUrlTool``.
_FETCH_MAX_OUTPUT_CHARS = 8000


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------


class _TextExtractor(HTMLParser):
    """Minimal HTML -> text extractor using only the stdlib.

    Drops ``<script>`` / ``<style>`` / ``<noscript>`` / ``<head>`` content
    entirely, collapses whitespace, and preserves paragraph breaks so the
    output is readable by an LLM downstream.
    """

    _SKIP = {"script", "style", "noscript", "head"}
    _BLOCK_TAGS = {
        "p", "br", "li", "tr", "div", "section",
        "h1", "h2", "h3", "h4", "h5", "h6",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if data.strip():
            self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        lines = [ln.strip() for ln in raw.splitlines()]
        return "\n".join(ln for ln in lines if ln)


def strip_html(raw: str) -> str:
    """Best-effort HTML -> plain text. Returns ``raw`` on parser failure."""
    try:
        parser = _TextExtractor()
        parser.feed(raw)
        parser.close()
        return parser.text()
    except Exception:  # noqa: BLE001 — fall back to raw on any parser crash
        return raw


# ---------------------------------------------------------------------------
# WebSearchTool
# ---------------------------------------------------------------------------


class WebSearchTool:
    """Proxy a natural-language query through Anthropic WebSearch.

    Conforms to :class:`concinno.tool_executor.Tool`. Returns a plain string
    (the model's merged text summary, capped at ``output_cap``). All
    error conditions are returned as strings beginning with ``"error: ..."``
    so the multi-step agent loop can observe and retry rather than crash.

    Constructor params
    ------------------
    client:
        Optional pre-constructed ``anthropic.Anthropic`` instance (or a
        duck-typed mock). When ``None`` the client is built lazily inside
        :meth:`call` so tests that never invoke the tool never import
        anthropic.
    model / max_uses / max_tokens / output_cap:
        Tunables mirroring the Sancio defaults.
    """

    name = "web_search"
    description = (
        "Search the web via Claude Sonnet WebSearch and return a concise "
        "text summary. Use a specific query; the assistant will search, "
        "read, and summarise the most relevant facts."
    )
    is_concurrency_safe = True  # read-only network

    def __init__(
        self,
        client: Any | None = None,
        model: str = _DEFAULT_MODEL,
        max_uses: int = _DEFAULT_MAX_USES,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        output_cap: int = _DEFAULT_OUTPUT_CAP,
    ) -> None:
        self._client = client
        self._model = model
        self._max_uses = int(max_uses)
        self._max_tokens = int(max_tokens)
        self._output_cap = int(output_cap)

    def _get_client(self) -> Any:
        """Return the injected or lazily-constructed Anthropic client.

        Lazy construction keeps module import free of the anthropic
        package being on ``sys.path`` at the time :mod:`web` is first
        imported (it is a hard dep in ``pyproject.toml``, but lazy
        construction also defers credential discovery until first use).
        """
        if self._client is not None:
            return self._client
        try:
            import anthropic  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover — env-specific
            msg = (
                "WebSearchTool requires the `anthropic` package; install "
                "`concinno[llm]` or `pip install anthropic` to enable it."
            )
            raise RuntimeError(msg) from exc
        self._client = anthropic.Anthropic()
        return self._client

    def call(self, **kwargs: Any) -> str:
        query = str(kwargs.get("query") or "").strip()
        if not query:
            return "error: query is required"

        try:
            client = self._get_client()
        except RuntimeError as exc:
            return f"error: {exc}"

        try:
            resp = client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                tools=[{
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": self._max_uses,
                }],
                messages=[{
                    "role": "user",
                    "content": (
                        f"Search: {query}\n\n"
                        "Summarise the key facts precisely. Include "
                        "exact numbers, names, dates, and cite sources "
                        "inline when relevant."
                    ),
                }],
            )
        except Exception as exc:  # noqa: BLE001 — surface as tool error
            return f"error: web_search failed: {type(exc).__name__}: {exc}"

        parts: list[str] = []
        content = getattr(resp, "content", None) or []
        for block in content:
            text = getattr(block, "text", None)
            if isinstance(text, str) and text.strip():
                parts.append(text)
        merged = "\n".join(parts).strip()
        if not merged:
            return "[empty search result]"
        if len(merged) > self._output_cap:
            merged = merged[: self._output_cap].rstrip() + "\u2026"
        return merged


# ---------------------------------------------------------------------------
# FetchUrlTool
# ---------------------------------------------------------------------------


class FetchUrlTool:
    """Fetch one URL via HTTP GET and return text (<= output cap).

    Conforms to :class:`concinno.tool_executor.Tool`. Uses ``httpx.Client``
    (sync) so it matches the Concinno Tool protocol. Size cap, timeout,
    and non-text sentinel behavior are ported verbatim from Sancio.
    """

    name = "fetch_url"
    description = (
        "HTTP GET one URL and return its text content. Strips HTML tags "
        "when the content-type is text/html. Rejects payloads larger "
        "than 5 MB. Output capped at 8000 chars."
    )
    is_concurrency_safe = True  # read-only network

    def __init__(
        self,
        client: httpx.Client | None = None,
        max_bytes: int = _FETCH_MAX_BYTES,
        timeout_s: float = _FETCH_TIMEOUT_S,
        max_output_chars: int = _FETCH_MAX_OUTPUT_CHARS,
    ) -> None:
        self._client = client
        self._owns_client = client is None
        self._max_bytes = int(max_bytes)
        self._timeout_s = float(timeout_s)
        self._max_output_chars = int(max_output_chars)

    # ------------------------------------------------------------------
    # Helpers — keep ``call`` under the structural length budget.
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_url(url: str) -> str | None:
        """Return an error string, or ``None`` when the URL is acceptable."""
        if not url:
            return "error: url is required"
        if not (url.startswith("http://") or url.startswith("https://")):
            return f"error: url must be http(s); got {url[:80]!r}"
        return None

    def _check_content_length_header(
        self, resp: httpx.Response,
    ) -> str | None:
        """If the server declares ``content-length`` and it exceeds our
        cap, return an error string so the caller can bail without
        reading the body. Returns ``None`` to proceed.
        """
        declared = resp.headers.get("content-length")
        if not declared:
            return None
        try:
            if int(declared) > self._max_bytes:
                return (
                    f"error: content-length {declared} exceeds "
                    f"cap {self._max_bytes}"
                )
        except ValueError:
            return None
        return None

    @staticmethod
    def _is_non_text_ctype(ctype: str) -> bool:
        """True when content-type is present and not a text-ish format."""
        if not ctype:
            return False
        return not (
            ctype.startswith("text/")
            or ctype.startswith("application/json")
            or ctype.startswith("application/xml")
            or "xml" in ctype
        )

    def _extract_text(self, resp: httpx.Response, ctype: str) -> str:
        """Decode + optionally HTML-strip + cap."""
        body = resp.content
        if len(body) > self._max_bytes:
            return f"error: body size {len(body)} exceeds cap {self._max_bytes}"
        try:
            text = body.decode(resp.encoding or "utf-8", errors="replace")
        except LookupError:
            text = body.decode("utf-8", errors="replace")
        if "html" in ctype:
            text = strip_html(text)
        text = text.strip()
        if len(text) > self._max_output_chars:
            text = text[: self._max_output_chars].rstrip() + "\u2026"
        return text or "[empty response]"

    # ------------------------------------------------------------------
    # Public entry point.
    # ------------------------------------------------------------------

    def call(self, **kwargs: Any) -> str:
        url = str(kwargs.get("url") or "").strip()
        err = self._validate_url(url)
        if err is not None:
            return err

        client = self._client
        owns_client = self._owns_client and client is None
        if client is None:
            client = httpx.Client(
                timeout=self._timeout_s,
                follow_redirects=True,
                headers={
                    # Wikimedia & other servers reject the default
                    # ``python-httpx/X`` UA. Identify the tool politely
                    # so we are not mistaken for a scraper.
                    "User-Agent": (
                        "concinno/2.34 (+https://pypi.org/"
                        "project/concinno/) httpx"
                    ),
                    "Accept": (
                        "text/html,application/xhtml+xml,"
                        "application/xml;q=0.9,*/*;q=0.8"
                    ),
                },
            )
        try:
            try:
                resp = client.get(url)
            except httpx.HTTPError as exc:
                return f"error: http {type(exc).__name__}: {exc}"

            size_err = self._check_content_length_header(resp)
            if size_err is not None:
                return size_err

            if resp.status_code >= 400:
                return (
                    f"error: http {resp.status_code} for {url[:80]} "
                    f"({resp.reason_phrase or 'no reason'})"
                )

            ctype = (resp.headers.get("content-type") or "").lower()
            if self._is_non_text_ctype(ctype):
                return f"[non-text content: {ctype.split(';')[0].strip()}]"

            return self._extract_text(resp, ctype)
        finally:
            if owns_client:
                client.close()
