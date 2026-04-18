#!/usr/bin/env python3
"""concinno.linting — ESLint wrapper for JavaScript/JSX files.

@module linting
@responsibility Run ESLint on JS/JSX files after write, report errors.
               Zero noise: only emits on errors. Timeout-protected.
@dependencies concinno.guards.base
@exports run_linter, LintGuard
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional

from concinno.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_DEFAULT_TIMEOUT = 15


def _hidden_startupinfo():
    if sys.platform != "win32":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    return si


SUPPORTED_EXTENSIONS = frozenset({".js", ".jsx", ".mjs", ".cjs"})


def run_linter(file_path: str, *, timeout: int = _DEFAULT_TIMEOUT) -> Optional[str]:
    """Run eslint on a JavaScript file.

    Returns:
        Error message string, or None if clean.
    """
    if not file_path or not os.path.isfile(file_path):
        return None

    ext = os.path.splitext(file_path)[1].lower()
    if ext not in SUPPORTED_EXTENSIONS:
        return None

    try:
        result = subprocess.run(
            ["npx", "--no-install", "eslint", "--no-color", "--format=compact", file_path],
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
        return None

    output = result.stdout or ""
    error_lines = [
        ln for ln in output.splitlines() if "Error" in ln or "Warning" in ln or ": line" in ln
    ]
    if not error_lines:
        # Try stderr
        error_lines = [ln for ln in (result.stderr or "").splitlines() if ln.strip()]

    count = len(error_lines)
    if count == 0:
        return None

    base = os.path.basename(file_path)
    shown = error_lines[:5]
    summary = "\n".join(f"  {ln.strip()}" for ln in shown)
    if count > 5:
        summary += f"\n  ... and {count - 5} more"

    msg = f"🔴 eslint ❌ {count} issues ({base}) — a clean codebase is a navigable codebase"
    return f"{msg}:\n{summary}"


# ── BaseGuard adapter ───────────────────────────────────────────


class LintGuard(BaseGuard):
    """PostToolUse: run eslint on JS/JSX files."""

    name = "lint_guard"
    feature_name = "linting"
    category = GuardCategory.QUALITY

    def check(self, ctx: GuardContext) -> GuardResult | None:
        """No-op PreToolUse — this guard only acts on PostToolUse.

        Returns:
            Always None.
        """
        return None  # PostToolUse only

    def on_post_tool(self, ctx: GuardContext) -> GuardResult | None:
        """Run ESLint on JS/JSX files after Write/Edit.

        Args:
            ctx: Guard context with tool_name and tool_input.

        Returns:
            GuardResult.allow with ESLint errors as context, or None if clean.
        """
        if ctx.tool_name not in ("Write", "Edit"):
            return None
        fp = ctx.tool_input.get("file_path", "")
        feedback = run_linter(fp)
        if feedback:
            return GuardResult.allow(context=feedback)
        return None
