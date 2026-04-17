"""concinno.tools.builtin.file_io — FileRead / FileWrite / FileEdit tools.

@module file_io
@responsibility Provide Claude Code-equivalent Read/Write/Edit tools with the
    ``readFileState`` write-gate invariant. FileWrite and FileEdit refuse to
    touch a pre-existing file unless a matching FileRead has already recorded
    it in the shared state store — this is the single invariant that prevents
    silent file corruption by the model.
@dependencies concinno.core.state_store (hard), concinno.tool_executor (Tool
    Protocol), ``chardet`` (lazy, optional — falls back to heuristic).
@exports FileRead, FileWrite, FileEdit, ReadBeforeWriteError, BinaryFileError,
    MultipleMatchesError

Design notes
------------
The readFileState cache is stored in the shared :class:`StateStore` under the
namespace ``file_read_state`` using an absolute-path key. Unlike CC's in-memory
``ToolUseContext.readFileState`` map, this survives across sessions; callers
that want session-scoped behavior can wire a per-session base_dir into the
StateStore constructor.

Behaviors copied verbatim from CC source are cited inline with the file path
and line numbers they were read from. The spec lives at
``E:/Cursor/src/src(CC源碼)/tools/``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from concinno.core.state_store import StateStore

logger = logging.getLogger("concinno.tools.builtin.file_io")

# ---------------------------------------------------------------------------
# Constants — mirror CC defaults.
# ---------------------------------------------------------------------------

#: Namespace in :class:`StateStore` used for the readFileState cache.
READ_STATE_NAMESPACE = "file_read_state"

#: Flat filename inside the namespace (not session-scoped — readFileState is
#: process-wide in CC, so we store it in a single flat JSON file here).
READ_STATE_FILENAME = "read_state.json"

#: Max total bytes read by a single FileRead call. See
#: ``E:/Cursor/src/src(CC源碼)/tools/FileReadTool/limits.ts:6-7``.
DEFAULT_MAX_BYTES = 256 * 1024

#: Default per-read line cap (CC default).
DEFAULT_MAX_LINES = 2000

#: Hard cap on editable file size. See
#: ``E:/Cursor/src/src(CC源碼)/tools/FileEditTool/FileEditTool.ts:79-84``.
MAX_EDIT_FILE_SIZE = 1024 * 1024 * 1024  # 1 GiB

#: Narrow no-break space used by macOS screenshot filenames. See
#: ``E:/Cursor/src/src(CC源碼)/tools/FileReadTool/FileReadTool.ts:131``.
THIN_SPACE = "\u202f"

#: Device files that would hang or make no sense to read. See
#: ``E:/Cursor/src/src(CC源碼)/tools/FileReadTool/FileReadTool.ts:96-115``.
BLOCKED_DEVICE_PATHS = frozenset(
    {
        "/dev/zero",
        "/dev/random",
        "/dev/urandom",
        "/dev/full",
        "/dev/stdin",
        "/dev/tty",
        "/dev/console",
        "/dev/stdout",
        "/dev/stderr",
        "/dev/fd/0",
        "/dev/fd/1",
        "/dev/fd/2",
    },
)

#: File extensions always treated as binary.
_BINARY_EXTENSIONS = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".bmp",
        ".ico",
        ".tiff",
        ".pdf",
        ".zip",
        ".tar",
        ".gz",
        ".bz2",
        ".7z",
        ".rar",
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".o",
        ".a",
        ".class",
        ".pyc",
        ".pyo",
        ".wasm",
        ".mp3",
        ".mp4",
        ".wav",
        ".ogg",
        ".flac",
        ".mkv",
        ".mov",
        ".avi",
        ".db",
        ".sqlite",
        ".sqlite3",
    },
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ReadBeforeWriteError(Exception):
    """Raised when Write/Edit is attempted on a file with no valid prior Read.

    Mirrors CC's ``errorCode: 6`` from FileEditTool.ts:275-287. Callers who
    want to bypass this invariant should issue a FileRead first.
    """


class BinaryFileError(Exception):
    """Raised when FileRead is called on a detected binary file."""


class MultipleMatchesError(ValueError):
    """Raised when FileEdit's ``old_string`` matches multiple locations and
    ``replace_all`` is False. Mirrors CC's ``errorCode: 9`` from
    FileEditTool.ts:332-343.
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _abs_path(file_path: str) -> str:
    """Return an absolute, normalized path. Does not touch the filesystem."""
    return os.path.abspath(os.path.expanduser(file_path))


