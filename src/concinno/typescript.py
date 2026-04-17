#!/usr/bin/env python3
"""concinno.typescript — tsc --noEmit checker with SHA256 content cache.

@module typescript
@responsibility Auto-detect TypeScript projects and run tsc --noEmit with SHA256
    content caching to skip unchanged projects. Timeout-protected.
@dependencies none (stdlib only)
@exports check_typescript
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_CACHE_FILE = os.path.join(
    os.environ.get("CLAUDE_PROJECT_DIR", "."), ".concinno_cache", "tsc_sha.json"
)
_DEFAULT_TIMEOUT = 15

SUPPORTED_EXTENSIONS = frozenset({".ts", ".tsx"})


def _hidden_startupinfo():
    if sys.platform != "win32":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    return si


# ── SHA256 Cache ──────────────────────────────────────────


def _load_cache() -> dict:
    try:
        with open(_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(cache: dict):
    try:
        os.makedirs(os.path.dirname(_CACHE_FILE), exist_ok=True)
        tmp = _CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        os.replace(tmp, _CACHE_FILE)
    except Exception:
        pass


def _file_sha256(path: str) -> str:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]
    except Exception:
        return ""


# ── Project Discovery ─────────────────────────────────────


def _find_ts_project(file_path: str) -> Optional[str]:
    """Walk up directories to find nearest tsconfig.json."""
    d = os.path.dirname(os.path.abspath(file_path))
    for _ in range(15):
        if os.path.isfile(os.path.join(d, "tsconfig.json")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


def _match_configured_project(
    file_path: str,
    ts_projects: list[tuple[str, str]] | None,
) -> tuple[Optional[str], Optional[str]]:
    """Match file against configured TS projects (sorted by path length, longest first)."""
    if not ts_projects:
        return None, None
    norm = os.path.normpath(file_path).lower()
    for proj_path, proj_name in ts_projects:
        if norm.startswith(os.path.normpath(proj_path).lower()):
            return proj_path, proj_name
    return None, None


# ── Public API ────────────────────────────────────────────


def check_typescript(
    tool_name: str,
    tool_input: dict,
    *,
    ts_projects: list[tuple[str, str]] | None = None,
    timeout: int = _DEFAULT_TIMEOUT,
    use_cache: bool = True,
) -> Optional[str]:
    """Run tsc --noEmit on the TypeScript project containing the edited file.

    Args:
        tool_name: Claude Code tool name
        tool_input: Tool input dict with file_path
        ts_projects: Optional list of (project_dir, project_name) tuples
        timeout: Max seconds for tsc
        use_cache: Skip if file SHA256 unchanged

    Returns:
        Error message string, or None if clean.
    """
    file_path = (
        tool_input.get("file_path")
        or tool_input.get("notebook_path")
        or tool_input.get("path")
        or ""
    )
    if not file_path:
        return None

    ext = os.path.splitext(file_path)[1].lower()
    if ext not in SUPPORTED_EXTENSIONS:
        return None

    # SHA256 cache check
    if use_cache:
        sha = _file_sha256(file_path)
        if sha:
            cache = _load_cache()
            if cache.get(file_path) == sha:
                return None

    # Find project
    project_dir, project_name = _match_configured_project(file_path, ts_projects)
    if not project_dir:
        project_dir = _find_ts_project(file_path)
        if project_dir:
            project_name = os.path.basename(project_dir)

    if not project_dir:
        return None

    # Run tsc
    try:
        result = subprocess.run(
            ["npx", "--no-install", "tsc", "--noEmit"],
            cwd=project_dir,
            capture_output=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            creationflags=_CREATE_NO_WINDOW,
            startupinfo=_hidden_startupinfo(),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        return None

    if result.returncode == 0:
        # Success: update cache
        if use_cache:
            sha = _file_sha256(file_path)
            if sha:
                cache = _load_cache()
                cache[file_path] = sha
                if len(cache) > 500:
                    keys = list(cache.keys())
                    for k in keys[:100]:
                        del cache[k]
                _save_cache(cache)
        return None

    # Parse errors
    all_output = (result.stdout or "") + (result.stderr or "")
    error_lines = [ln for ln in all_output.splitlines() if "error TS" in ln]
    count = len(error_lines)
    if count == 0:
        return None

    shown = error_lines[:3]
    proj_norm = os.path.normpath(project_dir)

    def shorten(line: str) -> str:
        return line.replace(proj_norm + os.sep, "").replace(proj_norm + "/", "")

    summary = "\n".join(f"  {shorten(ln).strip()}" for ln in shown)
    if count > 3:
        summary += f"\n  ... and {count - 3} more errors"

    name = project_name or os.path.basename(project_dir)
    msg = f"🔴 tsc ❌ {count} errors ({name}) — the type system is the first line of defense"
    return f"{msg}:\n{summary}"
