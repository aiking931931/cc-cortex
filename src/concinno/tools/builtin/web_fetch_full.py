"""concinno.tools.builtin.web_fetch_full — Playwright deep-extraction tool.

@module web_fetch_full
@responsibility Headless-browser URL fetcher that returns three artifacts
    a single ``WebSearchTool`` summary cannot provide:

    1. Rendered HTML after JavaScript execution (text-stripped, capped).
    2. A full-page PNG screenshot (base64 in-memory + optional disk save).
    3. The final URL after redirects + the document title.

    Used by the GAIA agent's ``Action: web_fetch_full(url)`` path when a
    multi-hop chain needs to **see** a page (e.g. spotting an entity in
    a background image / a small tombstone in a graveyard photo) rather
    than parse a search-engine summary.
@dependencies Playwright (already a hard concinno dep — see
    ``pyproject.toml``: ``playwright>=1.40``). Requires the chromium
    binary; install once via ``playwright install chromium``.
@exports WebFetchFullTool, web_fetch_full

Design notes
------------
* Conforms to :class:`concinno.tool_executor.Tool` — sync ``call(**kwargs)``
  returning a JSON-serialisable dict.
* Synchronous Playwright API (``sync_playwright``) matches concinno's
  sync Tool protocol. Each call spins up + tears down a browser
  (~3 s startup); acceptable for GAIA's per-question scope.
* Screenshot is saved to disk **and** returned as base64 so:
  - The Sonnet tool-use path can attach the image to a follow-up
    multimodal message without a second disk read.
  - The save path is recoverable from logs for evidence / debugging.
* HTML is text-stripped via :func:`concinno.tools.builtin.web.strip_html`
  to keep the agent's observation budget tight.
* All errors return as a dict with ``"error"`` key (string, prefixed
  ``"error: "`` / ``"warn: "``). The other keys carry partial / empty
  data so the agent can observe-then-retry rather than crash.
"""

from __future__ import annotations

import base64
import os
import tempfile
import uuid
from typing import Any

from .web import strip_html

#: Default page-load wait. ``networkidle`` waits until network stalls for
#: ~500 ms — best for image-heavy pages where the headstone text only
#: appears after lazy-loading. ``domcontentloaded`` is faster but misses
#: lazy images.
_DEFAULT_WAIT_UNTIL = "networkidle"

#: Hard timeout per page navigation. Beyond this we surface the error
#: rather than wait — GAIA loops are budget-bounded.
_DEFAULT_TIMEOUT_MS = 30_000

#: Maximum chars of HTML-stripped text to return to the agent. 8 000 keeps
#: contextual budget bounded but ~2× WebSearchTool's 4 000 cap because
#: full pages are denser than search summaries.
_DEFAULT_TEXT_CAP = 8_000

#: Maximum bytes the on-disk screenshot may consume. 8 MB is plenty for
#: full-page captures up to ~10k px tall and stays under typical model
#: file-attach limits.
_DEFAULT_SCREENSHOT_MAX_BYTES = 8 * 1024 * 1024

#: Chromium UA — identify ourselves so polite servers can distinguish us
#: from a generic scraper.
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (concinno-web_fetch_full; +https://pypi.org/"
    "project/concinno/) Chromium Playwright"
)


def _validate_url(url: str) -> str | None:
    """Return an error string, or ``None`` when the URL is acceptable."""
    if not url:
        return "error: url is required"
    if not (url.startswith("http://") or url.startswith("https://")):
        return f"error: url must be http(s); got {url[:80]!r}"
    return None


def _import_playwright() -> tuple[Any, Any] | str:
    """Lazy import of Playwright. Returns (sync_playwright, TimeoutError)
    on success, or an error string on failure."""
    try:
        from playwright.sync_api import (  # type: ignore[import-not-found]
            TimeoutError as PWTimeoutError,
        )
        from playwright.sync_api import (
            sync_playwright,
        )
        return sync_playwright, PWTimeoutError
    except ImportError as exc:  # pragma: no cover — env-specific
        return (
            f"error: playwright not installed ({exc}); install via "
            "`pip install playwright && playwright install chromium`"
        )


