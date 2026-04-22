"""Tests for concinno.tools.builtin.pdf — PdfRead + PdfExtract.

Uses ``reportlab`` (optional; test skips when missing) to materialise a
tiny PDF in a ``tmp_path`` fixture so we never depend on a checked-in
binary asset. When ``pypdf`` / ``pdfplumber`` themselves are missing,
each ``pytest.importorskip`` short-circuits — matches the
``[project.optional-dependencies].pdf`` extras contract.
"""

from __future__ import annotations

import pytest

from concinno.tools.builtin.pdf import (
    PdfExtract,
    PdfRead,
    PdfToolError,
    _parse_pages_spec,
    _validate_path,
)


# --------------------------------------------------------------------------- #
# Helper fixtures                                                             #
# --------------------------------------------------------------------------- #


@pytest.fixture
def tiny_pdf(tmp_path):
    """Write a 3-page PDF with predictable text per page."""
    reportlab = pytest.importorskip("reportlab.pdfgen.canvas")
    pdf_path = tmp_path / "tiny.pdf"
    c = reportlab.Canvas(str(pdf_path))
    for i, label in enumerate(("alpha", "beta", "gamma"), start=1):
        c.drawString(100, 750, f"page {i} marker: {label}")
        c.showPage()
    c.save()
    return pdf_path


# --------------------------------------------------------------------------- #
# Path / spec unit tests                                                      #
# --------------------------------------------------------------------------- #


def test_validate_path_rejects_http_url():
    with pytest.raises(PdfToolError, match="remote / URL paths not supported"):
        _validate_path("https://example.com/evil.pdf")


def test_validate_path_rejects_file_scheme():
    with pytest.raises(PdfToolError, match="remote / URL paths not supported"):
        _validate_path("file:///etc/passwd")


def test_validate_path_rejects_empty():
    with pytest.raises(PdfToolError, match="path is required"):
        _validate_path("")


def test_validate_path_missing(tmp_path):
    with pytest.raises(PdfToolError, match="file not found"):
        _validate_path(str(tmp_path / "nope.pdf"))


def test_parse_pages_all():
    assert _parse_pages_spec("all", 3) == [0, 1, 2]


def test_parse_pages_range():
    assert _parse_pages_spec("1-3", 5) == [0, 1, 2]


def test_parse_pages_csv():
    assert _parse_pages_spec("1,3,5", 10) == [0, 2, 4]


def test_parse_pages_mixed():
    assert _parse_pages_spec("1,3-4", 10) == [0, 2, 3]


def test_parse_pages_clamp():
    # Pages beyond total silently dropped, not raised.
    assert _parse_pages_spec("1,99", 3) == [0]


def test_parse_pages_empty_raises():
    with pytest.raises(PdfToolError, match="zero pages"):
        _parse_pages_spec("99", 3)


def test_parse_pages_reversed_range():
    # Caller supplied descending — we sort and still return ascending.
    assert _parse_pages_spec("5-1", 10) == [0, 1, 2, 3, 4]


# --------------------------------------------------------------------------- #
# PdfRead                                                                     #
# --------------------------------------------------------------------------- #


def test_pdf_read_attributes():
    assert PdfRead.name == "pdf_read"
    assert PdfRead.is_concurrency_safe is True
    assert "pypdf" in PdfRead.description


def test_pdf_read_missing_file(tmp_path):
    tool = PdfRead()
    out = tool.call(path=str(tmp_path / "missing.pdf"))
    assert out.startswith("error: file not found")


def test_pdf_read_rejects_url():
    tool = PdfRead()
    out = tool.call(path="https://example.com/x.pdf")
    assert out.startswith("error: remote / URL paths not supported")


def test_pdf_read_all_pages(tiny_pdf):
    pytest.importorskip("pypdf")
    tool = PdfRead()
    out = tool.call(path=str(tiny_pdf), pages="all")
    # Separator present for each page.
    assert "--- page 1 ---" in out
    assert "--- page 2 ---" in out
    assert "--- page 3 ---" in out
    assert "alpha" in out
    assert "beta" in out
    assert "gamma" in out


def test_pdf_read_subset(tiny_pdf):
    pytest.importorskip("pypdf")
    tool = PdfRead()
    out = tool.call(path=str(tiny_pdf), pages="2")
    assert "--- page 2 ---" in out
    assert "beta" in out
    assert "alpha" not in out
    assert "gamma" not in out


# --------------------------------------------------------------------------- #
# PdfExtract                                                                  #
# --------------------------------------------------------------------------- #


def test_pdf_extract_attributes():
    assert PdfExtract.name == "pdf_extract"
    assert PdfExtract.is_concurrency_safe is True
    assert "pdfplumber" in PdfExtract.description


def test_pdf_extract_missing_page():
    tool = PdfExtract()
    out = tool.call(path="any")
    assert "error" in out
    assert "page is required" in out["error"]


def test_pdf_extract_bad_page_type(tmp_path, tiny_pdf):
    pytest.importorskip("pdfplumber")
    tool = PdfExtract()
    out = tool.call(path=str(tiny_pdf), page="abc")
    assert "error" in out
    assert "page must be an integer" in out["error"]


def test_pdf_extract_rejects_url():
    tool = PdfExtract()
    out = tool.call(path="http://example.com/foo.pdf", page=1)
    assert out["error"].startswith("remote / URL paths not supported")


def test_pdf_extract_page_out_of_range(tiny_pdf):
    pytest.importorskip("pdfplumber")
    tool = PdfExtract()
    out = tool.call(path=str(tiny_pdf), page=99)
    assert "error" in out
    assert "out of range" in out["error"]


def test_pdf_extract_text(tiny_pdf):
    pytest.importorskip("pdfplumber")
    tool = PdfExtract()
    out = tool.call(path=str(tiny_pdf), page=1)
    assert "tables" in out
    assert "text" in out
    assert isinstance(out["tables"], list)
    assert "alpha" in out["text"]
