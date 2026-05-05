"""concinno.tools.builtin.pdf — PDF text + table extraction tools.

@module pdf
@responsibility Two tools covering the "read a local PDF" surface without
    pulling PDF parsers into Concinno's core dep graph:

    * :class:`PdfRead` — plain-text extraction via ``pypdf`` (BSD-3).
      Fastest path, works on 95% of text-bearing PDFs. Caller chooses
      page subset via ``pages="all" | "3" | "1-5" | "1,3,7"``.
    * :class:`PdfExtract` — table + text per-page via ``pdfplumber`` (MIT).
      Slower but returns structured rows; used when the caller needs the
      tabular shape (invoices, statements, GAIA-style spreadsheet PDFs).

@dependencies pypdf (optional, ``[pdf]`` extras), pdfplumber (optional,
    ``[pdf]`` extras). Both imported lazily inside ``call`` — Concinno's
    zero-dep core is preserved when the extras are not installed.

@exports PdfRead, PdfExtract, PdfToolError

Safety contract
---------------
Both tools reject non-local inputs:

    * Path must be an existing regular file on the filesystem.
    * ``file://``, ``http(s)://``, ``ftp://`` schemes are refused up-front
      to prevent the model from silently fetching a remote PDF through
      the tool (``httpx`` is for network tools; PDF tools are local-only).
    * Path traversal is NOT blocked beyond existence — Concinno trusts
      the caller's sandbox. Downstream consumers that need a chroot
      should wrap these tools.

Errors are returned as strings prefixed with ``"error: ..."`` so the
multi-step agent loop can observe and retry rather than raise — matches
the shape set by :class:`DateCalcTool` / :class:`PythonExecTool` in this
package.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Upper cap on pages returned by a single call. Keeps worst-case output
#: size bounded — a 10,000-page PDF will truncate rather than blow the
#: prompt.
DEFAULT_MAX_PAGES = 500

#: URL schemes we refuse outright. ``file://`` is included because that
#: path can resolve to network mounts on some OSes.
_REJECTED_SCHEMES = ("http://", "https://", "ftp://", "ftps://", "file://")


class PdfToolError(ValueError):
    """Raised for caller-visible PDF tool misuse. Caught inside ``call``
    and returned as a string — the multi-step loop prefers observations
    over exceptions."""


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _validate_path(path: str) -> Path:
    """Reject URLs, normalise, and confirm the file exists.

    Returns a :class:`pathlib.Path`. Raises :class:`PdfToolError` on any
    issue so callers can forward the message verbatim to the agent.
    """
    if not path:
        raise PdfToolError("path is required")
    lowered = path.lower()
    for scheme in _REJECTED_SCHEMES:
        if lowered.startswith(scheme):
            raise PdfToolError(
                f"remote / URL paths not supported (got {scheme!r}); "
                "PDF tools are local-filesystem only"
            )
    p = Path(path)
    if not p.exists():
        raise PdfToolError(f"file not found: {path}")
    if not p.is_file():
        raise PdfToolError(f"not a regular file: {path}")
    return p


def _parse_pages_spec(
    spec: str, total_pages: int
) -> list[int]:
    """Resolve ``"all" | "3" | "1-5" | "1,3,7"`` to a 0-indexed page list.

    Pages in the spec are 1-indexed (human convention). Output is
    0-indexed for array access. Silently clamps to ``[0, total_pages)``
    and de-duplicates while preserving order.
    """
    spec = (spec or "all").strip().lower()
    if spec == "all":
        return list(range(min(total_pages, DEFAULT_MAX_PAGES)))

    pages: list[int] = []
    seen: set[int] = set()
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            lo_s, hi_s = chunk.split("-", 1)
            try:
                lo = int(lo_s)
                hi = int(hi_s)
            except ValueError as exc:
                raise PdfToolError(
                    f"bad page range {chunk!r}: {exc}"
                ) from exc
            if lo > hi:
                lo, hi = hi, lo
            for i in range(lo, hi + 1):
                idx = i - 1
                if 0 <= idx < total_pages and idx not in seen:
                    pages.append(idx)
                    seen.add(idx)
        else:
            try:
                i = int(chunk)
            except ValueError as exc:
                raise PdfToolError(
                    f"bad page number {chunk!r}: {exc}"
                ) from exc
            idx = i - 1
            if 0 <= idx < total_pages and idx not in seen:
                pages.append(idx)
                seen.add(idx)
        if len(pages) >= DEFAULT_MAX_PAGES:
            break
    if not pages:
        raise PdfToolError(
            f"page spec {spec!r} resolved to zero pages (pdf has "
            f"{total_pages})"
        )
    return pages


# ---------------------------------------------------------------------------
# PdfRead — text extraction via pypdf
# ---------------------------------------------------------------------------


class PdfRead:
    """Extract plain text from a local PDF file via ``pypdf``.

    Attributes:
        name: ``"pdf_read"`` — what the LLM refers to in tool calls.
        description: Short summary shown to the LLM.
        is_concurrency_safe: ``True`` — pure I/O + CPU, no shared state.
    """

    name: str = "pdf_read"
    description: str = (
        "Extract plain text from a local PDF file via pypdf. "
        "Params: path(str) — existing local file; "
        "pages(str='all'|'3'|'1-5'|'1,3,7') — 1-indexed page selector. "
        "Returns concatenated page text with '\\n\\n--- page N ---\\n\\n' "
        "separators. Local files only — URLs rejected."
    )
    is_concurrency_safe: bool = True

    def call(self, **kwargs: Any) -> str:
        path = kwargs.get("path", "")
        pages = kwargs.get("pages", "all")
        try:
            p = _validate_path(path)
            try:
                import pypdf  # type: ignore[import-not-found]
            except ImportError as exc:
                return (
                    "error: pypdf not installed. "
                    "Run: pip install 'concinno[pdf]' "
                    f"(details: {exc})"
                )
            try:
                reader = pypdf.PdfReader(str(p))
            except Exception as exc:  # noqa: BLE001
                return f"error: failed to open pdf: {exc}"
            total = len(reader.pages)
            if total == 0:
                return "error: pdf has zero pages"
            try:
                indices = _parse_pages_spec(str(pages), total)
            except PdfToolError as exc:
                return f"error: {exc}"

            chunks: list[str] = []
            for idx in indices:
                try:
                    text = reader.pages[idx].extract_text() or ""
                except Exception as exc:  # noqa: BLE001
                    text = f"[extract failed: {exc}]"
                chunks.append(f"--- page {idx + 1} ---\n\n{text}")
            return "\n\n".join(chunks)
        except PdfToolError as exc:
            return f"error: {exc}"


# ---------------------------------------------------------------------------
# PdfExtract — tables + text per page via pdfplumber
# ---------------------------------------------------------------------------


class PdfExtract:
    """Extract tables + text from a single PDF page via ``pdfplumber``.

    Attributes:
        name: ``"pdf_extract"`` — what the LLM refers to in tool calls.
        description: Short summary shown to the LLM.
        is_concurrency_safe: ``True`` — pdfplumber opens the file per
            call and closes before returning; no shared state.
    """

    name: str = "pdf_extract"
    description: str = (
        "Extract tables and text from ONE page of a local PDF via "
        "pdfplumber. Params: path(str) — existing local file; "
        "page(int) — 1-indexed page number. "
        "Returns {'tables': list[list[list[str]]], 'text': str}. "
        "Local files only — URLs rejected."
    )
    is_concurrency_safe: bool = True

    def call(self, **kwargs: Any) -> dict[str, Any]:
        path = kwargs.get("path", "")
        page = kwargs.get("page", None)
        try:
            if page is None:
                raise PdfToolError("page is required (1-indexed int)")
            try:
                page_num = int(page)
            except (TypeError, ValueError) as exc:
                raise PdfToolError(
                    f"page must be an integer, got {page!r}"
                ) from exc
            if page_num < 1:
                raise PdfToolError("page must be >= 1 (1-indexed)")

            p = _validate_path(path)

            try:
                import pdfplumber  # type: ignore[import-not-found]
            except ImportError as exc:
                return {
                    "error": (
                        "pdfplumber not installed. "
                        "Run: pip install 'concinno[pdf]' "
                        f"(details: {exc})"
                    ),
                    "tables": [],
                    "text": "",
                }

            try:
                with pdfplumber.open(str(p)) as pdf:
                    total = len(pdf.pages)
                    if page_num > total:
                        raise PdfToolError(
                            f"page {page_num} out of range "
                            f"(pdf has {total} pages)"
                        )
                    pg = pdf.pages[page_num - 1]
                    raw_tables = pg.extract_tables() or []
                    # Normalise cells to str (pdfplumber may return None).
                    tables: list[list[list[str]]] = [
                        [
                            ["" if cell is None else str(cell) for cell in row]
                            for row in table
                        ]
                        for table in raw_tables
                    ]
                    text = pg.extract_text() or ""
            except PdfToolError:
                raise
            except Exception as exc:  # noqa: BLE001
                return {
                    "error": f"failed to extract page {page_num}: {exc}",
                    "tables": [],
                    "text": "",
                }
            return {"tables": tables, "text": text}
        except PdfToolError as exc:
            return {"error": str(exc), "tables": [], "text": ""}


__all__ = [
    "PdfExtract",
    "PdfRead",
    "PdfToolError",
]
