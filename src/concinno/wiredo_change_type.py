"""Lightweight change_type classifier for the WIREDO three-tier loader.

@module: concinno.wiredo_change_type
@responsibility: Map a collection of recent file paths + Bash commands
    to one of the 16 WIREDO change_type categories, so the loader can
    pull the right L3 recipe. Heuristic-only — no LLM call, no file IO.
@dependencies: stdlib only.
@exports: detect_change_type, detect_from_path, detect_from_command

The classifier is deliberately simple and transparent. Callers hand over
a list of recent artifact paths and/or Bash commands; the detector
returns the most specific category that matches, preferring:

  delivery commands (build / deploy / migration) >
  visible asset types (image / audio / video / word_doc) >
  code files by extension/path markers >
  fallback ("library" for .py under src/, "other" otherwise)

This module does NOT read files — only inspects strings. It is safe to
call from hot paths and inside hooks.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import PurePosixPath
from typing import Iterable

# 16 change_types from the WIREDO routing table.
CHANGE_TYPES: tuple[str, ...] = (
    "frontend",
    "backend",
    "library",
    "hook",
    "migration",
    "deploy",
    "cli",
    "word_doc",
    "image",
    "audio",
    "video",
    "db_query",
    "ai_prompt",
    "build_artifact",
    "vscode_extension",
    "test_only",
    "docs_only",
)

# ── Extension → category (single-category assets) ──────────────────
_EXT_MAP: dict[str, str] = {
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".gif": "image",
    ".webp": "image",
    ".svg": "image",
    ".bmp": "image",
    ".mp3": "audio",
    ".wav": "audio",
    ".flac": "audio",
    ".ogg": "audio",
    ".m4a": "audio",
    ".mp4": "video",
    ".mov": "video",
    ".webm": "video",
    ".avi": "video",
    ".mkv": "video",
    ".docx": "word_doc",
    ".doc": "word_doc",
    ".md": "docs_only",
    ".rst": "docs_only",
    ".sql": "db_query",
    ".whl": "build_artifact",
    ".tar.gz": "build_artifact",
    ".tgz": "build_artifact",
    ".vsix": "vscode_extension",
}

# Path-segment markers for code categories.
_FRONTEND_MARKERS = re.compile(
    r"(^|/)(src/(pages|components|ui)|app/(web|ui)|frontend|public)/",
    re.IGNORECASE,
)
_BACKEND_MARKERS = re.compile(
    r"(^|/)(api|routes|handlers|endpoints|controllers|server)/",
    re.IGNORECASE,
)
_HOOK_MARKERS = re.compile(
    r"(^|/)(hooks|guards|on_pre_tool|on_post_tool|on_stop)\b|/cbua_|_guard\.py$",
    re.IGNORECASE,
)
_CLI_MARKERS = re.compile(
    r"(^|/)(cli|bin|scripts)/|(^|/)main\.py$|_cli\.py$",
    re.IGNORECASE,
)
_MIGRATION_MARKERS = re.compile(
    r"(^|/)(migrations|alembic|schema)/|_migration\.py$",
    re.IGNORECASE,
)
_AI_PROMPT_MARKERS = re.compile(
    r"(^|/)(prompts|skills|agents)/|SKILL\.md$|\.prompt\.md$|_prompt\.py$",
    re.IGNORECASE,
)
_TEST_MARKERS = re.compile(
    r"(^|/)(tests?|__tests__)/|(^|/)test_[^/]+\.py$|[^/]+_test\.py$|\.spec\.ts$",
    re.IGNORECASE,
)

# ── Bash command → category ────────────────────────────────────────
_DEPLOY_CMD = re.compile(
    r"\b(deploy\.py|kubectl\s+apply|rsync|scp|docker\s+push|helm\s+upgrade)\b",
    re.IGNORECASE,
)
_BUILD_CMD = re.compile(
    r"\b(python\s+-m\s+build|pip\s+wheel|twine\s+upload|docker\s+build|"
    r"cargo\s+build|npm\s+run\s+build)\b",
    re.IGNORECASE,
)
_VSCE_CMD = re.compile(
    r"\b(vsce\s+(package|publish)|@vscode/vsce\s+package)\b",
    re.IGNORECASE,
)
_MIGRATION_CMD = re.compile(
    r"\b(alembic\s+upgrade|alembic\s+downgrade|django-admin\s+migrate|"
    r"knex\s+migrate)\b",
    re.IGNORECASE,
)
_DB_CMD = re.compile(
    r"\b(psql|mysql|sqlite3|SELECT\s+|INSERT\s+INTO|UPDATE\s+\w+\s+SET|"
    r"DELETE\s+FROM)\b",
    re.IGNORECASE,
)


def detect_from_path(path: str) -> str | None:
    """Classify a single file path. Returns None if no rule matches.

    Resolution order (most specific first):
      1. test markers (any ext, path says "it's a test")
      2. path-segment markers (migration/hook/ai_prompt/cli/frontend/backend)
      3. compound extension (.tar.gz, .tgz)
      4. single extension (.png/.mp3/.docx/.md/.sql/...)
      5. Python fallback (.py under src/ → library)
    """
    if not path:
        return None
    # Normalize to forward slashes for consistent matching.
    norm = path.replace("\\", "/")
    posix = PurePosixPath(norm)
    lower = norm.lower()
    ext = posix.suffix.lower()

    # Visual/binary asset extensions (image, audio, video, word_doc)
    # take precedence over path markers so `public/logo.svg` is image,
    # not frontend.
    _ASSET_EXTS = {
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp",
        ".mp3", ".wav", ".flac", ".ogg", ".m4a",
        ".mp4", ".mov", ".webm", ".avi", ".mkv",
        ".docx", ".doc",
    }

    # 1. Test files always win over everything else.
    if _TEST_MARKERS.search(norm):
        return "test_only"

    # 2. Asset extensions win over path markers.
    if ext in _ASSET_EXTS:
        return _EXT_MAP[ext]

    # 3. Path-segment markers override generic extensions.
    # Migration dirs can hold .sql that should NOT be db_query.
    # SKILL.md inside skills/ dirs should be ai_prompt not docs_only.
    if _MIGRATION_MARKERS.search(norm):
        return "migration"
    if _HOOK_MARKERS.search(norm):
        return "hook"
    if _AI_PROMPT_MARKERS.search(norm):
        return "ai_prompt"
    if _CLI_MARKERS.search(norm):
        return "cli"
    if _FRONTEND_MARKERS.search(norm):
        return "frontend"
    if _BACKEND_MARKERS.search(norm):
        return "backend"

    # 4. Compound extensions (.tar.gz).
    for suffix, cat in _EXT_MAP.items():
        if suffix.count(".") > 1 and lower.endswith(suffix):
            return cat

    # 5. Single-segment extension lookup (remaining: .md/.sql/.whl/...).
    if ext in _EXT_MAP:
        return _EXT_MAP[ext]

    # 5. Python source under src/ falls back to library.
    if ext == ".py" and "/src/" in "/" + norm:
        return "library"
    return None


def detect_from_command(cmd: str) -> str | None:
    """Classify a Bash command string. Returns None if no rule matches."""
    if not cmd:
        return None
    if _DEPLOY_CMD.search(cmd):
        return "deploy"
    if _VSCE_CMD.search(cmd):
        return "vscode_extension"
    if _BUILD_CMD.search(cmd):
        return "build_artifact"
    if _MIGRATION_CMD.search(cmd):
        return "migration"
    if _DB_CMD.search(cmd):
        return "db_query"
    return None


def detect_change_type(
    *,
    paths: Iterable[str] = (),
    commands: Iterable[str] = (),
    default: str = "other",
) -> str:
    """Classify a collection of paths/commands into one change_type.

    Strategy:
      1. Commands take precedence when they name a delivery action
         (deploy / build / migration / db_query).
      2. Otherwise, tally path classifications and return the mode.
      3. If the tally ties or has no hits, fall back to `default`.
      4. `docs_only` only wins if ALL files classify as docs_only.
    """
    cmd_list = [c for c in commands if c]
    for cmd in cmd_list:
        hit = detect_from_command(cmd)
        if hit is not None:
            return hit

    path_list = [p for p in paths if p]
    if not path_list:
        return default

    classifications: list[str] = []
    for p in path_list:
        cls = detect_from_path(p)
        if cls is not None:
            classifications.append(cls)

    if not classifications:
        return default

    # docs_only is only picked if it's a clean sweep.
    if all(c == "docs_only" for c in classifications):
        return "docs_only"

    # Ignore docs_only in the mode tally otherwise (docs don't dominate
    # mixed sessions).
    non_docs = [c for c in classifications if c != "docs_only"]
    if not non_docs:
        return default

    counts = Counter(non_docs)
    top, top_count = counts.most_common(1)[0]
    # Tie-breaking: prefer the more specific (non-library) category.
    tied = [c for c, n in counts.items() if n == top_count]
    if len(tied) > 1 and "library" in tied:
        tied.remove("library")
        return tied[0]
    return top
