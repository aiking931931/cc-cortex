"""cc_cortex.tools.builtin.search — Glob and Grep tools (Aegis P0.3).

@module tools.builtin.search
@responsibility In-process Python implementations of Claude Code's GlobTool
    and GrepTool, with two improvements over CC: (1) Glob returns
    continuation cursors instead of silently truncating at 100 files,
    (2) Grep transparently falls back to a Python ``re`` backend when the
    ``rg`` (ripgrep) binary is not available.
@dependencies pathlib, re, subprocess, shutil, base64 (all stdlib)
@exports FileGlob, FileGrep, EXCLUDED_DIRS

Both tools satisfy ``cc_cortex.tool_executor.Tool`` (``name``, ``description``,
``call(**kwargs)``) and expose ``is_concurrency_safe = True`` so the CC
partitioner may schedule them in parallel.

Glob matches CC's behavior of mtime-descending sort and excluded-dir skipping
(``.git``, ``node_modules``, etc.), but instead of dropping results past
``head_limit`` it returns ``next_cursor`` — a base64-encoded offset — that
the caller passes back to resume.

Grep prefers ``rg`` when available (faster, honors ``.gitignore``-style
behavior implicitly via our excluded dirs) and otherwise walks files in
Python and applies ``re.compile`` per file. The ``backend`` field of the
result reports which path was taken so callers can reason about expected
performance characteristics.
"""

from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Iterable, Literal

# ── Constants ───────────────────────────────────────────────────────────

#: Directories whose contents are skipped during traversal. Mirrors CC's
#: GlobTool/GrepTool exclusions plus a few cc-cortex-specific cache dirs.
EXCLUDED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".svn",
        ".hg",
        ".bzr",
        ".jj",
        ".sl",
        "node_modules",
        "__pycache__",
        ".cc_cortex_cache",
        ".destruction_backups",
        ".claude-forge",
    }
)

OutputMode = Literal["files_with_matches", "content", "count"]

_VALID_OUTPUT_MODES: frozenset[str] = frozenset(
    {"files_with_matches", "content", "count"}
)


# ── Helpers ─────────────────────────────────────────────────────────────


def _is_excluded(path: Path) -> bool:
    """Return True if any path component is in :data:`EXCLUDED_DIRS`.

    We test each component (not just the last) so that a deeply nested
    file like ``a/node_modules/b/c.js`` is excluded even when iteration
    yields it directly.
    """
    return any(part in EXCLUDED_DIRS for part in path.parts)


def _encode_cursor(idx: int) -> str:
    """Encode an integer offset as an opaque base64 cursor."""
    raw = json.dumps({"i": int(idx)}).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(cursor: str) -> int:
    """Decode a cursor produced by :func:`_encode_cursor`. Returns 0 on
    any error (cursor is opaque; bad cursors restart from the beginning).
    """
    if not cursor:
        return 0
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
        return int(data.get("i", 0))
    except (ValueError, TypeError, json.JSONDecodeError):
        return 0


def _safe_mtime(path: Path) -> float:
    """Return path mtime, or 0.0 if the file vanished mid-iteration."""
    try:
        return path.stat().st_mtime
    except (OSError, FileNotFoundError):
        return 0.0


def _iter_glob(root: Path, pattern: str) -> Iterable[Path]:
    """Iterate ``root.rglob(pattern)`` skipping excluded dirs and
    swallowing per-entry permission errors.
    """
    try:
        iterator = root.rglob(pattern)
    except (OSError, ValueError):
        return
    while True:
        try:
            entry = next(iterator)
        except StopIteration:
            return
        except (OSError, PermissionError):
            continue
        if _is_excluded(entry.relative_to(root) if entry.is_absolute() else entry):
            continue
        yield entry


