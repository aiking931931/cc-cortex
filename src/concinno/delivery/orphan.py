"""Orphan export detection (D8).

@module delivery.orphan
@responsibility Detect exported symbols that no other file imports
@dependencies delivery._base
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass

# Export patterns by language
_TS_EXPORT_RE = re.compile(
    r"export\s+(?:function|class|const|let|var|type|interface|enum)\s+"
    r"(\w+)",
)
_TS_NAMED_EXPORT_RE = re.compile(r"export\s*\{([^}]+)\}")
_PY_DEF_RE = re.compile(r"^(?:def|class)\s+(\w+)", re.MULTILINE)
_PY_ALL_RE = re.compile(r"__all__\s*=\s*\[([^\]]+)\]", re.DOTALL)

# Import patterns (what counts as "someone uses this symbol")
_TS_IMPORT_RE = re.compile(r"(?:import|from)\s+['\"].*['\"]|import\s*\{[^}]*\}")
_PY_IMPORT_RE = re.compile(r"(?:from\s+\S+\s+import|import\s+)")

# Skip directories
_SKIP_DIRS = {
    "node_modules", ".git", "dist", "build", "__pycache__",
    ".venv", "venv", ".tox", ".mypy_cache", ".ruff_cache",
}


@dataclass
class OrphanExport:
    """An exported symbol that no other file imports."""

    symbol: str
    file_path: str
    language: str

    def __str__(self) -> str:
        return f"orphan: {self.symbol} in {self.file_path} ({self.language})"


def _detect_language(file_path: str) -> str:
    """Detect language from file extension."""
    if file_path.endswith((".ts", ".tsx", ".js", ".jsx", ".mts", ".mjs")):
        return "typescript"
    if file_path.endswith(".py"):
        return "python"
    return ""


def _parse_comma_names(raw: str, strip_chars: str = "") -> list[str]:
    """Parse comma-separated names, stripping whitespace and optional chars.

    Handles 'name as alias' syntax (extracts original name before 'as').
    """
    names = []
    for item in raw.split(","):
        cleaned = item.strip()
        if strip_chars:
            cleaned = cleaned.strip(strip_chars)
        cleaned = cleaned.strip()
        # Handle "name as alias" → extract "name"
        if " as " in cleaned:
            cleaned = cleaned.split(" as ")[0].strip()
        if cleaned and cleaned.isidentifier():
            names.append(cleaned)
    return names


def _extract_exports_ts(content: str) -> list[str]:
    """Extract exported symbol names from TypeScript/JS content."""
    exports: list[str] = []
    exports.extend(m.group(1) for m in _TS_EXPORT_RE.finditer(content))
    for m in _TS_NAMED_EXPORT_RE.finditer(content):
        exports.extend(_parse_comma_names(m.group(1)))
    return exports


def _extract_exports_py(content: str) -> list[str]:
    """Extract exported symbol names from Python content."""
    all_match = _PY_ALL_RE.search(content)
    if all_match:
        return _parse_comma_names(all_match.group(1), strip_chars="'\"")
    return [
        m.group(1) for m in _PY_DEF_RE.finditer(content)
        if not m.group(1).startswith("_")
    ]


def _extract_exports(content: str, language: str) -> list[str]:
    """Extract exported symbol names from file content."""
    if language == "typescript":
        return _extract_exports_ts(content)
    if language == "python":
        return _extract_exports_py(content)
    return []


def _is_barrel_file(fname: str) -> bool:
    """Check if file is a barrel/index that re-exports (skip for orphan check)."""
    base = os.path.basename(fname).lower()
    barrel_names = {
        "index.ts", "index.js", "index.mts", "index.mjs",
        "__init__.py", "mod.rs", "lib.rs",
    }
    return base in barrel_names


def _hidden_startupinfo() -> subprocess.STARTUPINFO | None:
    """Windows: CREATE_NO_WINDOW startupinfo."""
    import sys

    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore[attr-defined]
        si.wShowWindow = 0  # type: ignore[attr-defined]
        return si
    return None


def _is_symbol_imported(
    symbol: str,
    source_file: str,
    workspace: str,
    language: str = "",
) -> bool:
    """Check if a symbol is imported by any file other than its source.

    Tries rg → grep → walk fallback.

    Args:
        symbol: Symbol name to search for.
        source_file: File that exports the symbol (excluded from results).
        workspace: Project root.
        language: Optional language hint (unused, kept for API compat).
    """
    result = _is_symbol_imported_rg(symbol, source_file, workspace)
    if result is not None:
        return result
    return _is_symbol_imported_walk(symbol, source_file, workspace)


def _is_symbol_imported_rg(
    symbol: str,
    source_file: str,
    workspace: str,
) -> bool | None:
    """Use ripgrep/grep to check if symbol is imported elsewhere."""
    abs_source = os.path.normcase(os.path.abspath(source_file))

    for cmd in ["rg", "grep"]:
        exe = shutil.which(cmd)
        if not exe:
            continue

        # Pattern: import { symbol } or from X import symbol
        pattern = f"\\b{re.escape(symbol)}\\b"
        skip_globs = [f"--glob=!{d}/" for d in _SKIP_DIRS]
        if cmd == "rg":
            args = [exe, "-l", pattern, "--type-add",
                    "code:*.{py,ts,tsx,js,jsx}", "-t", "code",
                    "--no-ignore", *skip_globs, "."]
        else:
            exclude_dirs = []
            for d in _SKIP_DIRS:
                exclude_dirs.extend(["--exclude-dir", d])
            args = [exe, "-rl", pattern, "--include=*.py",
                    "--include=*.ts", "--include=*.tsx", "--include=*.js",
                    *exclude_dirs, "."]

        try:
            proc = subprocess.run(
                args, capture_output=True, text=True, timeout=5,
                cwd=workspace, startupinfo=_hidden_startupinfo(),
            )
            for ln in proc.stdout.strip().split("\n"):
                if not ln:
                    continue
                abs_ln = os.path.normcase(
                    os.path.abspath(os.path.join(workspace, ln)),
                )
                if abs_ln != abs_source:
                    return True
            return False
        except Exception:
            continue
    return None


def _file_contains_symbol(path: str, symbol: str) -> bool:
    """Check if file content contains symbol (first 50K chars)."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return symbol in f.read(50000)
    except Exception:
        return False