def _is_unc_path(file_path: str) -> bool:
    """Detect Windows UNC paths. CC skips filesystem ops on these to prevent
    NTLM credential leaks — see FileEditTool.ts:176-181.
    """
    return file_path.startswith("\\\\") or file_path.startswith("//")


def _is_blocked_device(file_path: str) -> bool:
    """Block /dev/zero, /proc/self/fd/{0,1,2}, etc. See
    FileReadTool.ts:117-128.
    """
    if file_path in BLOCKED_DEVICE_PATHS:
        return True
    if file_path.startswith("/proc/") and file_path.endswith(("/fd/0", "/fd/1", "/fd/2")):
        return True
    return False


def _content_hash(content: str) -> str:
    """Content fingerprint for readFileState freshness checks."""
    return hashlib.blake2b(content.encode("utf-8", errors="replace"), digest_size=16).hexdigest()


def _looks_binary(data: bytes) -> bool:
    """Heuristic: presence of a NUL byte in the first 8 KiB ⇒ binary.

    Matches the behavior CC uses via ``hasBinaryExtension`` + NUL scan in
    readFileInRange. We use the NUL heuristic directly since we don't want
    to inline CC's full extension table.

    Exempts UTF-16 text (LE/BE BOM) — ASCII characters encoded in UTF-16
    contain NUL bytes by design, and CC handles them as text (see
    FileEditTool.ts:207-213).
    """
    if not data:
        return False
    # UTF-16 BOM: treat as text even though NULs appear.
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return False
    sample = data[:8192]
    if b"\x00" in sample:
        return True
    # Low ratio of "text-candidate" bytes (printable ASCII + common controls
    # + high bytes used by UTF-8 multi-byte / latin-1) suggests binary. We
    # deliberately include 0x80-0xFF so latin-1 / UTF-8 accents stay text.
    text_ok = sum(
        1
        for b in sample
        if (32 <= b < 127) or b in (9, 10, 13) or b >= 0x80
    )
    return (text_ok / len(sample)) < 0.85


def _has_binary_ext(file_path: str) -> bool:
    """Check if the extension is on the known-binary list."""
    return Path(file_path).suffix.lower() in _BINARY_EXTENSIONS


def _detect_encoding(data: bytes) -> tuple[str, bytes]:
    """Return ``(encoding, bom_bytes)``. Handles UTF-8, UTF-16 LE/BE BOMs
    then falls through to chardet (lazy) then utf-8 replacement.

    See FileEditTool.ts:207-213 for CC's inline utf-16 BOM check.
    """
    if len(data) >= 3 and data[:3] == b"\xef\xbb\xbf":
        return "utf-8-sig", b"\xef\xbb\xbf"
    if len(data) >= 2 and data[:2] == b"\xff\xfe":
        return "utf-16-le", b"\xff\xfe"
    if len(data) >= 2 and data[:2] == b"\xfe\xff":
        return "utf-16-be", b"\xfe\xff"
    # Try chardet lazily.
    try:
        import chardet  # type: ignore[import-untyped]

        guess = chardet.detect(data[:65536]) or {}
        enc = guess.get("encoding")
        conf = guess.get("confidence") or 0.0
        if enc and conf >= 0.5:
            # Normalize common names.
            norm = enc.lower().replace("_", "-")
            return norm, b""
    except ImportError:
        pass
    return "utf-8", b""


def _resolve_screenshot_path(file_path: str) -> str:
    """Swap regular/thin space before AM|PM on macOS screenshot filenames.

    See FileReadTool.ts:142-159. If neither exists, return original.
    """
    if os.path.exists(file_path):
        return file_path
    basename = os.path.basename(file_path)
    # Pattern: <prefix><space><AM|PM>.png
    for marker in (" AM.png", " PM.png", f"{THIN_SPACE}AM.png", f"{THIN_SPACE}PM.png"):
        if basename.endswith(marker):
            if marker.startswith(" "):
                alt_marker = THIN_SPACE + marker.lstrip(" ")
            else:
                alt_marker = " " + marker.lstrip(THIN_SPACE)
            alt = file_path[: -len(marker)] + alt_marker
            if os.path.exists(alt):
                return alt
    return file_path