def _python_grep_fallback(
    *,
    pattern: str,
    root: Path,
    glob: str,
    output_mode: OutputMode,
    multiline: bool,
    case_insensitive: bool,
    context_before: int,
    context_after: int,
) -> list[Any]:
    """Pure-Python regex grep used when ``rg`` is unavailable.

    Returns a list shaped per ``output_mode``:

    - ``files_with_matches`` → ``list[str]`` of relative paths
    - ``content``            → ``list[{file, line, text}]``
    - ``count``              → ``list[{file, count}]``

    Files are walked via :func:`_iter_glob` so excluded dirs are skipped.
    Binary files (those whose first read raises ``UnicodeDecodeError``)
    are silently skipped — same posture as CC's grep.
    """
    flags = re.MULTILINE
    if multiline:
        flags |= re.DOTALL
    if case_insensitive:
        flags |= re.IGNORECASE
    try:
        regex = re.compile(pattern, flags)
    except re.error as exc:
        msg = f"invalid regex: {exc}"
        raise ValueError(msg) from exc

    results: list[Any] = []
    for entry in _iter_glob(root, glob):
        if not entry.is_file():
            continue
        try:
            text = entry.read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, OSError, PermissionError):
            continue

        rel = str(entry.relative_to(root)) if entry.is_absolute() else str(entry)

        if output_mode == "files_with_matches":
            if regex.search(text):
                results.append(rel)
            continue

        if output_mode == "count":
            n = len(regex.findall(text))
            if n > 0:
                results.append({"file": rel, "count": n})
            continue

        # output_mode == "content"
        lines = text.splitlines()
        if multiline:
            # In multiline mode we still report per-line hits using the
            # earliest match line so callers can navigate. Match spans
            # the file; we approximate by line number of the start.
            for match in regex.finditer(text):
                start_line = text.count("\n", 0, match.start()) + 1
                results.append(
                    {
                        "file": rel,
                        "line": start_line,
                        "text": lines[start_line - 1] if start_line - 1 < len(lines) else "",
                    }
                )
            continue

        for idx, line in enumerate(lines):
            if regex.search(line):
                lo = max(0, idx - context_before)
                hi = min(len(lines), idx + context_after + 1)
                if context_before == 0 and context_after == 0:
                    results.append({"file": rel, "line": idx + 1, "text": line})
                else:
                    for j in range(lo, hi):
                        results.append(
                            {
                                "file": rel,
                                "line": j + 1,
                                "text": lines[j],
                            }
                        )

    return results