def _is_symbol_imported_walk(
    symbol: str,
    source_file: str,
    workspace: str,
) -> bool:
    """Walk workspace to check if symbol appears in other files."""
    norm_source = os.path.normpath(source_file)
    code_exts = (".py", ".ts", ".tsx", ".js", ".jsx")
    count = 0

    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fn in files:
            if not fn.endswith(code_exts):
                continue
            full = os.path.join(root, fn)
            if os.path.normpath(full) == norm_source:
                continue
            if _file_contains_symbol(full, symbol):
                return True
            count += 1
            if count > 500:
                return True  # Assume imported if too many files
    return False


def _build_rg_cmd(
    symbols: list[str],
    workspace: str,
) -> list[str] | None:
    """Build a ripgrep command to batch-check multiple symbols."""
    exe = shutil.which("rg")
    if not exe:
        return None
    pattern = "|".join(re.escape(s) for s in symbols)
    return [
        exe, "-l", "-e", pattern,
        "--type-add", "code:*.{py,ts,tsx,js,jsx}", "-t", "code",
        ".",
    ]


def _batch_check_imported_rg(
    symbols: list[str],
    source_files: dict[str, str],
    workspace: str,
) -> dict[str, set[str]]:
    """Batch check which symbols are imported using ripgrep.

    Args:
        symbols: List of symbol names.
        source_files: Map symbol -> source file path.
        workspace: Project root.

    Returns:
        Dict mapping symbol -> set of files that reference it.
    """
    found: dict[str, set[str]] = {s: set() for s in symbols}
    cmd = _build_rg_cmd(symbols, workspace)
    if not cmd:
        return found

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10,
            cwd=workspace, startupinfo=_hidden_startupinfo(),
        )
        matched_files = [
            ln for ln in proc.stdout.strip().split("\n") if ln
        ]
    except Exception:
        return found

    for mf in matched_files:
        abs_mf = os.path.normcase(os.path.abspath(os.path.join(workspace, mf)))
        try:
            with open(os.path.join(workspace, mf), "r",
                       encoding="utf-8", errors="ignore") as f:
                content = f.read(100000)
        except Exception:
            continue
        for sym in symbols:
            if sym in content:
                abs_source = os.path.normcase(
                    os.path.abspath(source_files.get(sym, "")),
                )
                if abs_mf != abs_source:
                    found[sym].add(abs_mf)
    return found