# ---------------------------------------------------------------------------
# Shared readFileState access layer
# ---------------------------------------------------------------------------


class _ReadStateCache:
    """Thin wrapper around :class:`StateStore` providing get/put for the
    readFileState map. Kept as an instance rather than module globals so
    tests can inject their own base_dir.
    """

    def __init__(self, store: StateStore) -> None:
        self._store = store

    def get(self, abs_path: str) -> dict[str, Any] | None:
        data = self._store.read_flat(
            READ_STATE_NAMESPACE,
            READ_STATE_FILENAME,
            default={},
        )
        if not isinstance(data, dict):
            return None
        entry = data.get(abs_path)
        if not isinstance(entry, dict):
            return None
        return entry

    def put(self, abs_path: str, entry: dict[str, Any]) -> None:
        data = self._store.read_flat(
            READ_STATE_NAMESPACE,
            READ_STATE_FILENAME,
            default={},
        )
        if not isinstance(data, dict):
            data = {}
        data[abs_path] = entry
        self._store.write_flat(READ_STATE_NAMESPACE, READ_STATE_FILENAME, data)

    def delete(self, abs_path: str) -> None:
        data = self._store.read_flat(
            READ_STATE_NAMESPACE,
            READ_STATE_FILENAME,
            default={},
        )
        if isinstance(data, dict) and abs_path in data:
            data.pop(abs_path, None)
            self._store.write_flat(READ_STATE_NAMESPACE, READ_STATE_FILENAME, data)


def _default_cache_dir() -> str:
    """Resolve the default StateStore base_dir. Mirrors the convention used
    by :class:`concinno.tool_executor.ToolExecutor`.
    """
    return os.environ.get("CONCINNO_CACHE_DIR", ".concinno_cache")


# ---------------------------------------------------------------------------
# FileRead
# ---------------------------------------------------------------------------