def _run_rg(
    *,
    pattern: str,
    root: Path,
    glob: str,
    output_mode: OutputMode,
    multiline: bool,
    case_insensitive: bool,
    context_before: int,
    context_after: int,
) -> list[Any]:
    """Call the ``rg`` binary and parse its stdout into our result schema.

    Always passes ``--no-config`` and excludes :data:`EXCLUDED_DIRS` via
    repeated ``--glob '!dir'`` flags so behavior matches the Python
    fallback regardless of the host's ripgrep config.
    """
    cmd: list[str] = ["rg", "--no-config"]

    for excluded in sorted(EXCLUDED_DIRS):
        cmd.extend(["--glob", f"!{excluded}"])

    if glob and glob != "*":
        cmd.extend(["--glob", glob])

    if case_insensitive:
        cmd.append("-i")
    if multiline:
        cmd.extend(["-U", "--multiline-dotall"])

    if output_mode == "files_with_matches":
        cmd.append("--files-with-matches")
    elif output_mode == "count":
        cmd.append("--count")
    elif output_mode == "content":
        cmd.append("-n")
        if context_before > 0:
            cmd.extend(["-B", str(context_before)])
        if context_after > 0:
            cmd.extend(["-A", str(context_after)])

    cmd.extend(["-e", pattern, str(root)])

    try:
        proc = subprocess.run(  # noqa: S603 — args fully controlled above
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        msg = f"rg invocation failed: {exc}"
        raise RuntimeError(msg) from exc

    # rg exit code 1 == "no matches", which is not an error for us.
    if proc.returncode not in (0, 1):
        msg = f"rg failed (exit {proc.returncode}): {proc.stderr.strip()}"
        raise RuntimeError(msg)

    return _parse_rg_output(
        proc.stdout, root=root, output_mode=output_mode
    )


def _parse_rg_output(
    stdout: str, *, root: Path, output_mode: OutputMode
) -> list[Any]:
    """Parse rg stdout for the given output_mode into our schema."""
    lines = stdout.splitlines()
    results: list[Any] = []

    if output_mode == "files_with_matches":
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rel = str(Path(line).relative_to(root))
            except ValueError:
                rel = line
            results.append(rel)
        return results

    if output_mode == "count":
        for line in lines:
            line = line.strip()
            if not line or ":" not in line:
                continue
            file_part, _, count_part = line.rpartition(":")
            try:
                n = int(count_part)
            except ValueError:
                continue
            try:
                rel = str(Path(file_part).relative_to(root))
            except ValueError:
                rel = file_part
            results.append({"file": rel, "count": n})
        return results

    # content mode: rg -n format is "file:line:text"; with context blocks
    # rg uses "--" separators between groups which we just skip.
    for line in lines:
        if line == "--" or not line:
            continue
        # Path may contain ":" on Windows ("C:\..."); split from the
        # left twice from the first colon AFTER drive prefix.
        first = line.find(":")
        if first == -1:
            continue
        # If colon is part of "C:" drive, advance.
        if first == 1 and len(line) > 2 and line[2] in ("\\", "/"):
            first = line.find(":", first + 1)
            if first == -1:
                continue
        second = line.find(":", first + 1)
        if second == -1:
            continue
        file_part = line[:first]
        line_part = line[first + 1 : second]
        text_part = line[second + 1 :]
        try:
            line_no = int(line_part)
        except ValueError:
            continue
        try:
            rel = str(Path(file_part).relative_to(root))
        except ValueError:
            rel = file_part
        results.append({"file": rel, "line": line_no, "text": text_part})

    return results


# ── FileGlob ────────────────────────────────────────────────────────────


class FileGlob:
    """Glob tool — recursive pathname matching with paginated results.

    Diverges from CC's GlobTool in one respect: instead of silently
    truncating at 100 results, we surface ``next_cursor`` so callers can
    resume. Total count is always reported even when truncated.
    """

    name: str = "Glob"
    description: str = (
        "Find files matching a glob pattern. Recursive, mtime-sorted, "
        "skips VCS and cache directories. Supports paginated results via "
        "next_cursor."
    )
    is_concurrency_safe: bool = True

    def call(
        self,
        *,
        pattern: str,
        path: str = ".",
        head_limit: int = 100,
        cursor: str = "",
        **_: Any,
    ) -> dict[str, Any]:
        """Execute the glob.

        Args:
            pattern: Glob pattern (e.g. ``"**/*.py"``, ``"src/*.ts"``).
            path: Directory to search under. Defaults to cwd.
            head_limit: Maximum results to return this call. ``0`` means
                unlimited (returns everything; ``next_cursor`` will be
                ``None``).
            cursor: Opaque continuation token from a previous call.

        Returns:
            ``{"matches": list[str], "total": int, "truncated": bool,
              "next_cursor": str | None}``
        """
        if not pattern:
            msg = "pattern is required"
            raise ValueError(msg)

        root = Path(path).resolve()
        if not root.exists() or not root.is_dir():
            msg = f"path is not a directory: {path}"
            raise ValueError(msg)

        # Collect everything (we need total count and stable mtime sort).
        candidates: list[tuple[float, Path]] = []
        for entry in _iter_glob(root, pattern):
            if not entry.is_file():
                continue
            candidates.append((_safe_mtime(entry), entry))

        candidates.sort(key=lambda pair: pair[0], reverse=True)
        all_paths = [str(p.relative_to(root)) for _, p in candidates]
        total = len(all_paths)

        start = _decode_cursor(cursor)
        if start < 0:
            start = 0

        if head_limit <= 0:
            page = all_paths[start:]
            end = total
        else:
            end = min(start + head_limit, total)
            page = all_paths[start:end]

        truncated = end < total
        next_cursor = _encode_cursor(end) if truncated else None

        return {
            "matches": page,
            "total": total,
            "truncated": truncated,
            "next_cursor": next_cursor,
        }


# ── FileGrep ────────────────────────────────────────────────────────────


class FileGrep:
    """Grep tool — regex search with rg backend and Python fallback."""

    name: str = "Grep"
    description: str = (
        "Search file contents with a regex. Prefers ripgrep when "
        "available, otherwise falls back to Python re. Excludes VCS and "
        "cache directories automatically."
    )
    is_concurrency_safe: bool = True

    def __init__(self, *, force_backend: str | None = None) -> None:
        """Construct the tool.

        Args:
            force_backend: ``"rg"`` or ``"python"`` to pin a backend
                (mostly for tests). Default ``None`` = auto-detect.
        """
        self._force_backend = force_backend

    def _detect_backend(self) -> str:
        if self._force_backend in ("rg", "python"):
            return self._force_backend
        return "rg" if shutil.which("rg") else "python"

    def call(
        self,
        *,
        pattern: str,
        path: str = ".",
        glob: str = "*",
        output_mode: OutputMode = "files_with_matches",
        head_limit: int = 250,
        multiline: bool = False,
        case_insensitive: bool = False,
        context_before: int = 0,
        context_after: int = 0,
        **_: Any,
    ) -> dict[str, Any]:
        """Run the grep.

        Args:
            pattern: Regex (Python ``re`` syntax for the fallback; rg's
                Rust regex syntax for the rg backend — they overlap for
                most common cases).
            path: Directory to search under.
            glob: File glob filter (passed to rg ``--glob`` or used as
                ``rglob`` arg in fallback). ``"*"`` means all files.
            output_mode: ``"files_with_matches"`` | ``"content"`` |
                ``"count"``.
            head_limit: Cap on returned entries. ``0`` means unlimited.
            multiline: If True, ``.`` matches newlines and patterns may
                span lines (rg ``-U --multiline-dotall``).
            case_insensitive: If True, case-insensitive (rg ``-i``).
            context_before: Lines of context before each hit (rg ``-B``).
                Only honored for ``output_mode="content"``.
            context_after: Lines of context after each hit (rg ``-A``).
                Only honored for ``output_mode="content"``.

        Returns:
            ``{"backend": "rg"|"python", "results": list, "total": int,
              "truncated": bool}``
        """
        if not pattern:
            msg = "pattern is required"
            raise ValueError(msg)
        if output_mode not in _VALID_OUTPUT_MODES:
            msg = f"invalid output_mode: {output_mode!r}"
            raise ValueError(msg)

        root = Path(path).resolve()
        if not root.exists() or not root.is_dir():
            msg = f"path is not a directory: {path}"
            raise ValueError(msg)

        backend = self._detect_backend()
        results: list[Any]
        if backend == "rg":
            try:
                results = _run_rg(
                    pattern=pattern,
                    root=root,
                    glob=glob,
                    output_mode=output_mode,
                    multiline=multiline,
                    case_insensitive=case_insensitive,
                    context_before=context_before,
                    context_after=context_after,
                )
            except RuntimeError:
                # rg present but blew up — fall through to Python so
                # callers always get a result rather than an exception.
                backend = "python"
                results = _python_grep_fallback(
                    pattern=pattern,
                    root=root,
                    glob=glob,
                    output_mode=output_mode,
                    multiline=multiline,
                    case_insensitive=case_insensitive,
                    context_before=context_before,
                    context_after=context_after,
                )
        else:
            results = _python_grep_fallback(
                pattern=pattern,
                root=root,
                glob=glob,
                output_mode=output_mode,
                multiline=multiline,
                case_insensitive=case_insensitive,
                context_before=context_before,
                context_after=context_after,
            )

        total = len(results)
        if head_limit <= 0:
            page = results
            truncated = False
        else:
            page = results[:head_limit]
            truncated = total > head_limit

        return {
            "backend": backend,
            "results": page,
            "total": total,
            "truncated": truncated,
        }


__all__ = [
    "EXCLUDED_DIRS",
    "FileGlob",
    "FileGrep",
]
