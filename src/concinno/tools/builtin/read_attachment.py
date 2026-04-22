"""concinno.tools.builtin.read_attachment — format-aware file reader.

@module read_attachment
@responsibility Read GAIA-style attachments with a format dispatch so weak
    models can consume xlsx/pdf/csv without choking on binary UTF-8 garbage.
    The existing ``FileRead`` tool refuses binary files; this tool complements
    it by normalizing common attachment formats into plain text the model
    can reason over.

@dependencies
    stdlib only for text/csv/json dispatch.
    openpyxl (lazy, optional) for xlsx/xlsm — install via the ``[agent]``
    extras or have it present in the runtime environment.

@exports ReadAttachmentTool, ReadAttachmentError

Behaviour contract — deliberately conservative:
    * ``.xlsx`` / ``.xlsm`` → openpyxl workbook traversal emitting one
      ``[Sheet: name]`` header per non-empty sheet followed by TSV rows.
    * ``.csv`` → stdlib csv.reader emitting TSV rows (comma → tab) so the
      output shape matches xlsx for downstream matching.
    * ``.json`` / ``.jsonl`` / ``.jsonld`` / ``.txt`` / ``.md`` / ``.tsv`` /
      ``.xml`` / ``.html`` / ``.htm`` / ``.yaml`` / ``.yml`` / ``.py`` /
      ``.sh`` / ``.pdb`` (chemistry!) / ``.log`` → UTF-8 decode with
      ``errors='replace'`` and an optional line cap.
    * Unknown suffix → UTF-8 text attempt; if the head looks binary the tool
      returns a structured error rather than garbage. This keeps the tool's
      failure mode explicit so the model can fall back to a different tool
      instead of parroting UTF-16 noise.

The tool does *not* attempt to render images, parse docx / pptx, pdf, or
handle sqlite — those formats need bespoke tooling and each branch must be
added *additively with its API verified on the target env* (lesson from the
04-22f anchor-stacking regression: speculative code paths break things).
"""

from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Any

from .file_io import _is_blocked_device, _is_unc_path, _looks_binary

# Cap read size. Attachments in GAIA are small (kb-range) so a generous but
# finite cap stops a pathological attachment from blowing up the prompt.
DEFAULT_MAX_BYTES = 512 * 1024

# Cap sheet / row count so one monstrous workbook does not dominate.
DEFAULT_MAX_SHEETS = 16
DEFAULT_MAX_ROWS_PER_SHEET = 400

# Per-cell truncation — individual notes fields can exceed 10 KB.
DEFAULT_MAX_CELL_CHARS = 500

#: Extensions handled via the plain-text path (decoded with utf-8 replace).
_PLAIN_TEXT_EXTENSIONS = frozenset(
    {
        ".txt",
        ".md",
        ".log",
        ".csv",  # handled before this set; listed for completeness
        ".tsv",
        ".json",
        ".jsonl",
        ".jsonld",
        ".xml",
        ".html",
        ".htm",
        ".yaml",
        ".yml",
        ".py",
        ".sh",
        ".bash",
        ".pdb",  # text PDB (chemistry) — not binary despite its niche
        ".cif",
        ".mmcif",
        ".rst",
        ".ini",
        ".toml",
        ".cfg",
    },
)


class ReadAttachmentError(Exception):
    """Raised when the attachment cannot be decoded into plain text."""


def _stringify_cell(value: Any, max_chars: int = DEFAULT_MAX_CELL_CHARS) -> str:
    """Convert an openpyxl cell value to a short printable string."""
    if value is None:
        return ""
    text = str(value)
    if len(text) > max_chars:
        return text[: max_chars - 3] + "..."
    # Tab/newline inside a cell would break the TSV framing.
    return text.replace("\t", " ").replace("\r", " ").replace("\n", " ")


def _read_xlsx(path: str) -> str:
    """Render an xlsx workbook as headered TSV sheets."""
    try:
        import openpyxl  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover — env without openpyxl
        msg = (
            "read_attachment: openpyxl is not installed, cannot read "
            f".xlsx files. Install 'concinno[agent]' or 'openpyxl'. "
            f"(detail: {exc})"
        )
        raise ReadAttachmentError(msg) from exc

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        blocks: list[str] = []
        for sheet_idx, sheet in enumerate(wb.worksheets):
            if sheet_idx >= DEFAULT_MAX_SHEETS:
                blocks.append(
                    f"[Sheet cap {DEFAULT_MAX_SHEETS} reached — "
                    f"{len(wb.worksheets) - DEFAULT_MAX_SHEETS} sheets omitted]"
                )
                break
            blocks.append(f"[Sheet: {sheet.title}]")
            row_count = 0
            for row in sheet.iter_rows(values_only=True):
                if row_count >= DEFAULT_MAX_ROWS_PER_SHEET:
                    blocks.append(
                        f"[Row cap {DEFAULT_MAX_ROWS_PER_SHEET} "
                        "reached — remaining rows omitted]"
                    )
                    break
                cells = [_stringify_cell(c) for c in row]
                # Drop trailing empty cells to keep rows compact.
                while cells and cells[-1] == "":
                    cells.pop()
                if not cells:
                    continue  # skip fully-empty row
                blocks.append("\t".join(cells))
                row_count += 1
            if row_count == 0:
                blocks.append("[empty sheet]")
        return "\n".join(blocks)
    finally:
        wb.close()