class FileRead:
    """Read a text file, with the same guardrails CC's FileReadTool has.

    Behavior copied from ``E:/Cursor/src/src(CC源碼)/tools/FileReadTool/
    FileReadTool.ts`` — specifically the blocked-device set, thin-space
    macOS screenshot resolver, UTF-16 BOM handling, and the line/byte caps
    from ``limits.ts``.
    """

    name = "Read"
    description = "Read contents of a text file by absolute path."
    is_concurrency_safe = True

    def __init__(
        self,
        *,
        cache_dir: str | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_lines: int = DEFAULT_MAX_LINES,
        allow_binary: bool = False,
    ) -> None:
        base_dir = cache_dir or _default_cache_dir()
        self._store = StateStore(base_dir)
        self._state = _ReadStateCache(self._store)
        self._max_bytes = int(max_bytes)
        self._max_lines = int(max_lines)
        self._allow_binary = bool(allow_binary)

    def call(self, **kwargs: Any) -> dict[str, Any]:
        file_path = kwargs.get("file_path")
        if not isinstance(file_path, str) or not file_path:
            msg = "FileRead: file_path is required and must be a string"
            raise ValueError(msg)

        offset = int(kwargs.get("offset") or 0)
        limit_arg = kwargs.get("limit")
        limit = int(limit_arg) if limit_arg is not None else None

        if _is_blocked_device(file_path):
            msg = f"FileRead: refusing to read blocked device path: {file_path}"
            raise PermissionError(msg)

        if _is_unc_path(file_path):
            msg = f"FileRead: UNC paths are not supported: {file_path}"
            raise PermissionError(msg)

        abs_path = _abs_path(file_path)
        resolved = _resolve_screenshot_path(abs_path)

        if not os.path.exists(resolved):
            msg = f"FileRead: file not found: {abs_path}"
            raise FileNotFoundError(msg)

        if not os.path.isfile(resolved):
            msg = f"FileRead: not a regular file: {abs_path}"
            raise IsADirectoryError(msg)

        size = os.path.getsize(resolved)

        # Read up to max_bytes + 1 so we can flag truncation.
        with open(resolved, "rb") as fh:
            raw = fh.read(self._max_bytes + 1)

        truncated_bytes = len(raw) > self._max_bytes
        if truncated_bytes:
            raw = raw[: self._max_bytes]

        # Binary detection.
        if _has_binary_ext(resolved) or _looks_binary(raw):
            if not self._allow_binary:
                msg = (
                    f"FileRead: binary file detected: {abs_path}. "
                    "Pass allow_binary=True to the constructor to read bytes."
                )
                raise BinaryFileError(msg)

        encoding, _bom = _detect_encoding(raw)
        try:
            text = raw.decode(encoding, errors="replace")
        except (LookupError, ValueError):
            text = raw.decode("utf-8", errors="replace")

        # Strip BOM char so the hash computed here matches the one FileEdit
        # computes (it also strips); otherwise round-tripping a UTF-16 file
        # through Read → Edit fails the content_hash invariant check.
        if text.startswith("\ufeff"):
            text = text[1:]
        # Normalize line endings (CC does this: FileEditTool.ts:214).
        text = text.replace("\r\n", "\n")

        lines = text.split("\n")
        total_lines = len(lines)

        if offset < 0:
            offset = 0
        if offset > total_lines:
            offset = total_lines

        effective_limit = limit if limit is not None else self._max_lines
        effective_limit = max(0, effective_limit)

        end = min(total_lines, offset + effective_limit)
        sliced = lines[offset:end]
        content = "\n".join(sliced)

        truncated_lines = end < total_lines
        truncated = truncated_bytes or truncated_lines

        # Update readFileState. Key is absolute path (pre-resolve) so future
        # Write/Edit lookups hit the same key users passed in.
        entry = {
            "timestamp": time.time(),
            "content_hash": _content_hash(text),
            "offset": offset,
            "limit": effective_limit,
            "encoding": encoding,
            "is_partial": truncated or offset > 0 or (limit is not None and limit < total_lines),
            "size_bytes": size,
        }
        self._state.put(abs_path, entry)

        return {
            "content": content,
            "lines_read": len(sliced),
            "bytes_read": len(raw),
            "encoding": encoding,
            "truncated": truncated,
            "total_lines": total_lines,
            "path": abs_path,
        }


# ---------------------------------------------------------------------------
# FileWrite
# ---------------------------------------------------------------------------


def _atomic_write_bytes(path: str, data: bytes) -> None:
    """Write bytes to ``path`` via tempfile + os.replace. Preserves parent dir.

    On Windows, ``os.replace`` is atomic for same-volume renames.
    """
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".concinno_write_",
        dir=parent,
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class FileWrite:
    """Create or overwrite a file. Refuses to overwrite a pre-existing file
    unless a matching FileRead has recorded it in readFileState.

    The read-before-write invariant is the single load-bearing rule here —
    without it, the model can silently corrupt a file it never looked at.
    Mirrors the check in FileEditTool.ts:275-287.
    """

    name = "Write"
    description = "Write content to a file by absolute path."
    is_concurrency_safe = False

    def __init__(self, *, cache_dir: str | None = None) -> None:
        base_dir = cache_dir or _default_cache_dir()
        self._store = StateStore(base_dir)
        self._state = _ReadStateCache(self._store)

    def call(self, **kwargs: Any) -> dict[str, Any]:
        file_path = kwargs.get("file_path")
        content = kwargs.get("content")
        if not isinstance(file_path, str) or not file_path:
            msg = "FileWrite: file_path is required"
            raise ValueError(msg)
        if not isinstance(content, str):
            msg = "FileWrite: content must be a string"
            raise TypeError(msg)

        if _is_unc_path(file_path):
            msg = f"FileWrite: UNC paths are not supported: {file_path}"
            raise PermissionError(msg)

        abs_path = _abs_path(file_path)
        exists_before = os.path.exists(abs_path)

        if exists_before:
            if not os.path.isfile(abs_path):
                msg = f"FileWrite: not a regular file: {abs_path}"
                raise IsADirectoryError(msg)
            # readFileState invariant: must have a fresh prior read.
            entry = self._state.get(abs_path)
            if entry is None:
                msg = (
                    f"FileWrite: file already exists but has not been read: "
                    f"{abs_path}. Read it first via FileRead."
                )
                raise ReadBeforeWriteError(msg)
            recorded_ts = float(entry.get("timestamp", 0.0))
            try:
                mtime = os.path.getmtime(abs_path)
            except OSError as exc:
                msg = f"FileWrite: stat failed for {abs_path}: {exc}"
                raise OSError(msg) from exc
            # Permit a tiny clock skew slack (10 ms) since some filesystems
            # truncate timestamps.
            if mtime > recorded_ts + 0.010:
                msg = (
                    f"FileWrite: {abs_path} has been modified since last read "
                    f"(mtime={mtime} > recorded={recorded_ts}). Read again."
                )
                raise ReadBeforeWriteError(msg)

        # Determine encoding & BOM to preserve. See FileEditTool.ts:207-213.
        encoding = "utf-8"
        bom = b""
        if exists_before:
            try:
                with open(abs_path, "rb") as fh:
                    head = fh.read(4096)
                encoding, bom = _detect_encoding(head)
            except OSError:
                encoding, bom = "utf-8", b""

        payload = _encode_with_bom(content, encoding, bom)

        _atomic_write_bytes(abs_path, payload)

        # Refresh readFileState so subsequent Reads/Edits see the new state.
        self._state.put(
            abs_path,
            {
                "timestamp": time.time(),
                "content_hash": _content_hash(content),
                "offset": 0,
                "limit": None,
                "encoding": encoding,
                "is_partial": False,
                "size_bytes": len(payload),
            },
        )

        return {
            "path": abs_path,
            "bytes_written": len(payload),
            "created": not exists_before,
        }


