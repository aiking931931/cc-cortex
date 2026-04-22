"""concinno.tools.builtin.html — HTML → clean markdown / text.

@module html
@responsibility A single :class:`HtmlToText` tool wrapping ``trafilatura``
    (Apache-2.0), the current SOTA for LLM-grade main-content extraction
    from a raw HTML string. Trafilatura strips navigation, ads, boilerplate,
    and returns the article body in a form small models can reason over
    without parroting site chrome.

@dependencies trafilatura (optional, ``[html]`` extras). Imported lazily
    inside ``call`` — Concinno's zero-dep core is preserved when the
    extras are not installed.

@exports HtmlToText, HtmlToolError

Why not BeautifulSoup + manual strip?
-------------------------------------
BeautifulSoup gives raw DOM access but *nothing* about content vs chrome;
we'd be reinventing Readability-style heuristics. Trafilatura is
benchmark-leading for this exact task (see ACL 2021 paper; 5.8k stars)
and is a single lazy import.

Input contract
--------------
Tool takes an HTML *string* — not a URL. Fetching HTML is a separate
concern that belongs in a network tool (or the caller's Shell / httpx
usage). This keeps the tool deterministic and offline-testable, and
prevents the model from triggering surprise network traffic.
"""

from __future__ import annotations

from typing import Any


class HtmlToolError(ValueError):
    """Raised for caller-visible HtmlToText misuse. Caught inside ``call``
    and returned as a string."""


class HtmlToText:
    """Convert an HTML string into clean markdown / plain text via
    ``trafilatura``.

    Attributes:
        name: ``"html_to_text"`` — what the LLM refers to in tool calls.
        description: Short summary shown to the LLM.
        is_concurrency_safe: ``True`` — trafilatura.extract is pure on
            the input string and holds no global state we care about.
    """

    name: str = "html_to_text"
    description: str = (
        "Convert an HTML string to clean markdown-style text via "
        "trafilatura (SOTA main-content extraction). "
        "Params: html(str) — raw HTML content; "
        "include_links(bool=False) — preserve link URLs inline; "
        "include_tables(bool=True) — keep table structure. "
        "Returns a single string. Input is a string, not a URL — fetch "
        "separately."
    )
    is_concurrency_safe: bool = True

    def call(self, **kwargs: Any) -> str:
        html = kwargs.get("html", None)
        include_links = bool(kwargs.get("include_links", False))
        include_tables = bool(kwargs.get("include_tables", True))

        if html is None:
            return "error: html is required (string)"
        if not isinstance(html, str):
            return f"error: html must be a str, got {type(html).__name__}"
        if not html.strip():
            return ""

        try:
            import trafilatura  # type: ignore[import-not-found]
        except ImportError as exc:
            return (
                "error: trafilatura not installed. "
                "Run: pip install 'concinno[html]' "
                f"(details: {exc})"
            )

        try:
            result = trafilatura.extract(
                html,
                include_links=include_links,
                include_tables=include_tables,
                output_format="markdown",
            )
        except Exception as exc:  # noqa: BLE001
            return f"error: trafilatura extract failed: {exc}"
        if result is None:
            # Trafilatura returns None for "nothing extractable" (no
            # article-like content, or the HTML is too small). Return an
            # empty string rather than error: callers can distinguish
            # "empty content" from "tool failed" by checking for the
            # "error:" prefix.
            return ""
        return result


__all__ = ["HtmlToText", "HtmlToolError"]