def _save_screenshot(
    png_bytes: bytes,
    save_path: str | None,
    result: dict[str, Any],
) -> None:
    """Encode ``png_bytes`` into ``result`` (base64 + disk path).

    Mutates ``result`` in place: sets ``screenshot_b64`` /
    ``screenshot_path`` / appends to ``error`` on warnings.
    """
    if not png_bytes:
        return

    if len(png_bytes) <= _DEFAULT_SCREENSHOT_MAX_BYTES:
        result["screenshot_b64"] = (
            base64.b64encode(png_bytes).decode("ascii")
        )
    else:
        result["screenshot_b64"] = None
        if not result["error"]:
            result["error"] = (
                f"warn: screenshot {len(png_bytes)}B exceeds inline "
                "cap; saved to disk only"
            )

    if save_path is None:
        save_path = os.path.join(
            tempfile.gettempdir(),
            f"concinno_web_fetch_{uuid.uuid4().hex[:8]}.png",
        )
    try:
        with open(save_path, "wb") as fh:
            fh.write(png_bytes)
        result["screenshot_path"] = save_path
    except OSError as exc:
        if not result["error"]:
            result["error"] = f"warn: screenshot save failed: {exc}"


def _capture_page(
    page: Any,
    url: str,
    wait_until: str,
    timeout_ms: int,
    pw_timeout_exc: type,
    text_cap: int,
    capture_screenshot: bool,
    save_screenshot_to: str | None,
    result: dict[str, Any],
) -> None:
    """Drive one Playwright ``page`` through navigation + extraction.

    Mutates ``result`` in place: sets ``title`` / ``final_url`` / ``text``
    / ``screenshot_*`` / ``error``. Soft-fails (partial result) on
    timeout — a partially rendered page often still carries the answer.
    """
    try:
        page.goto(url, wait_until=wait_until, timeout=timeout_ms)
    except pw_timeout_exc:
        result["error"] = (
            f"warn: navigation timeout {timeout_ms}ms; "
            "returning partial page"
        )

    try:
        html = page.content()
    except Exception as exc:  # noqa: BLE001
        html = ""
        if not result["error"]:
            result["error"] = f"error: page.content() failed: {exc}"

    try:
        result["title"] = page.title() or ""
    except Exception:  # noqa: BLE001
        result["title"] = ""
    try:
        result["final_url"] = page.url or url
    except Exception:  # noqa: BLE001
        result["final_url"] = url

    text = strip_html(html) if html else ""
    if len(text) > text_cap:
        text = text[:text_cap].rstrip() + "…"
    result["text"] = text

    if not capture_screenshot:
        return
    try:
        png_bytes = page.screenshot(full_page=True, type="png")
    except Exception as exc:  # noqa: BLE001
        if not result["error"]:
            result["error"] = f"warn: screenshot failed: {exc}"
        return
    _save_screenshot(png_bytes, save_screenshot_to, result)