def check_orphan_exports(
    file_path: str,
    workspace: str = "",
) -> list[OrphanExport]:
    """Check a single file for orphan exports (symbols not imported elsewhere).

    Args:
        file_path: Path to the file to check.
        workspace: Project root for searching imports.

    Returns:
        List of OrphanExport for each symbol not imported elsewhere.
    """
    if not workspace:
        workspace = os.environ.get("CLAUDE_PROJECT_DIR", ".")

    language = _detect_language(file_path)
    if not language:
        return []

    if _is_barrel_file(file_path):
        return []

    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception:
        return []

    exports = _extract_exports(content, language)
    orphans: list[OrphanExport] = []
    for sym in exports:
        if not _is_symbol_imported(sym, file_path, workspace):
            orphans.append(OrphanExport(
                symbol=sym, file_path=file_path, language=language,
            ))
    return orphans


def _collect_file_exports(
    file_path: str,
) -> tuple[str, list[str], str]:
    """Collect exports from a single file.

    Returns:
        (file_path, exports, language) tuple.
    """
    language = _detect_language(file_path)
    if not language or _is_barrel_file(file_path):
        return file_path, [], language
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception:
        return file_path, [], language
    exports = _extract_exports(content, language)
    return file_path, exports, language


def _build_symbol_index(
    file_exports: list[tuple[str, list[str], str]],
) -> tuple[list[str], dict[str, str]]:
    """Build flat symbol list and source map from file exports.

    Returns:
        (all_symbols, symbol_to_source) tuple.
    """
    all_symbols: list[str] = []
    source_map: dict[str, str] = {}
    for fpath, exports, _lang in file_exports:
        for sym in exports:
            all_symbols.append(sym)
            source_map[sym] = fpath
    return all_symbols, source_map


def scan_orphans_batch(
    file_paths: list[str],
    workspace: str = "",
) -> list[OrphanExport]:
    """Batch scan multiple files for orphan exports (optimized with rg).

    Args:
        file_paths: List of files to check.
        workspace: Project root.

    Returns:
        List of OrphanExport across all files.
    """
    if not workspace:
        workspace = os.environ.get("CLAUDE_PROJECT_DIR", ".")

    # Resolve relative paths against workspace
    resolved = [
        fp if os.path.isabs(fp) else os.path.join(workspace, fp)
        for fp in file_paths
    ]
    file_exports = [_collect_file_exports(fp) for fp in resolved]
    all_symbols, source_map = _build_symbol_index(file_exports)

    if not all_symbols:
        return []

    found_in = _batch_check_imported_rg(all_symbols, source_map, workspace)
    has_rg = any(bool(v) for v in found_in.values()) or shutil.which("rg") is not None

    all_orphans: list[OrphanExport] = []
    for fpath, exports, lang in file_exports:
        abs_full = os.path.normcase(os.path.abspath(fpath))
        for sym in exports:
            refs = found_in.get(sym, set())
            if refs - {abs_full}:
                continue  # referenced elsewhere
            # Fallback to walk if rg unavailable
            if not has_rg and _is_symbol_imported_walk(sym, fpath, workspace):
                continue
            if not (refs - {abs_full}):
                all_orphans.append(OrphanExport(
                    symbol=sym, file_path=abs_full, language=lang,
                ))
    return all_orphans
