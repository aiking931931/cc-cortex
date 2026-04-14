"""cc_cortex.sibling_scan — Sibling Pattern Scan guard.

@module sibling_scan
@responsibility After an Edit fixes a pattern in file A, scan for the same
    pattern in sibling files (same extension, same directory tree). If found,
    inject context so the agent doesn't skip related occurrences.
@dependencies cc_cortex.guards.base
@exports SiblingScanGuard

Solves the "fix A, skip B" anti-pattern where an agent fixes a bug in one
component but doesn't check other components with the same pattern. This
causes repeated deploy cycles and wasted tokens.

This is a PostToolUse QUALITY guard. It only injects context (ALLOW with
additionalContext), never DENY — it's a cognitive nudge, not a gate.
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
from typing import Optional

from cc_cortex.guards.base import (
    BaseGuard,
    GuardCategory,
    GuardContext,
    GuardResult,
)

# Windows: hide console window for subprocess calls
_STARTUPINFO = None
_CREATIONFLAGS = 0
if platform.system() == "Windows":
    _STARTUPINFO = subprocess.STARTUPINFO()
    _STARTUPINFO.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    _CREATIONFLAGS = subprocess.CREATE_NO_WINDOW

# Minimum old_string length to trigger scan (avoid noise from tiny edits)
_MIN_PATTERN_LEN = 15

# Maximum files to report (avoid flooding context)
_MAX_REPORT_FILES = 5

# Extensions to scan (only scan same-extension siblings)
_SCANNABLE_EXTENSIONS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".vue", ".svelte",
    ".css", ".scss", ".html", ".md", ".json", ".yaml", ".yml",
}

# Directories to skip during scan
_SKIP_DIRS = {
    "node_modules", ".git", "dist", "build", "__pycache__",
    ".cc_cortex_cache", ".next", ".nuxt", "coverage",
}


def _extract_core_pattern(old_string: str) -> str:
    """Extract the most distinctive line from old_string for grep.

    Strategy: pick the longest non-blank, non-import, non-comment line.
    This is what likely contains the actual bug pattern.
    """
    lines = [
        ln.strip() for ln in old_string.splitlines()
        if ln.strip()
        and not ln.strip().startswith("#")
        and not ln.strip().startswith("//")
        and not ln.strip().startswith("import ")
        and not ln.strip().startswith("from ")
        and not ln.strip().startswith("*")
        and len(ln.strip()) >= 8
    ]
    if not lines:
        return ""
    # Pick the longest line as the most distinctive
    return max(lines, key=len)


def _escape_for_grep(pattern: str) -> str:
    """Escape regex special chars for literal grep search."""
    return re.escape(pattern)


def _run_grep(pattern: str, directory: str, extension: str) -> list[str]:
    """Run ripgrep or grep to find pattern in sibling files.

    Returns list of matching file paths (deduplicated).
    """
    escaped = _escape_for_grep(pattern)

    # Try ripgrep first (faster, respects .gitignore)
    for cmd_name in ("rg", "grep"):
        try:
            if cmd_name == "rg":
                cmd = [
                    "rg", "--files-with-matches", "--no-messages",
                    "--glob", f"*{extension}",
                    "--fixed-strings", pattern,
                    directory,
                ]
            else:
                cmd = [
                    "grep", "-rl", "--include", f"*{extension}",
                    escaped, directory,
                ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=5,
                cwd=directory,
                startupinfo=_STARTUPINFO,
                creationflags=_CREATIONFLAGS,
            )
            if result.returncode <= 1:  # 0=found, 1=not found
                files = [
                    f.strip() for f in result.stdout.splitlines()
                    if f.strip()
                ]
                return files[:_MAX_REPORT_FILES + 5]  # Extra for filtering
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return []


class SiblingScanGuard(BaseGuard):
    """PostToolUse guard: scan for sibling occurrences of edited patterns.

    When an Edit tool modifies a pattern, this guard scans other files with
    the same extension for the same pattern. If found, it injects context
    so the agent checks those files too — preventing the "fix A, skip B"
    anti-pattern.

    This is context injection only (ALLOW), never DENY.
    """

    name = "sibling_scan"
    category = GuardCategory.QUALITY

    def check(self, ctx: GuardContext) -> Optional[GuardResult]:
        """PreToolUse — no-op."""
        return None

    def on_post_tool(self, ctx: GuardContext) -> Optional[GuardResult]:
        """PostToolUse — scan siblings after Edit."""
        if ctx.tool_name != "Edit":
            return None

        old_string = ctx.tool_input.get("old_string", "")
        file_path = ctx.tool_input.get("file_path", "")

        if not old_string or not file_path:
            return None

        if len(old_string.strip()) < _MIN_PATTERN_LEN:
            return None

        _, ext = os.path.splitext(file_path)
        if ext not in _SCANNABLE_EXTENSIONS:
            return None

        core_pattern = _extract_core_pattern(old_string)
        if not core_pattern or len(core_pattern) < _MIN_PATTERN_LEN:
            return None

        if not ctx.workspace:
            return None

        matches = _run_grep(core_pattern, ctx.workspace, ext)
        siblings = _filter_siblings(matches, file_path, ctx.workspace)

        if not siblings:
            return None

        return _build_result(siblings, len(matches))


def _filter_siblings(
    matches: list[str], edited_path: str, workspace: str,
) -> list[str]:
    """Filter grep matches: remove edited file itself and skip dirs."""
    norm_edited = os.path.normpath(edited_path).lower()
    result = []
    for m in matches:
        abs_m = os.path.join(workspace, m) if not os.path.isabs(m) else m
        if os.path.normpath(abs_m).lower() == norm_edited:
            continue
        parts = m.replace("\\", "/").split("/")
        if any(p in _SKIP_DIRS for p in parts):
            continue
        result.append(m)
    return result[:_MAX_REPORT_FILES]


def _build_result(siblings: list[str], total_matches: int) -> GuardResult:
    """Build context injection result for sibling matches."""
    file_list = "\n".join(f"  - {s}" for s in siblings)
    more = ""
    if total_matches > _MAX_REPORT_FILES + 1:
        more = f"\n  (and more — {total_matches - 1} total matches)"
    return GuardResult.allow(
        context=(
            f"⚠ Sibling pattern detected: the pattern you just fixed "
            f"also exists in {len(siblings)} other file(s):\n"
            f"{file_list}{more}\n"
            f"Check these files for the same issue before moving on."
        )
    )