def web_fetch_full(
    url: str,
    *,
    screenshot: bool = True,
    save_screenshot_to: str | None = None,
    wait_until: str = _DEFAULT_WAIT_UNTIL,
    timeout_ms: int = _DEFAULT_TIMEOUT_MS,
    text_cap: int = _DEFAULT_TEXT_CAP,
    user_agent: str = _DEFAULT_USER_AGENT,
) -> dict[str, Any]:
    """Headless-browser fetch returning text + screenshot + metadata.

    Parameters
    ----------
    url:
        HTTP/HTTPS URL to load.
    screenshot:
        When ``True`` (default), capture a full-page PNG and return it
        base64-encoded under ``screenshot_b64``.
    save_screenshot_to:
        Optional disk path. When ``None`` and ``screenshot=True``, a temp
        path is chosen and returned under ``screenshot_path``.
    wait_until:
        Playwright wait strategy. ``networkidle`` (default) is best for
        image-heavy pages.
    timeout_ms:
        Per-navigation timeout. 30 s default.
    text_cap:
        Maximum chars of HTML-stripped text returned. 8 000 default.
    user_agent:
        Browser UA string.

    Returns
    -------
    dict with keys: ``url``, ``final_url``, ``title``, ``text``,
        ``screenshot_b64`` (str | None), ``screenshot_path``
        (str | None), ``error`` (str | None).
    """
    result: dict[str, Any] = {
        "url": url,
        "final_url": "",
        "title": "",
        "text": "",
        "screenshot_b64": None,
        "screenshot_path": None,
        "error": None,
    }

    err = _validate_url(url)
    if err:
        result["error"] = err
        return result

    pw_or_err = _import_playwright()
    if isinstance(pw_or_err, str):
        result["error"] = pw_or_err
        return result
    sync_playwright, pw_timeout_exc = pw_or_err

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(user_agent=user_agent)
                page = context.new_page()
                _capture_page(
                    page=page,
                    url=url,
                    wait_until=wait_until,
                    timeout_ms=timeout_ms,
                    pw_timeout_exc=pw_timeout_exc,
                    text_cap=text_cap,
                    capture_screenshot=screenshot,
                    save_screenshot_to=save_screenshot_to,
                    result=result,
                )
            finally:
                try:
                    browser.close()
                except Exception:  # noqa: BLE001 — best-effort cleanup
                    pass
    except Exception as exc:  # noqa: BLE001 — top-level safety net
        result["error"] = (
            f"error: web_fetch_full failed: "
            f"{type(exc).__name__}: {exc}"
        )

    return result


class WebFetchFullTool:
    """Concinno Tool wrapper around :func:`web_fetch_full`.

    Conforms to :class:`concinno.tool_executor.Tool`. Returns the dict
    from :func:`web_fetch_full` directly so downstream agents can read
    structured fields without re-parsing.

    Constructor params (all optional, mirror :func:`web_fetch_full`
    keyword arguments).
    """

    name = "web_fetch_full"
    description = (
        "Headless-browser fetch one URL and return rendered text + "
        "full-page screenshot (base64) + final URL + title. Use after "
        "web_search has surfaced a candidate URL when the answer "
        "depends on what is visible on the page (small text in an "
        "image / tombstone / chart label) rather than the search "
        "summary text."
    )
    is_concurrency_safe = True  # read-only network

    def __init__(
        self,
        screenshot: bool = True,
        wait_until: str = _DEFAULT_WAIT_UNTIL,
        timeout_ms: int = _DEFAULT_TIMEOUT_MS,
        text_cap: int = _DEFAULT_TEXT_CAP,
        user_agent: str = _DEFAULT_USER_AGENT,
    ) -> None:
        self._screenshot = bool(screenshot)
        self._wait_until = str(wait_until)
        self._timeout_ms = int(timeout_ms)
        self._text_cap = int(text_cap)
        self._user_agent = str(user_agent)

    def call(self, **kwargs: Any) -> dict[str, Any]:
        url = str(kwargs.get("url") or "").strip()
        screenshot = bool(
            kwargs.get("screenshot", self._screenshot),
        )
        save_screenshot_to = kwargs.get("save_screenshot_to")
        wait_until = str(kwargs.get("wait_until", self._wait_until))
        timeout_ms = int(kwargs.get("timeout_ms", self._timeout_ms))
        text_cap = int(kwargs.get("text_cap", self._text_cap))
        user_agent = str(kwargs.get("user_agent", self._user_agent))

        return web_fetch_full(
            url,
            screenshot=screenshot,
            save_screenshot_to=save_screenshot_to,
            wait_until=wait_until,
            timeout_ms=timeout_ms,
            text_cap=text_cap,
            user_agent=user_agent,
        )
