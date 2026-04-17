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

from .file_io import (
    BinaryFileError,
    FileEdit,
    FileRead,
    FileWrite,
    MultipleMatchesError,
    ReadBeforeWriteError,
)
from .search import EXCLUDED_DIRS, FileGlob, FileGrep
from .shell import (
    Shell,
    ShellDestructionError,
    ShellSecurityError,
    ShellTimeoutError,
)

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
]
