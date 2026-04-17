"""concinno.core.path_utils — Unified path extraction, normalization, and transcript lookup.

@module path_utils
@responsibility Extract file paths from tool_input; normalize to
    lowercase forward-slash form; locate Claude Code transcript JSONL files
@dependencies (none — stdlib only)
@exports extract_file_path, normalize_path, find_transcript, find_latest_transcript
"""

from __future__ import annotations

import os


def extract_file_path(tool_input: dict | None) -> str:
    """Extract file path from a Claude Code tool_input dict.

    Checks ``file_path``, ``path``, and ``notebook_path`` keys in order.

    Args:
        tool_input: The tool input dictionary (may be None).

    Returns:
        File path string, or ``""`` if not found.
    """
    if not isinstance(tool_input, dict):
        return ""
    return (
        tool_input.get("file_path")
        or tool_input.get("path")
        or tool_input.get("notebook_path")
        or ""
    )


def normalize_path(path: str) -> str:
    """Normalize a file path for cross-platform comparison.

    Applies ``os.path.normpath``, replaces backslashes with forward slashes,
    and lowercases the result.

    Args:
        path: Raw file path.

    Returns:
        Normalized, lowercase, forward-slash path.
    """
    if not path:
        return ""
    return os.path.normpath(path).replace("\\", "/").lower()


# ── Transcript Lookup ────────────────────────────────────────────


_CACHE_FILENAME = "transcript_path.txt"


def _projects_root() -> str:
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return os.path.join(home, ".claude", "projects")


def _cache_path() -> str:
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if not project_dir:
        return ""
    return os.path.join(project_dir, ".concinno_cache", _CACHE_FILENAME)


def _write_cache(path: str) -> None:
    cache = _cache_path()
    if not cache:
        return
    try:
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        with open(cache, "w", encoding="utf-8") as f:
            f.write(path)
    except Exception:
        pass


def find_transcript(session_id: str) -> str:
    """Locate the transcript JSONL for a Claude Code session.

    Strategy (fast path first):
      1. Cache hit (~0ms)
      2. Project slug candidates (basename + full-path slug)
      3. Walk all project dirs (exact match)
      4. Partial match fallback (session_id[:8])

    Results are cached to ``<project>/.concinno_cache/transcript_path.txt``.
    """
    if not session_id:
        return ""

    # 1. Cache (must match current session_id — guard against stale cross-session cache)
    cache = _cache_path()
    if cache and os.path.isfile(cache):
        try:
            cached = open(cache, "r", encoding="utf-8").read().strip()
            if (
                cached
                and os.path.isfile(cached)
                and session_id in os.path.basename(cached)
            ):
                return cached
        except Exception:
            pass

    root = _projects_root()
    if not os.path.isdir(root):
        return ""

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")

    # 2. Slug candidates (no walk — fast)
    if project_dir:
        basename_slug = os.path.basename(project_dir).replace("\\", "-").replace(":", "-")
        full_slug = (
            project_dir.replace("\\", "-").replace(":", "-")
            .replace("/", "-").lstrip("-")
        )
        for slug in (basename_slug, full_slug):
            c = os.path.join(root, slug, f"{session_id}.jsonl")
            if os.path.isfile(c):
                _write_cache(c)
                return c

    # 3. Walk — exact match
    try:
        for d in os.listdir(root):
            subdir = os.path.join(root, d)
            if not os.path.isdir(subdir):
                continue
            exact = os.path.join(subdir, f"{session_id}.jsonl")
            if os.path.isfile(exact):
                _write_cache(exact)
                return exact
    except OSError:
        pass

    # 4. Partial match (first 8 chars of session_id)
    prefix = session_id[:8]
    try:
        for d in os.listdir(root):
            subdir = os.path.join(root, d)
            if not os.path.isdir(subdir):
                continue
            try:
                for f in os.listdir(subdir):
                    if f.endswith(".jsonl") and prefix in f:
                        hit = os.path.join(subdir, f)
                        _write_cache(hit)
                        return hit
            except OSError:
                continue
    except OSError:
        pass

    return ""


def find_latest_transcript() -> str:
    """Find the most recent transcript JSONL for the current project.

    Tries project-slug directories first, then falls back to
    the most recently modified ``.jsonl`` across all project dirs.
    """
    import glob as _glob

    root = _projects_root()
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", ".")

    # Try project-slug directory first
    for slug in (
        project_dir.replace("\\", "-").replace(":", "-")
        .replace("/", "-").lstrip("-"),
        os.path.basename(project_dir).replace("\\", "-").replace(":", "-"),
    ):
        pat = os.path.join(root, slug, "*.jsonl")
        files = _glob.glob(pat)
        if files:
            return max(files, key=os.path.getmtime)

    # Fallback: most recent across all project dirs
    pat = os.path.join(root, "*", "*.jsonl")
    files = _glob.glob(pat)
    if files:
        return max(files, key=os.path.getmtime)
    return ""
