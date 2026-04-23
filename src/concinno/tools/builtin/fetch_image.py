"""``fetch_image`` tool — download an image and pass it to a vision-capable LLM.

@module concinno.tools.builtin.fetch_image
@responsibility Download a single image URL to base-64 and wrap it in a
    marker-delimited string so downstream providers (Sancio's
    ``_openai_messages_with_tools``) can rewrite the marker into the
    OpenAI ``{"type":"image_url","image_url":{"url":"data:..."}}``
    multimodal content block on the next LLM call. The ``Tool``
    protocol still returns ``str`` so the Concinno agent loop does
    not need a schema change — the multimodal split happens at the
    provider boundary, not inside the loop.
@dependencies stdlib only at import time; ``httpx`` is a hard
    Concinno dep (already in ``pyproject.toml``) and is imported
    lazily inside :meth:`call` so unit tests that mock the tool
    don't pull it.
@exports FetchImageTool, IMAGE_MARKER_START, IMAGE_MARKER_END,
    parse_image_marker

Why a marker string rather than a structured return
---------------------------------------------------
* ``concinno.tool_executor.Tool.call`` is typed ``-> str``. Changing
  that to ``-> Union[str, dict]`` would ripple through every
  existing built-in tool + every subclass + every executor test.
* Providers already have to translate loop messages to provider-
  specific payloads (OpenAI function calls, Anthropic blocks, etc.).
  Adding a small regex transform that upgrades ``IMAGE_MARKER`` to
  a multimodal block is the cheapest place to wire the conversion.
* Keeps the no-cheat contract cleanly separated — the tool function
  itself is pure (URL in, base64 out) and has no side-effect on the
  extractor, grader, or scoring path.

Design notes
------------
* 10 MB hard cap on fetched image bytes (``_MAX_IMAGE_BYTES``) —
  matches the general ``FetchUrlTool`` cap scaled up for typical
  photo / PNG / webp sizes. Pods that need larger uploads should
  wire a dedicated attachment channel rather than inline-base64.
* MIME sniffing is response-header-first, with a magic-byte
  fallback for the common formats (JPEG, PNG, GIF, WebP) so a
  mis-declaring server still produces a usable data URI.
* Errors come back as ``tool error: …`` — the loop runs the same
  error shape as every other Concinno built-in.
"""

from __future__ import annotations

import base64
import re
from typing import Any

#: Marker the tool wraps the base64 payload in so provider transforms
#: can identify & swap it for a multimodal ``image_url`` block.
IMAGE_MARKER_START = "__CONCINNO_IMAGE_B64__"
IMAGE_MARKER_END = "__CONCINNO_END_IMAGE__"

#: Regex to parse a marker-delimited image block out of a larger
#: string. Whitespace-tolerant around the base-64 payload so tool
#: output that's been concatenated or wrapped by the agent loop
#: still matches. The ``DOTALL`` flag lets the payload span lines.
IMAGE_MARKER_RE = re.compile(
    rf"{re.escape(IMAGE_MARKER_START)}\s*"
    r"(?P<b64>[A-Za-z0-9+/=\s]+?)\s*"
    rf"{re.escape(IMAGE_MARKER_END)}\s*"
    r"mime:\s*(?P<mime>\S+)",
    re.DOTALL,
)

_MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB

# Magic-byte fallback for mis-declared content types.
_MAGIC_MIME = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),  # webp-in-RIFF; good enough for routing
    (b"<?xml", "image/svg+xml"),
    (b"<svg", "image/svg+xml"),
)


def _sniff_mime(body: bytes, declared: str) -> str:
    """Prefer the response ``Content-Type`` but fall back to magic bytes."""
    dc = (declared or "").split(";")[0].strip().lower()
    if dc.startswith("image/"):
        return dc
    for magic, mime in _MAGIC_MIME:
        if body[: len(magic)] == magic:
            return mime
    # Last-resort default — most GAIA image links are JPEG.
    return "image/jpeg"


