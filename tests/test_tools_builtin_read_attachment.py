"""Tests for :mod:`concinno.tools.builtin.read_attachment`.

Covers:

* Tool contract (``name`` / ``description`` / ``is_concurrency_safe``).
* xlsx dispatch → TSV with ``[Sheet: name]`` header.
* csv dispatch → TSV rows.
* Plain-text extension dispatch (json / jsonl / pdb / txt).
* Unknown-extension fallback: text if printable, structured error if binary.
* Missing file / directory / empty path / blocked device → ``error: ...``.
* Lazy openpyxl import failure path (monkeypatched) for the
  zero-runtime-deps contract — a clean ``error: ...`` string.

Output-shape assertions are deliberately loose (contains-based) so downstream
format tweaks do not break every test at once. Hard invariants — sheet
header presence, TSV cell separator, ``error:`` prefix on failure — are
strict.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import Workbook

from concinno.tools.builtin.read_attachment import (
    DEFAULT_MAX_ROWS_PER_SHEET,
    ReadAttachmentError,
    ReadAttachmentTool,
)

# ── Contract ─────────────────────────────────────────


def test_contract_attrs() -> None:
    t = ReadAttachmentTool()
    assert t.name == "read_attachment"
    assert t.is_concurrency_safe is True
    desc = t.description.lower()
    # Description must name what formats it handles + the anti-retry
    # guidance so weak models do not loop on binary formats.
    assert "xlsx" in desc
    assert "csv" in desc
    assert "plain" in desc or "text" in desc


# ── xlsx happy path ──────────────────────────────────


@pytest.fixture
def xlsx_path(tmp_path: Path) -> Path:
    """A two-sheet workbook: first sheet has data, second is empty."""
    wb = Workbook()
    ws1 = wb.active
    assert ws1 is not None
    ws1.title = "Inventory"
    ws1.append(["Title", "Format", "Year"])
    ws1.append(["Time-Parking 2: Parallel Universe", "Blu-Ray", 1991])
    ws1.append(["Other Film", "DVD", 2005])
    ws2 = wb.create_sheet("Notes")  # left empty on purpose
    del ws2
    p = tmp_path / "inv.xlsx"
    wb.save(p)
    return p


def test_xlsx_rendering(xlsx_path: Path) -> None:
    t = ReadAttachmentTool()
    out = t.call(path=str(xlsx_path))
    # Sheet markers present.
    assert "[Sheet: Inventory]" in out
    assert "[Sheet: Notes]" in out
    # Data rows preserved, TSV-separated.
    assert "Title\tFormat\tYear" in out
    assert "Time-Parking 2: Parallel Universe\tBlu-Ray\t1991" in out
    # Empty sheet annotation.
    assert "[empty sheet]" in out


def test_xlsx_row_cap(tmp_path: Path) -> None:
    """Row cap fires and annotates the truncation."""
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    # Overshoot the cap by a healthy margin.
    for i in range(DEFAULT_MAX_ROWS_PER_SHEET + 20):
        ws.append([i, f"row-{i}"])
    p = tmp_path / "big.xlsx"
    wb.save(p)
    out = ReadAttachmentTool().call(path=str(p))
    assert f"Row cap {DEFAULT_MAX_ROWS_PER_SHEET}" in out


def test_xlsx_cell_sanitization(tmp_path: Path) -> None:
    """Tabs/newlines in a cell must be replaced to keep TSV framing."""
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.append(["a\tb", "c\nd"])
    p = tmp_path / "mess.xlsx"
    wb.save(p)
    out = ReadAttachmentTool().call(path=str(p))
    # Row has exactly one tab separator between the two cells → no
    # extra tabs leaked from the cell content.
    data_line = [ln for ln in out.splitlines() if "a" in ln and "c" in ln][0]
    assert data_line.count("\t") == 1
    assert "\n" not in data_line


# ── csv ──────────────────────────────────────────────


def test_csv_rendering(tmp_path: Path) -> None:
    p = tmp_path / "data.csv"
    p.write_text("h1,h2,h3\nv1,v2,v3\n", encoding="utf-8")
    out = ReadAttachmentTool().call(path=str(p))
    assert "h1\th2\th3" in out
    assert "v1\tv2\tv3" in out


def test_csv_empty(tmp_path: Path) -> None:
    p = tmp_path / "empty.csv"
    p.write_text("", encoding="utf-8")
    out = ReadAttachmentTool().call(path=str(p))
    assert "[empty csv]" in out


# ── plain-text extensions ────────────────────────────


@pytest.mark.parametrize(
    "ext,payload",
    [
        (".txt", "hello world"),
        (".md", "# heading\n\nparagraph"),
        (".json", '{"a":1,"b":2}'),
        (".jsonld", '{"@context":"schema.org"}'),
        (".pdb", "ATOM      1  N   ALA A   1"),
        (".yaml", "a: 1\nb: 2"),
    ],
)
def test_plain_text_extensions(tmp_path: Path, ext: str, payload: str) -> None:
    p = tmp_path / f"file{ext}"
    p.write_text(payload, encoding="utf-8")
    out = ReadAttachmentTool().call(path=str(p))
    assert payload.splitlines()[0] in out


# ── Unknown extension / binary sniff ─────────────────


def test_unknown_extension_text_passes(tmp_path: Path) -> None:
    p = tmp_path / "mystery.xyz"
    p.write_text("plain ascii content", encoding="utf-8")
    out = ReadAttachmentTool().call(path=str(p))
    assert "plain ascii content" in out


def test_unknown_extension_binary_errors(tmp_path: Path) -> None:
    """NUL bytes in head → structured error, not garbage."""
    p = tmp_path / "mystery.xyz"
    p.write_bytes(b"\x00\x01\x02\xff" * 200)
    out = ReadAttachmentTool().call(path=str(p))
    assert out.startswith("error:")
    # Message must tell the model not to retry blindly.
    assert "binary" in out.lower() or "cannot" in out.lower()


# ── Failure modes ────────────────────────────────────


def test_empty_path() -> None:
    assert ReadAttachmentTool().call(path="").startswith("error:")


def test_missing_file(tmp_path: Path) -> None:
    out = ReadAttachmentTool().call(path=str(tmp_path / "nope.xlsx"))
    assert "not found" in out


def test_directory_refused(tmp_path: Path) -> None:
    out = ReadAttachmentTool().call(path=str(tmp_path))
    assert "not a regular file" in out


def test_unc_path_refused() -> None:
    assert "refusing" in ReadAttachmentTool().call(path="\\\\host\\share")


# ── Lazy-import failure contract ─────────────────────


def test_openpyxl_missing_returns_clean_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulate a runtime without openpyxl — tool must not raise."""
    import sys
    # Create a plausible .xlsx path. We do not need the bytes to be a
    # real workbook because the tool should fail at import before
    # touching the file.
    p = tmp_path / "dummy.xlsx"
    p.write_bytes(b"PK\x03\x04fake")

    # Hide openpyxl from the lazy import inside _read_xlsx.
    monkeypatch.setitem(sys.modules, "openpyxl", None)
    out = ReadAttachmentTool().call(path=str(p))
    assert out.startswith("error:")
    assert "openpyxl" in out


def test_readattachmenterror_is_exception() -> None:
    # Type contract — raised internally, string-wrapped by ``call``.
    assert issubclass(ReadAttachmentError, Exception)
