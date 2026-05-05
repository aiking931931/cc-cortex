"""concinno.tools.builtin.wiki — Wikipedia section lookup tool.

Positioning
-----------
Weak models (Gemma4-Q4_K_M, small instruction-tuned variants) struggle
with the multi-step sequence "web_search → identify URL → fetch_url →
locate section → count entries" that an accurate Wikipedia lookup
requires. Each step is a separate tool-call the model has to emit in
correct JSON shape; anything less than near-perfect instruction
following degenerates into reasoning loops (GAIA #7 Mercedes Sosa
smoke: 23k-char "I'll use web_search to find URL" repetition until
the agent loop bailed).

This tool collapses that pipeline into a single call. The model
emits one structured tool-call — `fetch_wikipedia_section(subject,
section)` — and receives only the requested section's text. This
removes URL-resolution ambiguity (no "is it Mercedes_Sosa or
Mercedes_Sosa_(singer)?" second-guessing), removes section-finding
(the MediaWiki REST API returns each section pre-sliced), and caps
the output at the same 8000-char budget `FetchUrlTool` uses.

API choice
----------
The `mobile-sections` REST endpoint
(`/api/rest_v1/page/mobile-sections/{title}`) returns a JSON object
with `lead` + `remaining.sections[]`, each section pre-parsed with
`id`, `toclevel`, `line` (header text), `anchor`, and `text` (HTML).
Versus `action=parse` on the MediaWiki API, this endpoint:

- Already splits sections so we do not need to re-parse the TOC.
- Has a stable response schema (documented in Wikimedia REST API
  reference).
- Follows redirects server-side (e.g. "Mercedes Sosa" →
  "Mercedes_Sosa" is resolved transparently).

Errors
------
All failure modes return strings beginning with ``"error: ..."`` so
the calling agent can observe the failure and retry with a different
``subject``/``section`` argument rather than crash.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any

import httpx

from .web import strip_html

#: Default request timeout for the MediaWiki REST API.
_WIKI_TIMEOUT_S = 15.0

#: Cap the section text at the same budget ``FetchUrlTool`` uses so
#: the agent loop never receives more tokens than it expects.
_WIKI_MAX_OUTPUT_CHARS = 8000

#: Body-size cap before we decline to parse (5 MB matches
#: ``FetchUrlTool``'s ``_FETCH_MAX_BYTES``).
_WIKI_MAX_BYTES = 5 * 1024 * 1024

#: Polite User-Agent identifying the tool to Wikimedia operators.
#: Wikimedia's API etiquette policy asks tools to identify themselves
#: with a URL pointer; a bare ``python-httpx/X`` UA is a 403 magnet.
_WIKI_USER_AGENT = (
    "concinno/2.34 (+https://pypi.org/project/concinno/) "
    "fetch_wikipedia_section httpx"
)


class FetchWikipediaSectionTool:
    """Fetch one section from an English Wikipedia article.

    Conforms to :class:`concinno.tool_executor.Tool`. Returns the
    requested section's plain text (HTML stripped, capped at
    ``max_output_chars``). Section matching is case-insensitive and
    accepts either the exact header text or a unique prefix (e.g.
    "Studio albums", "studio", "Bibliography").

    Constructor params
    ------------------
    client:
        Optional pre-constructed :class:`httpx.Client`. When ``None``
        a client with the polite User-Agent is built lazily inside
        :meth:`call`. Tests can inject a mock that implements
        ``.get(url) -> response``.
    timeout_s / max_bytes / max_output_chars:
        Tunables mirroring :class:`FetchUrlTool`.
    """

    name = "fetch_wikipedia_section"
    description = (
        "Fetch one section from an English Wikipedia article in a "
        "single tool call. Takes ``subject`` (the article name, e.g. "
        "'Mercedes Sosa') and ``section`` (the header text, e.g. "
        "'Studio albums'). Returns only that section's plain text, "
        "HTML stripped, capped at 8000 chars. Prefer this over "
        "fetch_url+manual-section-finding when the question asks for "
        "a count or list from a named section of a person's article."
    )
    is_concurrency_safe = True  # read-only network

    def __init__(
        self,
        client: httpx.Client | None = None,
        timeout_s: float = _WIKI_TIMEOUT_S,
        max_bytes: int = _WIKI_MAX_BYTES,
        max_output_chars: int = _WIKI_MAX_OUTPUT_CHARS,
    ) -> None:
        self._client = client
        self._owns_client = client is None
        self._timeout_s = float(timeout_s)
        self._max_bytes = int(max_bytes)
        self._max_output_chars = int(max_output_chars)

    # ------------------------------------------------------------------
    # Public entry point.
    # ------------------------------------------------------------------

    def call(self, **kwargs: Any) -> str:
        subject = str(kwargs.get("subject") or "").strip()
        section = str(kwargs.get("section") or "").strip()
        if not subject:
            return "error: subject is required"
        if not section:
            return "error: section is required"

        client = self._client
        owns = self._owns_client and client is None
        if client is None:
            client = httpx.Client(
                timeout=self._timeout_s,
                follow_redirects=True,
                headers={"User-Agent": _WIKI_USER_AGENT},
            )
        try:
            # Step 1: fetch the section TOC to resolve ``section`` to an
            # index. MediaWiki ``action=parse`` with ``prop=sections``
            # returns a compact JSON of {index, line, toclevel, anchor}
            # triples that is stable across article revisions.
            toc = self._api_get(
                client,
                subject=subject,
                params={"prop": "sections"},
            )
            if isinstance(toc, str):
                return toc  # error string
            sections = (toc.get("parse") or {}).get("sections") or []

            matched = self._find_section_entry(sections, section)
            if matched is None:
                available = [
                    (s.get("line") or "").strip()
                    for s in sections
                    if (s.get("line") or "").strip()
                ]
                preview = ", ".join(available[:12]) or "(none)"
                return (
                    f"error: section {section!r} not found in Wikipedia "
                    f"article for {subject!r}. Available headers: "
                    f"{preview}"
                )
            index = matched.get("index")
            if not index:
                return (
                    f"error: Wikipedia sections TOC missing 'index' for "
                    f"section {section!r}"
                )

            # Step 2: fetch that section's HTML and strip to plain text.
            body = self._api_get(
                client,
                subject=subject,
                params={"prop": "text", "section": str(index)},
            )
            if isinstance(body, str):
                return body  # error string
            html = (
                ((body.get("parse") or {}).get("text") or {}).get("*")
                or ""
            )
            text = strip_html(html).strip()
            if not text:
                return (
                    f"error: section {section!r} for {subject!r} "
                    f"resolved but is empty."
                )
            if len(text) > self._max_output_chars:
                text = text[: self._max_output_chars].rstrip() + "…"
            return text
        finally:
            if owns:
                client.close()

    # ------------------------------------------------------------------
    # Helpers.
    # ------------------------------------------------------------------

    def _api_get(
        self, client: httpx.Client, *, subject: str, params: dict,
    ) -> dict | str:
        """Hit MediaWiki ``action=parse`` with ``params`` + standard
        fields. Returns parsed JSON or an ``error: ...`` string.

        The ``action=parse`` endpoint is stable, follows redirects
        server-side, and does not 404 on the actual page (it returns
        an ``error`` object in the JSON payload when the title is
        missing) — we surface both transport and payload failures as
        strings so the agent loop can observe them like any other
        tool error.
        """
        q = {
            "action": "parse",
            "page": subject,
            "format": "json",
            "redirects": "1",
            **params,
        }
        url = (
            "https://en.wikipedia.org/w/api.php?"
            + urllib.parse.urlencode(q)
        )
        try:
            resp = client.get(url)
        except httpx.HTTPError as exc:
            return f"error: http {type(exc).__name__}: {exc}"
        if resp.status_code >= 400:
            return (
                f"error: http {resp.status_code} for Wikipedia "
                f"subject {subject!r} "
                f"({resp.reason_phrase or 'no reason'})"
            )
        raw = resp.content
        if len(raw) > self._max_bytes:
            return (
                f"error: body size {len(raw)} exceeds cap "
                f"{self._max_bytes}"
            )
        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            return f"error: Wikipedia API returned non-JSON body: {exc}"
        # Payload-level errors (missing title / invalid section) arrive
        # in ``data['error']`` per MediaWiki convention.
        if isinstance(data.get("error"), dict):
            info = data["error"].get("info") or data["error"].get("code")
            code = data["error"].get("code") or "error"
            if code == "missingtitle":
                return (
                    f"error: subject {subject!r} not found on English "
                    f"Wikipedia. Check spelling or try the canonical "
                    f"article title."
                )
            return f"error: Wikipedia API {code}: {info}"
        return data

    @staticmethod
    def _find_section_entry(
        sections: list[dict], wanted: str,
    ) -> dict | None:
        """Match by ``line`` (header text), case-insensitive.

        Tries exact match first, then startswith for weak-model
        imprecision ("studio" → "Studio albums"), then substring.
        Returns the section dict or ``None`` when no match.
        """
        target = wanted.strip().lower()
        for s in sections:
            if (s.get("line") or "").strip().lower() == target:
                return s
        for s in sections:
            if (s.get("line") or "").strip().lower().startswith(target):
                return s
        for s in sections:
            if target in (s.get("line") or "").strip().lower():
                return s
        return None

    # ------------------------------------------------------------------
    # Back-compat: keep the legacy classmethod names so downstream
    # callers / tests that imported ``_find_section`` / ``_list_headers``
    # / ``_build_url`` / ``_iter_sections`` from the ``mobile-sections``
    # implementation do not break on import. These are thin adapters
    # over the ``action=parse`` helpers above.
    # ------------------------------------------------------------------

    @staticmethod
    def _build_url(subject: str) -> str:
        """Return the ``action=parse`` URL for fetching sections TOC.

        Kept for back-compat tests. Real traffic goes through
        :meth:`_api_get`.
        """
        q = {
            "action": "parse",
            "page": subject,
            "format": "json",
            "redirects": "1",
            "prop": "sections",
        }
        return (
            "https://en.wikipedia.org/w/api.php?"
            + urllib.parse.urlencode(q)
        )

    @staticmethod
    def _iter_sections(data: dict) -> list[dict]:
        """Normalise a sections payload into a flat list.

        Accepts either:
        - The ``action=parse&prop=sections`` shape:
          ``{"parse": {"sections": [...]}}``
        - A legacy ``mobile-sections`` shape with
          ``lead.sections[]`` + ``remaining.sections[]`` (for
          back-compat with older test fixtures).
        """
        if isinstance(data.get("parse"), dict):
            secs = (data["parse"].get("sections") or [])
            return [s for s in secs if isinstance(s, dict)]
        out: list[dict] = []
        lead = data.get("lead") or {}
        for s in lead.get("sections") or []:
            if isinstance(s, dict):
                out.append(s)
        remaining = data.get("remaining") or {}
        for s in remaining.get("sections") or []:
            if isinstance(s, dict):
                out.append(s)
        return out

    @classmethod
    def _find_section(
        cls, data: dict, wanted: str,
    ) -> dict | None:
        """Back-compat wrapper accepting either API shape."""
        return cls._find_section_entry(
            cls._iter_sections(data), wanted,
        )

    @classmethod
    def _list_headers(cls, data: dict) -> list[str]:
        out: list[str] = []
        for s in cls._iter_sections(data):
            line = (s.get("line") or "").strip()
            if line:
                out.append(line)
        return out
