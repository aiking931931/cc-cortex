"""concinno.tools.builtin — first-class builtin tool implementations.

Exports:
    FileRead, FileWrite, FileEdit — file I/O tools matching Claude Code's
        FileReadTool / FileWriteTool / FileEditTool behavior, including the
        ``readFileState`` write-gate invariant.
    Shell — Bash execution tool with bash_validators + destruction_guard +
        2-stage classifier (stage-2 stub) + auto-background after 15s.
    FileGlob, FileGrep — search tools matching CC's GlobTool/GrepTool with
        ripgrep backend + Python re fallback.
    Errors — ReadBeforeWriteError, BinaryFileError, MultipleMatchesError
        (file_io); ShellSecurityError, ShellDestructionError, ShellTimeoutError
        (shell).
    EXCLUDED_DIRS — shared exclude set for search tools.
"""

from __future__ import annotations

from .date_calc import DateCalcTool
from .file_io import (
    BinaryFileError,
    FileEdit,
    FileRead,
    FileWrite,
    MultipleMatchesError,
    ReadBeforeWriteError,
)
from .html import HtmlToolError, HtmlToText
from .pdf import PdfExtract, PdfRead, PdfToolError
from .python_exec import PythonExecError, PythonExecTool
from .read_attachment import ReadAttachmentError, ReadAttachmentTool
from .rss import RssFetch, RssToolError
from .search import EXCLUDED_DIRS, FileGlob, FileGrep
from .shell import (
    Shell,
    ShellDestructionError,
    ShellSecurityError,
    ShellTimeoutError,
)
from .sql import DuckDbQuery, SqlToolError
from .wiki import FetchWikipediaSectionTool

__all__ = [
    # file_io
    "BinaryFileError",
    "FileEdit",
    "FileRead",
    "FileWrite",
    "MultipleMatchesError",
    "ReadBeforeWriteError",
    # shell
    "Shell",
    "ShellDestructionError",
    "ShellSecurityError",
    "ShellTimeoutError",
    # search
    "EXCLUDED_DIRS",
    "FileGlob",
    "FileGrep",
    # date_calc
    "DateCalcTool",
    # python_exec
    "PythonExecError",
    "PythonExecTool",
    # read_attachment
    "ReadAttachmentError",
    "ReadAttachmentTool",
    # pdf
    "PdfExtract",
    "PdfRead",
    "PdfToolError",
    # html
    "HtmlToText",
    "HtmlToolError",
    # sql
    "DuckDbQuery",
    "SqlToolError",
    # rss
    "RssFetch",
    "RssToolError",
    # wiki
    "FetchWikipediaSectionTool",
]
