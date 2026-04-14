"""Tests for ``cc_cortex.tools.builtin.file_io`` — FileRead/Write/Edit.

The readFileState write-gate invariant is the central contract here: Write
and Edit must refuse to touch a pre-existing file unless FileRead has
recorded it. Each behavior copied from CC source is tagged in the test
docstring with the corresponding line number.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from cc_cortex.tools.builtin.file_io import (
    DEFAULT_MAX_BYTES,
    BinaryFileError,
    FileEdit,
    FileRead,
    FileWrite,
    MultipleMatchesError,
    ReadBeforeWriteError,
    _content_hash,
    _detect_encoding,
    _has_binary_ext,
    _is_unc_path,
    _looks_binary,
    _resolve_screenshot_path,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def cache_dir(tmp_path: Path) -> str:
    d = tmp_path / ".cc_cortex_cache"
    d.mkdir()
    return str(d)


@pytest.fixture()
def reader(cache_dir: str) -> FileRead:
    return FileRead(cache_dir=cache_dir)


@pytest.fixture()
def writer(cache_dir: str) -> FileWrite:
    return FileWrite(cache_dir=cache_dir)


@pytest.fixture()
def editor(cache_dir: str) -> FileEdit:
    return FileEdit(cache_dir=cache_dir)


# ---------------------------------------------------------------------------
# Unit tests — helpers
# ---------------------------------------------------------------------------


def test_content_hash_stable() -> None:
    assert _content_hash("hello") == _content_hash("hello")
    assert _content_hash("hello") != _content_hash("hello\n")


def test_looks_binary_detects_nul() -> None:
    assert _looks_binary(b"\x00abc")
    assert not _looks_binary(b"hello world\n")
    assert not _looks_binary(b"")


def test_has_binary_ext() -> None:
    assert _has_binary_ext("/tmp/foo.png")
    assert _has_binary_ext("/tmp/foo.PDF")
    assert not _has_binary_ext("/tmp/foo.py")


def test_is_unc_path() -> None:
    assert _is_unc_path("\\\\server\\share\\file.txt")
    assert _is_unc_path("//server/share/file.txt")
    assert not _is_unc_path("C:\\Users\\x.txt")
    assert not _is_unc_path("/home/user/x.txt")


def test_detect_encoding_utf8() -> None:
    enc, bom = _detect_encoding(b"plain ascii text\n")
    # chardet may report "ascii" for pure-ASCII; both are decode-compatible.
    assert enc in {"utf-8", "ascii"}
    assert bom == b""


def test_detect_encoding_utf8_bom() -> None:
    enc, bom = _detect_encoding(b"\xef\xbb\xbfhello")
    assert enc == "utf-8-sig"
    assert bom == b"\xef\xbb\xbf"


def test_detect_encoding_utf16_le() -> None:
    enc, bom = _detect_encoding(b"\xff\xfeh\x00i\x00")
    assert enc == "utf-16-le"
    assert bom == b"\xff\xfe"


def test_detect_encoding_utf16_be() -> None:
    enc, bom = _detect_encoding(b"\xfe\xff\x00h\x00i")
    assert enc == "utf-16-be"
    assert bom == b"\xfe\xff"


def test_resolve_screenshot_noop_for_existing(tmp_path: Path) -> None:
    f = tmp_path / "normal.txt"
    f.write_text("hi")
    assert _resolve_screenshot_path(str(f)) == str(f)


# ---------------------------------------------------------------------------
# FileRead
# ---------------------------------------------------------------------------


def test_read_plain_text(tmp_path: Path, reader: FileRead) -> None:
    f = tmp_path / "hello.txt"
    f.write_text("line1\nline2\nline3\n")
    out = reader.call(file_path=str(f))
    assert out["content"].startswith("line1")
    assert out["lines_read"] >= 3
    assert out["encoding"] in {"utf-8", "ascii"}
    assert out["truncated"] is False


def test_read_updates_read_state(
    tmp_path: Path, reader: FileRead, cache_dir: str,
) -> None:
    f = tmp_path / "a.txt"
    f.write_text("content")
    reader.call(file_path=str(f))
    entry = reader._state.get(str(f))
    assert entry is not None
    assert "timestamp" in entry
    assert "content_hash" in entry


def test_read_utf16_le(tmp_path: Path, reader: FileRead) -> None:
    f = tmp_path / "u16.txt"
    f.write_bytes(b"\xff\xfeh\x00i\x00")
    out = reader.call(file_path=str(f))
    assert "hi" in out["content"]
    assert out["encoding"] == "utf-16-le"


def test_read_refuses_binary(tmp_path: Path, reader: FileRead) -> None:
    f = tmp_path / "bin.dat"
    f.write_bytes(b"\x00\x01\x02\x03" * 10)
    with pytest.raises(BinaryFileError):
        reader.call(file_path=str(f))


def test_read_refuses_binary_by_extension(
    tmp_path: Path, reader: FileRead,
) -> None:
    f = tmp_path / "image.png"
    f.write_bytes(b"fake but png extension")
    with pytest.raises(BinaryFileError):
        reader.call(file_path=str(f))


def test_read_line_cap(tmp_path: Path, cache_dir: str) -> None:
    r = FileRead(cache_dir=cache_dir, max_lines=5)
    f = tmp_path / "long.txt"
    f.write_text("\n".join(f"line{i}" for i in range(20)))
    out = r.call(file_path=str(f))
    assert out["lines_read"] == 5
    assert out["truncated"] is True


def test_read_byte_cap(tmp_path: Path, cache_dir: str) -> None:
    r = FileRead(cache_dir=cache_dir, max_bytes=64)
    f = tmp_path / "big.txt"
    f.write_text("A" * 500)
    out = r.call(file_path=str(f))
    assert out["bytes_read"] == 64
    assert out["truncated"] is True


def test_read_offset(tmp_path: Path, reader: FileRead) -> None:
    f = tmp_path / "offset.txt"
    f.write_text("a\nb\nc\nd\n")
    out = reader.call(file_path=str(f), offset=2)
    assert "c" in out["content"]
    assert "a" not in out["content"]


def test_read_limit(tmp_path: Path, reader: FileRead) -> None:
    f = tmp_path / "limit.txt"
    f.write_text("a\nb\nc\nd\ne\n")
    out = reader.call(file_path=str(f), limit=2)
    assert out["lines_read"] == 2


def test_read_blocks_dev_zero(reader: FileRead) -> None:
    with pytest.raises(PermissionError):
        reader.call(file_path="/dev/zero")


def test_read_blocks_proc_fd(reader: FileRead) -> None:
    with pytest.raises(PermissionError):
        reader.call(file_path="/proc/self/fd/0")


def test_read_rejects_unc(reader: FileRead) -> None:
    with pytest.raises(PermissionError):
        reader.call(file_path="\\\\server\\share\\x.txt")


def test_read_file_not_found(tmp_path: Path, reader: FileRead) -> None:
    with pytest.raises(FileNotFoundError):
        reader.call(file_path=str(tmp_path / "nope.txt"))


def test_read_requires_file_path(reader: FileRead) -> None:
    with pytest.raises(ValueError):
        reader.call()


# ---------------------------------------------------------------------------
# FileWrite
# ---------------------------------------------------------------------------


def test_write_create_new_no_prior_read(
    tmp_path: Path, writer: FileWrite,
) -> None:
    """Create-new is always allowed — no prior read needed."""
    target = tmp_path / "new.txt"
    out = writer.call(file_path=str(target), content="fresh content")
    assert out["created"] is True
    assert target.read_text() == "fresh content"


def test_write_refuses_existing_without_prior_read(
    tmp_path: Path, writer: FileWrite,
) -> None:
    """readFileState invariant: existing file + no prior read → refuse."""
    target = tmp_path / "exists.txt"
    target.write_text("old")
    with pytest.raises(ReadBeforeWriteError):
        writer.call(file_path=str(target), content="new")
    # File unchanged.
    assert target.read_text() == "old"


def test_write_allowed_after_read(
    tmp_path: Path, reader: FileRead, writer: FileWrite,
) -> None:
    target = tmp_path / "flow.txt"
    target.write_text("old")
    reader.call(file_path=str(target))
    out = writer.call(file_path=str(target), content="new")
    assert out["created"] is False
    assert target.read_text() == "new"


def test_write_refuses_stale_read(
    tmp_path: Path, reader: FileRead, writer: FileWrite,
) -> None:
    """If mtime advances after the recorded read, refuse."""
    target = tmp_path / "stale.txt"
    target.write_text("v1")
    reader.call(file_path=str(target))
    # Mutate the file externally.
    time.sleep(0.05)
    target.write_text("v2 external")
    # Bump mtime to be clearly greater than recorded.
    future = time.time() + 10
    os.utime(str(target), (future, future))
    with pytest.raises(ReadBeforeWriteError):
        writer.call(file_path=str(target), content="v3")


def test_write_atomic_via_tempfile(
    tmp_path: Path, writer: FileWrite,
) -> None:
    target = tmp_path / "atomic.txt"
    writer.call(file_path=str(target), content="payload")
    # No stray tempfiles left behind.
    leftovers = [
        p for p in tmp_path.iterdir() if p.name.startswith(".cc_cortex_write_")
    ]
    assert leftovers == []


def test_write_preserves_utf8_bom(
    tmp_path: Path, reader: FileRead, writer: FileWrite,
) -> None:
    target = tmp_path / "bom.txt"
    target.write_bytes(b"\xef\xbb\xbfhello")
    reader.call(file_path=str(target))
    writer.call(file_path=str(target), content="world")
    data = target.read_bytes()
    assert data.startswith(b"\xef\xbb\xbf")
    assert b"world" in data


def test_write_updates_read_state(
    tmp_path: Path, writer: FileWrite,
) -> None:
    target = tmp_path / "state.txt"
    writer.call(file_path=str(target), content="x")
    entry = writer._state.get(str(target))
    assert entry is not None
    assert entry.get("content_hash") == _content_hash("x")


def test_write_rejects_unc(writer: FileWrite) -> None:
    with pytest.raises(PermissionError):
        writer.call(file_path="\\\\server\\share\\x.txt", content="y")


def test_write_requires_string_content(
    tmp_path: Path, writer: FileWrite,
) -> None:
    with pytest.raises(TypeError):
        writer.call(file_path=str(tmp_path / "x.txt"), content=123)


# ---------------------------------------------------------------------------
# FileEdit
# ---------------------------------------------------------------------------


def test_edit_happy_path(
    tmp_path: Path, reader: FileRead, editor: FileEdit,
) -> None:
    target = tmp_path / "edit.txt"
    target.write_text("hello world\n")
    reader.call(file_path=str(target))
    out = editor.call(
        file_path=str(target),
        old_string="world",
        new_string="python",
    )
    assert out["replacements"] == 1
    assert target.read_text() == "hello python\n"


def test_edit_refuses_no_prior_read(
    tmp_path: Path, editor: FileEdit,
) -> None:
    target = tmp_path / "no_read.txt"
    target.write_text("abc")
    with pytest.raises(ReadBeforeWriteError):
        editor.call(file_path=str(target), old_string="a", new_string="z")
    assert target.read_text() == "abc"


def test_edit_multiple_matches_without_replace_all(
    tmp_path: Path, reader: FileRead, editor: FileEdit,
) -> None:
    target = tmp_path / "multi.txt"
    target.write_text("foo foo foo\n")
    reader.call(file_path=str(target))
    with pytest.raises(MultipleMatchesError):
        editor.call(
            file_path=str(target), old_string="foo", new_string="bar",
        )
    # File unchanged.
    assert target.read_text() == "foo foo foo\n"


def test_edit_replace_all(
    tmp_path: Path, reader: FileRead, editor: FileEdit,
) -> None:
    target = tmp_path / "multi.txt"
    target.write_text("foo foo foo\n")
    reader.call(file_path=str(target))
    out = editor.call(
        file_path=str(target),
        old_string="foo",
        new_string="bar",
        replace_all=True,
    )
    assert out["replacements"] == 3
    assert target.read_text() == "bar bar bar\n"


def test_edit_old_string_not_found(
    tmp_path: Path, reader: FileRead, editor: FileEdit,
) -> None:
    target = tmp_path / "nf.txt"
    target.write_text("abc")
    reader.call(file_path=str(target))
    with pytest.raises(ValueError):
        editor.call(
            file_path=str(target), old_string="xyz", new_string="q",
        )


def test_edit_identical_old_new_refused(
    tmp_path: Path, reader: FileRead, editor: FileEdit,
) -> None:
    target = tmp_path / "same.txt"
    target.write_text("abc")
    reader.call(file_path=str(target))
    with pytest.raises(ValueError):
        editor.call(
            file_path=str(target), old_string="abc", new_string="abc",
        )


def test_edit_rejects_unc(editor: FileEdit) -> None:
    with pytest.raises(PermissionError):
        editor.call(
            file_path="\\\\server\\share\\x.txt",
            old_string="a",
            new_string="b",
        )


def test_edit_rejects_missing_file(
    tmp_path: Path, editor: FileEdit,
) -> None:
    with pytest.raises(FileNotFoundError):
        editor.call(
            file_path=str(tmp_path / "nope.txt"),
            old_string="a",
            new_string="b",
        )


def test_edit_refuses_after_external_modification(
    tmp_path: Path, reader: FileRead, editor: FileEdit,
) -> None:
    target = tmp_path / "ext.txt"
    target.write_text("original")
    reader.call(file_path=str(target))
    # External mutation with bumped mtime.
    time.sleep(0.05)
    target.write_text("tampered")
    future = time.time() + 10
    os.utime(str(target), (future, future))
    with pytest.raises(ReadBeforeWriteError):
        editor.call(
            file_path=str(target),
            old_string="tampered",
            new_string="x",
        )


def test_edit_large_file_refused(
    tmp_path: Path, reader: FileRead, editor: FileEdit, monkeypatch,
) -> None:
    target = tmp_path / "big.txt"
    target.write_text("hello world")
    reader.call(file_path=str(target))

    # Fake os.path.getsize to report > 1 GiB.
    real_getsize = os.path.getsize

    def fake_getsize(p: str) -> int:
        if p == str(target):
            return 2 * 1024 * 1024 * 1024
        return real_getsize(p)

    monkeypatch.setattr(os.path, "getsize", fake_getsize)
    with pytest.raises(ValueError, match="too large"):
        editor.call(
            file_path=str(target),
            old_string="world",
            new_string="python",
        )


def test_edit_utf16_bom_preserved(
    tmp_path: Path, reader: FileRead, editor: FileEdit,
) -> None:
    target = tmp_path / "u16.txt"
    target.write_bytes(b"\xff\xfeh\x00e\x00l\x00l\x00o\x00")
    reader.call(file_path=str(target))
    editor.call(
        file_path=str(target), old_string="hello", new_string="world",
    )
    raw = target.read_bytes()
    assert raw.startswith(b"\xff\xfe")
    # BOM + utf-16-le body; no double-BOM.
    assert raw.count(b"\xff\xfe") == 1
    assert raw[2:] == "world".encode("utf-16-le")


# ---------------------------------------------------------------------------
# Cross-tool flow
# ---------------------------------------------------------------------------


def test_read_edit_write_flow(
    tmp_path: Path,
    reader: FileRead,
    editor: FileEdit,
    writer: FileWrite,
) -> None:
    """Read → Edit → Write: each step should refresh readFileState so the
    next step's invariant check passes.
    """
    target = tmp_path / "flow.py"
    target.write_text("value = 1\n")
    reader.call(file_path=str(target))
    editor.call(
        file_path=str(target), old_string="value = 1", new_string="value = 2",
    )
    # After Edit the readFileState has been refreshed; a subsequent Write
    # should not trip the invariant.
    writer.call(file_path=str(target), content="value = 3\n")
    assert target.read_text() == "value = 3\n"


def test_write_then_read_state_consistent(
    tmp_path: Path, writer: FileWrite, reader: FileRead,
) -> None:
    target = tmp_path / "consistent.txt"
    writer.call(file_path=str(target), content="alpha")
    out = reader.call(file_path=str(target))
    assert "alpha" in out["content"]


def test_default_max_bytes_constant() -> None:
    # Sanity: exported constant matches CC's MAX_OUTPUT_SIZE convention.
    assert DEFAULT_MAX_BYTES == 256 * 1024


def test_read_no_exception_on_utf8_chardet_fallback(
    tmp_path: Path, reader: FileRead,
) -> None:
    f = tmp_path / "latin.txt"
    f.write_bytes("éèê résumé café".encode("latin-1"))
    out = reader.call(file_path=str(f))
    # chardet may pick latin-1 / iso-8859-1 / utf-8; any is fine,
    # as long as we got content back.
    assert out["content"]
