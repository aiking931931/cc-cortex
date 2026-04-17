"""concinno.version_sync_guard — Edit-time enforcement that version bumps
stay aligned across ``pyproject.toml``, ``<pkg>/__init__.py``, and
``CHANGELOG.md``.

@module version_sync_guard
@responsibility Detect a bump to the version field in pyproject.toml or
    ``__init__.py`` at write time and cross-check against the latest
    non-``[Unreleased]`` heading in the nearest CHANGELOG.md. If the three
    sources would disagree after the edit lands, emit a step-back warning
    naming each drifting source so the agent can fix them in one pass.
@dependencies stdlib only (re, pathlib). concinno.guards.base for the
    BaseGuard interface.
@exports VersionSyncGuard
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from concinno.constants import WRITE_TOOLS_EXT
from concinno.guards.base import (
    BaseGuard,
    GuardCategory,
    GuardContext,
    GuardResult,
)

# ── Regex sources ────────────────────────────────────────────

# Matches ``version = "X.Y.Z"`` (pyproject.toml [project] table)
_PYPROJECT_VERSION = re.compile(r'^\s*version\s*=\s*"([^"]+)"', re.MULTILINE)

# Matches ``__version__ = "X.Y.Z"`` (package __init__.py)
_DUNDER_VERSION = re.compile(r'^\s*__version__\s*=\s*"([^"]+)"', re.MULTILINE)

# Matches Keep-a-Changelog H2 ``## [X.Y.Z] - YYYY-MM-DD`` or
# ``## [X.Y.Z]`` (date optional). ``[Unreleased]`` is explicitly
# skipped because it never carries a real version.
_CHANGELOG_HEADING = re.compile(
    r"^##\s*\[(?P<ver>(?!Unreleased\])[^\]]+)\]",
    re.MULTILINE,
)

_ENV_SKIP = "CONCINNO_SKIP_VERSION_GATE"

# Single source of truth: concinno.constants.WRITE_TOOLS_EXT
# ({"Edit","Write","NotebookEdit","MultiEdit"}). Previously this file
# hard-coded a subset that missed NotebookEdit — red-team F2 catch.
_WATCHED_TOOLS = WRITE_TOOLS_EXT


def _latest_changelog_version(changelog_path: Path) -> Optional[str]:
    """Return the first non-Unreleased H2 version in ``CHANGELOG.md``."""
    try:
        text = changelog_path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = _CHANGELOG_HEADING.search(text)
    return match.group("ver") if match else None


def _extract_version(pattern: re.Pattern[str], text: str) -> Optional[str]:
    match = pattern.search(text or "")
    return match.group(1) if match else None


def _find_sibling_changelog(edited: Path) -> Optional[Path]:
    """Walk up from the edited file to the nearest CHANGELOG.md.

    Example: ``.../projects/concinno/pyproject.toml`` →
    ``.../projects/concinno/CHANGELOG.md`` (walk stops at the first hit).
    """
    for parent in [edited.parent, *edited.parents]:
        candidate = parent / "CHANGELOG.md"
        if candidate.exists():
            return candidate
    return None


class VersionSyncGuard(BaseGuard):
    """PreToolUse guard that flags version drift across three sources."""

    name = "version_sync_guard"
    category = GuardCategory.QUALITY

    def check(self, ctx: GuardContext) -> GuardResult:
        if os.environ.get(_ENV_SKIP) == "1":
            # H2 red-team fix: record every escape use so backfill
            # sessions are audit-traceable (was "silent bypass").
            self._audit_escape(ctx)
            return GuardResult.allow()
        if ctx.tool_name not in _WATCHED_TOOLS:
            return GuardResult.allow()

        tool_input = ctx.tool_input or {}
        # Normalise file_path across Edit / Write / MultiEdit shapes
        file_path_raw = tool_input.get("file_path") or tool_input.get("path")
        if not file_path_raw:
            return GuardResult.allow()

        edited = Path(file_path_raw)
        name = edited.name.lower()
        if name not in ("pyproject.toml", "__init__.py"):
            return GuardResult.allow()

        # Pull the version string that would land after this edit.
        # Each write-capable tool stores the incoming content under a
        # different key; read the right one explicitly so new tools
        # don't silently fall through to a MultiEdit-shaped parse that
        # returns "" (which would bypass the guard entirely).
        if ctx.tool_name == "Edit":
            candidate_text = str(tool_input.get("new_string") or "")
        elif ctx.tool_name == "Write":
            candidate_text = str(tool_input.get("content") or "")
        elif ctx.tool_name == "MultiEdit":
            edits = tool_input.get("edits") or []
            candidate_text = "\n".join(
                str(e.get("new_string") or "") for e in edits if isinstance(e, dict)
            )
        elif ctx.tool_name == "NotebookEdit":
            # Jupyter cells store replacement text in ``new_source`` (or
            # legacy ``cell_source``). pyproject.toml / __init__.py are
            # not notebooks, so landing a version bump via NotebookEdit
            # is almost certainly a mistake — but we still read the cell
            # text so a drift is detected and surfaced to the agent.
            candidate_text = str(
                tool_input.get("new_source")
                or tool_input.get("cell_source")
                or ""
            )
        else:
            # Unknown write tool in WRITE_TOOLS_EXT — refuse to guess.
            return GuardResult.allow_advisory(
                context=(
                    f"[{self.name}] unhandled write tool {ctx.tool_name!r}; "
                    f"version gate cannot inspect this edit. Update "
                    f"version_sync_guard.py to add an explicit branch."
                )
            )

        pattern = _PYPROJECT_VERSION if name == "pyproject.toml" else _DUNDER_VERSION
        new_version = _extract_version(pattern, candidate_text)
        if new_version is None:
            # The edit does not touch the version line; nothing to cross-check.
            return GuardResult.allow()

        changelog = _find_sibling_changelog(edited)
        if changelog is None:
            return GuardResult.allow_advisory(
                context=f"[{self.name}] {name} version → {new_version!r} but no "
                f"sibling CHANGELOG.md was found to cross-check against. "
                f"Create one (Keep-a-Changelog format) before the release."
            )

        changelog_version = _latest_changelog_version(changelog)
        if changelog_version == new_version:
            return GuardResult.allow()

        # Drift detected — emit a step-back warning that names all three
        # sources so the fix is a single, visible multi-edit pass.
        msg = (
            f"[{self.name}] Version drift — {name} is being bumped to "
            f"{new_version!r} but {changelog.name} still tops out at "
            f"{changelog_version!r}. Before committing, align all three "
            f"sources: 1) pyproject.toml `version = '…'`, 2) package "
            f"__init__ `__version__ = '…'`, 3) CHANGELOG.md top-most "
            f"`## [X.Y.Z] - YYYY-MM-DD` entry. `test_version_sync` will "
            f"fail until they match; `CI publish.yml` also gates on it. "
            f"Env escape for intentional backfill: "
            f"{_ENV_SKIP}=1."
        )
        return GuardResult.allow_advisory(context=msg)

    @staticmethod
    def _audit_escape(ctx: GuardContext) -> None:
        """Append a JSONL record when CONCINNO_SKIP_VERSION_GATE=1 is used.

        Written under ``ctx.cache_dir`` if set, else the workspace
        ``_AI_BRAIN/logs/`` dir if it exists, else skipped silently
        (don't break the guard for logging failures).
        """
        import json
        import time

        record = {
            "ts": int(time.time()),
            "session_id": ctx.session_id,
            "tool_name": ctx.tool_name,
            "file_path": (ctx.tool_input or {}).get("file_path"),
            "reason": "CONCINNO_SKIP_VERSION_GATE=1",
        }
        candidates: list[Path] = []
        if ctx.cache_dir:
            candidates.append(Path(ctx.cache_dir) / "version_gate_skip.jsonl")
        if ctx.workspace:
            candidates.append(
                Path(ctx.workspace) / ".concinno" / "logs" / "version_gate_skip.jsonl"
            )
        # User-scope XDG cache fallback (library-neutral, not CC-specific).
        import os
        xdg = os.environ.get("XDG_CACHE_HOME")
        if xdg:
            candidates.append(Path(xdg) / "concinno" / "version_gate_skip.jsonl")
        else:
            candidates.append(
                Path.home() / ".cache" / "concinno" / "version_gate_skip.jsonl"
            )
        for path in candidates:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                return  # one successful write is enough
            except OSError:
                continue
        # All destinations failed — guard still returns allow, we just
        # couldn't audit. Do not raise.


__all__ = ["VersionSyncGuard"]
