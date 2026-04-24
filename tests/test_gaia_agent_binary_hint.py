"""Tests for concinno.skills.public.agent.gaia_agent binary attachment hint."""

from __future__ import annotations

import sys

import pytest


@pytest.fixture
def gaia_agent_mod(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "test-dummy")
    # gaia_agent caches the HF_TOKEN at module import time; drop it so the
    # monkeypatched value is picked up if a previous test already loaded it.
    sys.modules.pop("concinno.skills.public.agent.gaia_agent", None)
    from concinno.skills.public.agent import gaia_agent
    return gaia_agent


def test_build_binary_hint_xlsx_known_lib(tmp_path, gaia_agent_mod):
    f = tmp_path / "data.xlsx"
    f.write_bytes(b"PK\x03\x04" + b"\x00" * 100)
    hint = gaia_agent_mod.build_binary_attachment_hint(str(f))
    assert str(f) in hint
    assert "openpyxl" in hint
    assert "104 bytes" in hint
    assert "ext=.xlsx" in hint
    assert "code_exec" in hint


def test_build_binary_hint_csv(tmp_path, gaia_agent_mod):
    f = tmp_path / "vendors.csv"
    f.write_text("a,b,c\n1,2,3\n")
    hint = gaia_agent_mod.build_binary_attachment_hint(str(f))
    assert "pandas.read_csv" in hint


def test_build_binary_hint_pdf(tmp_path, gaia_agent_mod):
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4\n" + b"\x00" * 20)
    hint = gaia_agent_mod.build_binary_attachment_hint(str(f))
    assert "pypdf" in hint or "pdfplumber" in hint


def test_build_binary_hint_unknown_extension(tmp_path, gaia_agent_mod):
    f = tmp_path / "mystery.blob"
    f.write_bytes(b"\x00\x01\x02")
    hint = gaia_agent_mod.build_binary_attachment_hint(str(f))
    assert "open(" in hint
    assert ".blob" in hint


def test_build_binary_hint_missing_file(gaia_agent_mod):
    hint = gaia_agent_mod.build_binary_attachment_hint(
        "/no/such/path/file.xlsx"
    )
    assert "path not found" in hint


def test_build_binary_hint_empty_path(gaia_agent_mod):
    hint = gaia_agent_mod.build_binary_attachment_hint("")
    assert "path missing" in hint


def test_binary_lib_hints_table_covers_core_formats(gaia_agent_mod):
    table = gaia_agent_mod._BINARY_LIB_HINTS
    for ext in ("xlsx", "csv", "pdf", "docx", "pptx", "json",
                "xml", "zip", "sqlite", "parquet"):
        assert ext in table, f"missing core extension: {ext}"


def test_build_binary_hint_preserves_spaces_in_path(tmp_path, gaia_agent_mod):
    spaced = tmp_path / "with space.csv"
    spaced.write_text("x\n")
    hint = gaia_agent_mod.build_binary_attachment_hint(str(spaced))
    assert str(spaced) in hint
    assert "pandas.read_csv" in hint


def test_extract_csv_returns_text(tmp_path, gaia_agent_mod):
    f = tmp_path / "rows.csv"
    f.write_text("a,b,c\n1,2,3\n4,5,6\n")
    out = gaia_agent_mod.extract_tabular_attachment_text(str(f))
    assert out is not None
    assert "a,b,c" in out
    assert "4,5,6" in out


def test_extract_tsv_returns_text(tmp_path, gaia_agent_mod):
    f = tmp_path / "rows.tsv"
    f.write_text("a\tb\n1\t2\n")
    out = gaia_agent_mod.extract_tabular_attachment_text(str(f))
    assert out is not None
    assert "a\tb" in out


def test_extract_unsupported_returns_none(tmp_path, gaia_agent_mod):
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.4\n\x00\x00\x00")
    assert gaia_agent_mod.extract_tabular_attachment_text(str(f)) is None


def test_extract_missing_file_returns_none(gaia_agent_mod):
    assert gaia_agent_mod.extract_tabular_attachment_text(
        "/no/such/path.xlsx"
    ) is None


def test_extract_empty_path_returns_none(gaia_agent_mod):
    assert gaia_agent_mod.extract_tabular_attachment_text("") is None


def test_extract_truncates_oversize(tmp_path, gaia_agent_mod):
    f = tmp_path / "big.csv"
    f.write_text("a,b\n" + ("x" * 20000))
    out = gaia_agent_mod.extract_tabular_attachment_text(
        str(f), max_chars=1000
    )
    assert out is not None
    assert "[...truncated]" in out
    assert len(out) <= 1050  # 1000 + marker


def test_extract_xlsx_returns_rows(tmp_path, gaia_agent_mod):
    openpyxl = pytest.importorskip("openpyxl")
    f = tmp_path / "data.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Name", "Revenue", "Rent"])
    ws.append(["Foo", 100, 50])
    ws.append(["Bar", 200, 1000])
    wb.save(str(f))
    out = gaia_agent_mod.extract_tabular_attachment_text(str(f))
    assert out is not None
    assert "Name" in out and "Revenue" in out
    assert "Foo" in out
    assert "Bar" in out