# ---------------------------------------------------------------------------
# FileEdit
# ---------------------------------------------------------------------------


def _encode_with_bom(text: str, encoding: str, bom: bytes) -> bytes:
    """Encode ``text`` preserving the BOM recovered from the source file.

    Split out so FileWrite and FileEdit can share the logic.
    """
    if encoding == "utf-8-sig":
        return b"\xef\xbb\xbf" + text.encode("utf-8")
    if encoding == "utf-16-le":
        return bom + text.encode("utf-16-le")
    if encoding == "utf-16-be":
        return bom + text.encode("utf-16-be")
    try:
        return text.encode(encoding, errors="replace")
    except (LookupError, ValueError):
        return text.encode("utf-8", errors="replace")


class FileEdit:
    """Replace ``old_string`` with ``new_string`` in a file.

    Enforces the readFileState invariant, the 1 GiB file cap, the
    unique-match rule (unless ``replace_all=True``), and the UNC skip.
    Mirrors FileEditTool.ts:86-450.
    """

    name = "Edit"
    description = "Replace a string in a file. Enforces read-before-write."
    is_concurrency_safe = False

    def __init__(self, *, cache_dir: str | None = None) -> None:
        base_dir = cache_dir or _default_cache_dir()
        self._store = StateStore(base_dir)
        self._state = _ReadStateCache(self._store)

    # ------------------------------------------------------------------
    # Helpers — keep ``call`` under the structural length budget.
    # ------------------------------------------------------------------

    def _validate_inputs(
        self,
        file_path: Any,
        old_string: Any,
        new_string: Any,
    ) -> None:
        if not isinstance(file_path, str) or not file_path:
            msg = "FileEdit: file_path is required"
            raise ValueError(msg)
        if not isinstance(old_string, str) or not isinstance(new_string, str):
            msg = "FileEdit: old_string and new_string must be strings"
            raise TypeError(msg)
        if old_string == new_string:
            msg = "FileEdit: old_string and new_string are identical"
            raise ValueError(msg)
        if _is_unc_path(file_path):
            # Matches FileEditTool.ts:176-181.
            msg = f"FileEdit: UNC paths are not supported: {file_path}"
            raise PermissionError(msg)

    def _stat_and_check_invariant(self, abs_path: str) -> int:
        if not os.path.exists(abs_path):
            msg = f"FileEdit: file not found: {abs_path}"
            raise FileNotFoundError(msg)
        if not os.path.isfile(abs_path):
            msg = f"FileEdit: not a regular file: {abs_path}"
            raise IsADirectoryError(msg)
        size = os.path.getsize(abs_path)
        if size > MAX_EDIT_FILE_SIZE:
            msg = (
                f"FileEdit: file too large to edit ({size} bytes > "
                f"{MAX_EDIT_FILE_SIZE})"
            )
            raise ValueError(msg)
        entry = self._state.get(abs_path)
        if entry is None:
            msg = (
                f"FileEdit: file has not been read: {abs_path}. "
                "Read it first before editing."
            )
            raise ReadBeforeWriteError(msg)
        recorded_ts = float(entry.get("timestamp", 0.0))
        try:
            mtime = os.path.getmtime(abs_path)
        except OSError as exc:
            msg = f"FileEdit: stat failed for {abs_path}: {exc}"
            raise OSError(msg) from exc
        if mtime > recorded_ts + 0.010:
            msg = (
                f"FileEdit: {abs_path} modified since last read "
                f"(mtime={mtime} > recorded={recorded_ts}). Read again."
            )
            raise ReadBeforeWriteError(msg)
        return size

    def _load_and_verify(self, abs_path: str) -> tuple[str, str, bytes]:
        """Read + decode + verify content hash against readFileState."""
        with open(abs_path, "rb") as fh:
            raw = fh.read()
        encoding, bom = _detect_encoding(raw)
        try:
            text = raw.decode(encoding, errors="replace")
        except (LookupError, ValueError):
            text, encoding = raw.decode("utf-8", errors="replace"), "utf-8"
        # Strip BOM character if it survived decoding (utf-16-le/be keep it).
        if text.startswith("\ufeff"):
            text = text[1:]
        text = text.replace("\r\n", "\n")
        entry = self._state.get(abs_path) or {}
        recorded_hash = entry.get("content_hash")
        if recorded_hash and not entry.get("is_partial"):
            if _content_hash(text) != recorded_hash:
                msg = (
                    f"FileEdit: content hash mismatch for {abs_path}. "
                    "File changed since last read."
                )
                raise ReadBeforeWriteError(msg)
        return text, encoding, bom

    def _apply_replacement(
        self,
        abs_path: str,
        text: str,
        old_string: str,
        new_string: str,
        replace_all: bool,
    ) -> tuple[str, int]:
        if old_string not in text:
            msg = f"FileEdit: old_string not found in {abs_path}"
            raise ValueError(msg)
        occurrences = text.count(old_string)
        if occurrences > 1 and not replace_all:
            msg = (
                f"FileEdit: found {occurrences} matches of old_string in "
                f"{abs_path} but replace_all is False. Provide more context "
                f"to make the match unique, or pass replace_all=True."
            )
            raise MultipleMatchesError(msg)
        if replace_all:
            return text.replace(old_string, new_string), occurrences
        return text.replace(old_string, new_string, 1), 1

    # ------------------------------------------------------------------
    # Public entry point.
    # ------------------------------------------------------------------

    def call(self, **kwargs: Any) -> dict[str, Any]:
        file_path = kwargs.get("file_path")
        old_string = kwargs.get("old_string")
        new_string = kwargs.get("new_string")
        replace_all = bool(kwargs.get("replace_all", False))

        self._validate_inputs(file_path, old_string, new_string)

        # After validation we know the types.
        assert isinstance(file_path, str)
        assert isinstance(old_string, str)
        assert isinstance(new_string, str)

        abs_path = _abs_path(file_path)
        size = self._stat_and_check_invariant(abs_path)

        # Atomic read-modify-write. NO yields between the read below and
        # the write (FileEditTool.ts:442-443 comment).
        text, encoding, bom = self._load_and_verify(abs_path)
        new_text, replacements = self._apply_replacement(
            abs_path, text, old_string, new_string, replace_all,
        )

        payload = _encode_with_bom(new_text, encoding, bom)
        _atomic_write_bytes(abs_path, payload)

        self._state.put(
            abs_path,
            {
                "timestamp": time.time(),
                "content_hash": _content_hash(new_text),
                "offset": 0,
                "limit": None,
                "encoding": encoding,
                "is_partial": False,
                "size_bytes": len(payload),
            },
        )

        return {
            "path": abs_path,
            "replacements": replacements,
            "bytes_delta": len(payload) - size,
        }


# Silence unused imports on platforms where Path/sys are only used for types.
_ = (Path, sys)