def _read_csv(path: str) -> str:
    """Render CSV as TSV to mirror xlsx output shape."""
    with open(path, encoding="utf-8", errors="replace", newline="") as fh:
        reader = csv.reader(fh)
        rows: list[str] = []
        for i, row in enumerate(reader):
            if i >= DEFAULT_MAX_ROWS_PER_SHEET:
                rows.append(
                    f"[Row cap {DEFAULT_MAX_ROWS_PER_SHEET} reached]"
                )
                break
            rows.append(
                "\t".join(
                    _stringify_cell(c) for c in row
                )
            )
        return "\n".join(rows) if rows else "[empty csv]"


def _read_plain_text(path: str, max_bytes: int) -> str:
    """UTF-8 read with binary sniff at head to avoid garbage."""
    with open(path, "rb") as fh:
        head = fh.read(8192)
    if _looks_binary(head):
        msg = (
            f"read_attachment: {path} looks like a binary format this "
            "tool cannot render. Supported: xlsx, xls, xlsm, csv, pdf, "
            "plus plain text extensions. Use the native format-specific "
            "tool or ask the user for a textual export."
        )
        raise ReadAttachmentError(msg)
    with open(path, "rb") as fh:
        raw = fh.read(max_bytes + 1)
    truncated = len(raw) > max_bytes
    if truncated:
        raw = raw[:max_bytes]
    text = raw.decode("utf-8", errors="replace")
    if text.startswith("﻿"):
        text = text[1:]
    text = text.replace("\r\n", "\n")
    if truncated:
        text += (
            f"\n[read_attachment: truncated at {max_bytes} bytes — "
            "file is larger]"
        )
    return text


class ReadAttachmentTool:
    """LLM-facing tool that reads an attachment with format dispatch.

    Attributes:
        name: ``"read_attachment"`` — LLM tool name.
        description: One-shot hint telling the model which formats this
            tool handles so it picks the right tool the first time.
        is_concurrency_safe: ``True`` — pure read, no mutation.
    """

    name: str = "read_attachment"
    description: str = (
        "Read a GAIA-style attachment and return plain text. Handles "
        "xlsx/xls/xlsm spreadsheets (as TSV rows per sheet), csv (TSV), "
        "pdf (per-page text), and plain-text formats "
        "(txt/md/json/jsonl/jsonld/xml/html/yaml/py/pdb/tsv/log). "
        "Use this tool when the user message references an attached "
        "file. Returns 'error: ...' on unsupported binary formats; do "
        "not retry the same path with read_file — ask for a textual "
        "export instead."
    )
    is_concurrency_safe: bool = True

    def call(
        self,
        *,
        path: str,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> str:
        """Dispatch on extension and return a model-ready text rendering.

        Returns:
            The extracted text. On failure returns ``"error: ..."`` so
            the agent loop treats it as an observation and can retry
            with a different tool rather than raising.
        """
        if not isinstance(path, str) or not path:
            return "error: path must be a non-empty string"

        if _is_blocked_device(path) or _is_unc_path(path):
            return f"error: refusing path {path}"

        abs_path = os.path.abspath(os.path.expanduser(path))
        if not os.path.exists(abs_path):
            return f"error: file not found: {abs_path}"
        if not os.path.isfile(abs_path):
            return f"error: not a regular file: {abs_path}"

        ext = Path(abs_path).suffix.lower()
        try:
            if ext in {".xlsx", ".xlsm"}:
                return _read_xlsx(abs_path)
            if ext == ".csv":
                return _read_csv(abs_path)
            if ext in _PLAIN_TEXT_EXTENSIONS:
                return _read_plain_text(abs_path, max_bytes)
            # Unknown extension — try plain-text path; binary sniff
            # inside _read_plain_text returns a clear error.
            return _read_plain_text(abs_path, max_bytes)
        except ReadAttachmentError as exc:
            return f"error: {exc}"
        except Exception as exc:  # noqa: BLE001 — observation tool
            return (
                f"error: read_attachment runtime: "
                f"{type(exc).__name__}: {exc}"
            )