def parse_image_marker(
    content: str,
) -> list[dict[str, Any]] | None:
    """Split a string that may contain image markers into OpenAI blocks.

    Returns ``None`` when no marker is present (caller keeps the
    original string). Otherwise returns a ``[{type:"text"}, …,
    {type:"image_url"}, …]`` list ready to drop into an OpenAI-compat
    ``messages[*].content`` field. Whitespace-only text segments are
    dropped so the block order stays tight.
    """
    if IMAGE_MARKER_START not in content:
        return None
    parts: list[dict[str, Any]] = []
    last = 0
    matched = False
    for m in IMAGE_MARKER_RE.finditer(content):
        matched = True
        pre = content[last:m.start()]
        if pre.strip():
            parts.append({"type": "text", "text": pre})
        b64 = re.sub(r"\s+", "", m.group("b64"))
        mime = m.group("mime")
        parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
        })
        last = m.end()
    if not matched:
        return None
    tail = content[last:]
    if tail.strip():
        parts.append({"type": "text", "text": tail})
    return parts


class FetchImageTool:
    """Download an image URL and inline it so a vision LLM can see it.

    The tool returns a marker-delimited string carrying the base-64
    payload and mime. Sancio's OpenAI-compat provider rewrites the
    marker into a ``{"type":"image_url","image_url":{"url":"data:…"}}``
    block the next time it materializes a request — so the agent
    loop never needs to know about multimodal content shapes.

    Usage inside an agent::

        fetch_image(url="https://example.com/photo.jpg")
        # → "__CONCINNO_IMAGE_B64__\\n<base64>\\n__CONCINNO_END_IMAGE__\\nmime: image/jpeg"
        # Next iteration sees the image rendered into the prompt.
    """

    name = "fetch_image"
    description = (
        "Download an image from a URL so the model can see it. Use this "
        "when a question requires visual inspection (identifying a subject "
        "in a photo, reading text on an object, locating something in the "
        "background). Returns a marker string that the provider rewrites "
        "into an inline image for the next inference turn. One URL per "
        "call. For text documents use fetch_url instead."
    )

    def __init__(self, *, max_bytes: int = _MAX_IMAGE_BYTES) -> None:
        self._max_bytes = int(max_bytes)

    def call(self, **kwargs: Any) -> str:
        """Fetch ``url`` and return the marker-wrapped base64 payload.

        Keyword args
        ------------
        url : str
            HTTP(S) URL of the image to fetch. Non-HTTP schemes and
            URLs > ``max_bytes`` bytes are rejected with ``tool
            error: …``.
        """
        url = kwargs.get("url")
        if not isinstance(url, str) or not url.strip():
            return "tool error: fetch_image: 'url' argument is required"
        if not url.lower().startswith(("http://", "https://")):
            return (
                "tool error: fetch_image: URL must start with http:// or "
                "https://"
            )
        try:
            import httpx
        except ImportError:
            return (
                "tool error: fetch_image: the 'httpx' package is required "
                "but not installed"
            )
        try:
            with httpx.Client(follow_redirects=True, timeout=30.0) as c:
                r = c.get(url)
            r.raise_for_status()
        except Exception as exc:  # noqa: BLE001 — tool error, not server
            return f"tool error: fetch_image: HTTP {type(exc).__name__}: {exc}"

        body = r.content
        if len(body) > self._max_bytes:
            return (
                f"tool error: fetch_image: image is {len(body)} bytes "
                f"(cap {self._max_bytes})"
            )
        mime = _sniff_mime(body, r.headers.get("content-type", ""))
        b64 = base64.b64encode(body).decode()
        return (
            f"{IMAGE_MARKER_START}\n{b64}\n{IMAGE_MARKER_END}\n"
            f"mime: {mime}"
        )


__all__ = [
    "IMAGE_MARKER_END",
    "IMAGE_MARKER_RE",
    "IMAGE_MARKER_START",
    "FetchImageTool",
    "parse_image_marker",
]
