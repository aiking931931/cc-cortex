#!/usr/bin/env python3
"""concinno.code_guard — Unified static analysis for Python/Rust/Go.

@module code_guard
@responsibility Run linters (ruff/cargo/go vet) on written files with SHA256 caching,
    timeout protection, and zero-noise output. PostToolUse guard.
@dependencies concinno.core.log, concinno.core.path_utils, concinno.core.state_store,
    concinno.guards.base
@exports check_code_guard, CodeGuard
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from concinno.core.log import get_logger
from concinno.core.path_utils import extract_file_path
from concinno.core.state_store import StateStore
from concinno.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

logger = get_logger(__name__)

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_DEFAULT_TIMEOUT = 15

_CACHE_NS = "code_guard"
_CACHE_FILE = "sha_cache.json"


def _hidden_startupinfo():
    if sys.platform != "win32":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    return si


# ── SHA256 Cache (via StateStore) ─────────────────────────


def _get_cache_store() -> StateStore:
    cache_dir = os.path.join(
        os.environ.get("CLAUDE_PROJECT_DIR", "."), ".concinno_cache",
    )
    return StateStore(cache_dir)


def _file_sha256(path: str) -> str:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]
    except Exception:
        return ""


def _is_cached(path: str, sha: str) -> bool:
    store = _get_cache_store()
    cache = store.read_flat(_CACHE_NS, _CACHE_FILE, default={})
    return cache.get(path) == sha


def _update_cache(path: str, sha: str) -> None:
    store = _get_cache_store()
    cache = store.read_flat(_CACHE_NS, _CACHE_FILE, default={})
    cache[path] = sha
    # Keep cache bounded
    if len(cache) > 500:
        keys = list(cache.keys())
        cache = {k: cache[k] for k in keys[-400:]}
    store.write_flat(_CACHE_NS, _CACHE_FILE, cache)


# ── Runners ───────────────────────────────────────────────


def _run_cmd(
    cmd: list[str],
    cwd: str | None = None,
    timeout: int = _DEFAULT_TIMEOUT,
) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            creationflags=_CREATE_NO_WINDOW,
            startupinfo=_hidden_startupinfo(),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        return None


def _check_python(file_path: str) -> Optional[str]:
    """Run ruff check on a Python file."""
    result = _run_cmd(["ruff", "check", "--select=E,F,W", "--no-fix", file_path])
    if result is None or result.returncode == 0:
        return None

    lines = [ln for ln in (result.stdout or "").splitlines() if ln.strip() and "Found" not in ln]
    count = len(lines)
    if count == 0:
        return None

    shown = lines[:5]
    base = os.path.basename(file_path)
    summary = "\n".join(f"  {ln.strip()}" for ln in shown)
    if count > 5:
        summary += f"\n  ... and {count - 5} more"
    msg = f"🔴 ruff ❌ {count} issues ({base}) — a clean codebase is a navigable codebase"
    return f"{msg}:\n{summary}"


def _check_rust(file_path: str) -> Optional[str]:
    """Run cargo check in the nearest Cargo.toml directory."""
    d = os.path.dirname(os.path.abspath(file_path))
    cargo_dir = None
    for _ in range(10):
        if os.path.isfile(os.path.join(d, "Cargo.toml")):
            cargo_dir = d
            break
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    if not cargo_dir:
        return None

    result = _run_cmd(["cargo", "check", "--message-format=short"], cwd=cargo_dir, timeout=30)
    if result is None or result.returncode == 0:
        return None

    errors = [ln for ln in (result.stderr or "").splitlines() if "error" in ln.lower()]
    count = len(errors)
    if count == 0:
        return None

    shown = errors[:3]
    summary = "\n".join(f"  {ln.strip()}" for ln in shown)
    if count > 3:
        summary += f"\n  ... and {count - 3} more"
    return f"🔴 cargo ❌ {count} errors — the compiler is the first reviewer:\n{summary}"


def _check_go(file_path: str) -> Optional[str]:
    """Run go vet on the package containing the file."""
    pkg_dir = os.path.dirname(os.path.abspath(file_path))
    result = _run_cmd(["go", "vet", "./..."], cwd=pkg_dir, timeout=20)
    if result is None or result.returncode == 0:
        return None

    output = (result.stderr or "") + (result.stdout or "")
    lines = [ln for ln in output.splitlines() if ln.strip()]
    count = len(lines)
    if count == 0:
        return None

    shown = lines[:3]
    summary = "\n".join(f"  {ln.strip()}" for ln in shown)
    if count > 3:
        summary += f"\n  ... and {count - 3} more"
    msg = f"🔴 go vet ❌ {count} issues — clean foundations support everything built on top"
    return f"{msg}:\n{summary}"


# ── Public API ────────────────────────────────────────────

_EXT_CHECKER = {
    ".py": _check_python,
    ".rs": _check_rust,
    ".go": _check_go,
}

SUPPORTED_EXTENSIONS = frozenset(_EXT_CHECKER.keys())


def check_code_guard(
    tool_name: str,
    tool_input: dict,
    *,
    use_cache: bool = True,
) -> Optional[str]:
    """Unified entry point for code quality checks.

    Args:
        tool_name: Claude Code tool name (Write/Edit/etc.)
        tool_input: Tool input dict with file_path
        use_cache: Skip check if file content unchanged (SHA256)

    Returns:
        Error message string, or None if clean.
    """
    file_path = extract_file_path(tool_input)
    if not file_path or not os.path.isfile(file_path):
        return None

    ext = os.path.splitext(file_path)[1].lower()
    checker = _EXT_CHECKER.get(ext)
    if not checker:
        return None

    # SHA256 cache: skip if unchanged
    if use_cache:
        sha = _file_sha256(file_path)
        if sha and _is_cached(file_path, sha):
            return None

    result = checker(file_path)

    # Update cache on success
    if result is None and use_cache:
        sha = _file_sha256(file_path)
        if sha:
            _update_cache(file_path, sha)

    return result


# ── BaseGuard adapter ───────────────────────────────────────────


_DEBT_NS = "lint_debt"
_DEBT_FILE = "debt.json"


def _read_lint_debt() -> dict[str, str]:
    """Read persisted lint debt from state store."""
    store = _get_cache_store()
    data = store.read_flat(_DEBT_NS, _DEBT_FILE, default={})
    return data if isinstance(data, dict) else {}


def _write_lint_debt(debt: dict[str, str]) -> None:
    """Persist lint debt to state store."""
    store = _get_cache_store()
    store.write_flat(_DEBT_NS, _DEBT_FILE, debt)


class CodeGuard(BaseGuard):
    """Pre+PostToolUse: lint debt enforcement + ruff/cargo/go vet on written files.

    PostToolUse: run linter → errors found → persist lint debt.
    PreToolUse: lint debt exists for OTHER files → DENY Write/Edit until fixed.
    Edits to the file WITH debt are allowed (user is fixing it).
    """

    name = "code_guard"
    category = GuardCategory.QUALITY

    def check(self, ctx: GuardContext) -> GuardResult | None:
        """PreToolUse: block Write/Edit when lint debt exists for other files.

        Allows edits to the file that HAS debt (user is presumably fixing it).
        Clears stale debt entries for files that no longer exist.

        Args:
            ctx: Guard context with tool_name and tool_input.

        Returns:
            GuardResult.deny if lint debt exists for other files, else None.
        """
        if ctx.tool_name not in ("Write", "Edit"):
            return None

        debt = _read_lint_debt()
        if not debt:
            return None

        # Clean stale entries (deleted files)
        cleaned = {f: msg for f, msg in debt.items() if os.path.isfile(f)}
        if len(cleaned) != len(debt):
            _write_lint_debt(cleaned)
        if not cleaned:
            return None

        current_file = extract_file_path(ctx.tool_input) or ""
        # Normalize for comparison
        current_norm = os.path.normpath(current_file) if current_file else ""

        # Allow edits to the file WITH debt (user is fixing it)
        other_debt = {
            f: msg for f, msg in cleaned.items()
            if os.path.normpath(f) != current_norm
        }
        if not other_debt:
            return None

        files_str = ", ".join(
            os.path.basename(f) for f in list(other_debt.keys())[:3]
        )
        debt_details = "\n".join(
            msg for msg in list(other_debt.values())[:3]
        )
        return GuardResult.deny(
            reason=(
                f"🔴 Lint debt: {files_str} has unfixed lint errors. "
                f"A builder who leaves cracks in one wall doesn't start the next."
            ),
            context=debt_details,
        )

    def on_post_tool(self, ctx: GuardContext) -> GuardResult | None:
        """Run ruff/cargo/go vet on written files. Persist lint debt on failure.

        Args:
            ctx: Guard context with tool_name and tool_input.

        Returns:
            GuardResult.allow with lint errors as context if issues found,
            or None if clean (also clears debt for this file).
        """
        if ctx.tool_name not in ("Write", "Edit"):
            return None

        file_path = extract_file_path(ctx.tool_input)
        feedback = check_code_guard(ctx.tool_name, ctx.tool_input)

        if feedback and file_path:
            # Persist lint debt — next PreToolUse Write/Edit to OTHER files blocked
            debt = _read_lint_debt()
            debt[os.path.normpath(file_path)] = feedback
            _write_lint_debt(debt)
            return GuardResult.allow(context=feedback)

        if file_path:
            # Clean: clear this file's debt
            norm = os.path.normpath(file_path)
            debt = _read_lint_debt()
            if norm in debt:
                del debt[norm]
                _write_lint_debt(debt)

        return None
