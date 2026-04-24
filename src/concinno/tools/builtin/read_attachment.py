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

# Inline cap. Any rendered attachment longer than this gets returned as
# a short summary (path + format + size + head excerpt + processing hint)
# rather than the full text. Stops 2.9 MB PDB / 512 KB log files from
# getting stuffed into the tool_response message history and blowing
# the LLM context window on the next agent_loop turn.
#
# Origin: 2026-04-23 pod v0ggvz5dcsu9gu GAIA task 7dd30055 (5wb7 PDB,
# 2,897,856 bytes) — InProcessLlamaCppBackend raised
# "Requested tokens (9479) exceed context window of 8192" on round 2
# because read_attachment had returned ~700K tokens of atom coords.
# The fix is general: any weak-model + small-ctx deploy needs the tool
# to stay under the context budget regardless of file size. Large
# attachments are processed via python_exec ``open(path)`` directly.
#
# Budget math: 15,000 chars ≈ 3,500 Gemma tokens ≈ 45% of 8K ctx after
# system prompt + question + prior turns; leaves headroom for the
# model's own response. Tuneable per deploy via env var or kwarg.
DEFAULT_MAX_INLINE_CHARS = 15000

# How much of the head of a too-large attachment to include in the
# summary. Short enough to stay under budget, long enough for the model
# to recognise the format (first few atoms of a PDB, first row of a
# gargantuan CSV, first lines of a log).
DEFAULT_HEAD_PREVIEW_CHARS = 800

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
        max_inline_chars: int | None = None,
    ) -> str:
        """Dispatch on extension and return a model-ready text rendering.

        Args:
            path: Absolute path to the attachment.
            max_bytes: Byte cap on raw file read (plain-text path).
            max_inline_chars: Character cap on the final rendered text
                before we swap to a summary-only reply. ``None`` pulls
                the default from the ``CONCINNO_READ_ATTACHMENT_MAX_INLINE_CHARS``
                env var or :data:`DEFAULT_MAX_INLINE_CHARS`. Set very
                large to disable the summary fallback entirely.

        Returns:
            The extracted text, or a ``[read_attachment: file too
            large ...]`` summary block when the rendering would exceed
            the inline cap. On failure returns ``"error: ..."`` so
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

        # Resolve inline cap once so downstream truncation + summary
        # use the same number.
        if max_inline_chars is None:
            max_inline_chars = _resolve_max_inline_chars()

        ext = Path(abs_path).suffix.lower()
        try:
            if ext in {".xlsx", ".xlsm"}:
                rendered = _read_xlsx(abs_path)
            elif ext == ".csv":
                rendered = _read_csv(abs_path)
            elif ext in _PLAIN_TEXT_EXTENSIONS:
                rendered = _read_plain_text(abs_path, max_bytes)
            else:
                # Unknown extension — try plain-text path; binary sniff
                # inside _read_plain_text returns a clear error.
                rendered = _read_plain_text(abs_path, max_bytes)
        except ReadAttachmentError as exc:
            return f"error: {exc}"
        except Exception as exc:  # noqa: BLE001 — observation tool
            return (
                f"error: read_attachment runtime: "
                f"{type(exc).__name__}: {exc}"
            )

        # Inline-cap guard. Large attachments get summarised so the
        # tool_response stays well under the LLM context budget.
        if max_inline_chars > 0 and len(rendered) > max_inline_chars:
            return _summarise_large_attachment(
                abs_path=abs_path,
                rendered=rendered,
                max_inline_chars=max_inline_chars,
            )
        return rendered


def _resolve_max_inline_chars() -> int:
    """Pick the inline cap from env or default.

    Env override: ``CONCINNO_READ_ATTACHMENT_MAX_INLINE_CHARS``.
    Non-integer / negative values fall back to the default so a
    malformed deploy env doesn't accidentally disable the guard.
    ``0`` explicitly disables the cap (full content always returned).
    """
    raw = os.environ.get("CONCINNO_READ_ATTACHMENT_MAX_INLINE_CHARS")
    if raw is None:
        return DEFAULT_MAX_INLINE_CHARS
    try:
        value = int(raw.strip())
    except (ValueError, AttributeError):
        return DEFAULT_MAX_INLINE_CHARS
    if value < 0:
        return DEFAULT_MAX_INLINE_CHARS
    return value


def _summarise_large_attachment(
    *,
    abs_path: str,
    rendered: str,
    max_inline_chars: int,
) -> str:
    """Return a short path + format + head + processing-hint summary.

    The hint actively steers the model toward ``python_exec`` with a
    plain ``open(path)`` so a Biopython / pandas / json / CSV parse
    happens outside the prompt. Without this the model tends to retry
    ``read_attachment`` or ``read_file`` on the same path and blow
    context again.

    Summary total size is bounded: head preview + fixed boilerplate
    stays under ~1,200 chars regardless of input size.
    """
    size_bytes = 0
    try:
        size_bytes = os.path.getsize(abs_path)
    except OSError:
        pass
    ext = Path(abs_path).suffix.lower() or "(none)"
    head = rendered[:DEFAULT_HEAD_PREVIEW_CHARS]
    # Stop the head at the last newline so we don't cut mid-token.
    if len(head) == DEFAULT_HEAD_PREVIEW_CHARS:
        last_nl = head.rfind("\n")
        if last_nl > DEFAULT_HEAD_PREVIEW_CHARS // 2:
            head = head[:last_nl]
    rendered_chars = len(rendered)
    return (
        "[read_attachment: file too large to inline — returned summary "
        f"only (rendered {rendered_chars} chars > cap {max_inline_chars}).]\n"
        f"path: {abs_path}\n"
        f"size_bytes: {size_bytes}\n"
        f"extension: {ext}\n"
        "how_to_process: Use python_exec with "
        "`with open(path) as f: ...` (or the relevant library — "
        "Biopython PDB.PDBParser, pandas.read_csv, json.load — for "
        "structured formats). Do NOT retry read_attachment or "
        "read_file on this path; the content has already been fully "
        "rendered on disk and would overflow the LLM context window.\n"
        "head_preview (first "
        f"{len(head)} chars):\n"
        f"{head}\n"
        "[end of read_attachment summary]"
    )
