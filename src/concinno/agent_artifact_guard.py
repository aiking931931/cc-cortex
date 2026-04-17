"""concinno.agent_artifact_guard — PostToolUse verification of Agent artifacts.

@module agent_artifact_guard
@responsibility When a background/foreground Agent completes, extract claimed file
    paths from its result text, verify they exist on disk, and inject a manifest
    into additionalContext. If files are missing, inject a hard warning with
    the list of missing paths so the parent agent knows NOT to skip re-creation.
@dependencies concinno.guards.base
@exports AgentArtifactGuard

Problem this solves:
    Sub-agents report "done" with file paths in their result text, but the files
    may not actually exist (wrong cwd, path mismatch, Windows/Unix confusion).
    The parent agent then trusts the report and skips verification, leading to
    silent data loss and redundant rework when discovered later.

Design:
    PostToolUse on tool_name == "Agent":
    1. Extract file paths from agent result text (regex: absolute paths + relative)
    2. Check each path exists on disk
    3. Inject manifest: ✅ confirmed / ❌ missing
    4. If ANY missing → inject hard warning (not soft warn — per 軟警告負收益定律)
"""

from __future__ import annotations

import os
import re
from typing import Optional

from concinno.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

# ── Path extraction patterns ─────────────────────────────────

# Windows absolute: C:\project\... or C:/project/...
_WIN_ABS = re.compile(r'[A-Za-z]:[/\\][\w./\\-]+\.[\w]+')
# Unix absolute: /home/user/... /tmp/...
_UNIX_ABS = re.compile(r'(?<!\w)/[a-z][\w./\\-]+\.[\w]+')
# Relative from project: packages/foo/src/bar.ts, src/concinno/baz.py
_RELATIVE = re.compile(r'(?:packages|src|dist|aegis|docker)/[\w./\\-]+\.[\w]+')
# Backtick-quoted paths: `path/to/file.ts`
_BACKTICK = re.compile(r'`([\w./\\:-]+\.[\w]+)`')

# Excluded patterns (not real file paths)
_EXCLUDE = re.compile(r'(node_modules|\.git/|dist/index\.(js|d\.ts)$|__pycache__)')

# File extensions we care about
_CODE_EXTS = frozenset({
    '.ts', '.tsx', '.js', '.jsx', '.py', '.yml', '.yaml',
    '.json', '.md', '.toml', '.cfg', '.sh',
})


def extract_paths(text: str) -> list[str]:
    """Extract file paths from agent result text.

    Returns deduplicated list of candidate paths, preferring absolute forms.
    """
    paths: dict[str, str] = {}  # normalized → original

    for pattern in [_WIN_ABS, _UNIX_ABS, _BACKTICK, _RELATIVE]:
        for match in pattern.finditer(text):
            raw = match.group(1) if pattern == _BACKTICK else match.group(0)
            raw = raw.strip().rstrip('.,;:)')

            if _EXCLUDE.search(raw):
                continue

            ext = os.path.splitext(raw)[1].lower()
            if ext not in _CODE_EXTS:
                continue

            # Normalize for dedup
            norm = raw.replace('\\', '/').lower()
            if norm not in paths:
                paths[norm] = raw

    return list(paths.values())


def resolve_path(raw: str, workspace: str) -> str:
    """Resolve a raw path to an absolute path for existence check.

    Handles:
    - Already absolute (Windows or Unix)
    - /e/Cursor/... → E:\\Cursor\\... (Git Bash style)
    - Relative → workspace + relative
    """
    # Already absolute Windows
    if len(raw) >= 3 and raw[1] == ':':
        return os.path.normpath(raw)

    # Git Bash style: /e/Cursor/...
    if raw.startswith('/') and len(raw) >= 3 and raw[2] == '/':
        drive = raw[1].upper()
        return os.path.normpath(f"{drive}:{raw[2:]}")

    # Unix absolute but not Git Bash
    if raw.startswith('/'):
        return os.path.normpath(raw)

    # Relative
    if workspace:
        return os.path.normpath(os.path.join(workspace, raw))

    return os.path.normpath(raw)


def verify_artifacts(
    result_text: str,
    workspace: str,
) -> tuple[list[str], list[str]]:
    """Extract paths from agent result and verify existence.

    Returns:
        (confirmed, missing) — lists of absolute paths
    """
    raw_paths = extract_paths(result_text)
    confirmed: list[str] = []
    missing: list[str] = []

    for raw in raw_paths:
        resolved = resolve_path(raw, workspace)
        if os.path.isfile(resolved):
            confirmed.append(resolved)
        else:
            missing.append(f"{raw} → {resolved}")

    return confirmed, missing


def format_manifest(confirmed: list[str], missing: list[str]) -> str:
    """Format artifact verification manifest for additionalContext."""
    lines = []

    if missing:
        lines.append(f"⚠️ Agent Artifact Verification: {len(missing)} file(s) NOT FOUND on disk!")
        lines.append("Missing files (agent claimed to create but don't exist):")
        for p in missing:
            lines.append(f"  ❌ {p}")
        lines.append("Action: Re-create these files or verify paths before marking task complete.")
        if confirmed:
            lines.append(f"Confirmed ({len(confirmed)} files exist):")
            for p in confirmed[:5]:  # limit to avoid noise
                lines.append(f"  ✅ {os.path.basename(p)}")
            if len(confirmed) > 5:
                lines.append(f"  ... and {len(confirmed) - 5} more")
    elif confirmed:
        lines.append(f"✅ Agent artifacts verified: {len(confirmed)} file(s) confirmed on disk")
    # else: no paths detected, nothing to report

    return "\n".join(lines)


# ── Guard ──────────────────────────────────────────────────────


class AgentArtifactGuard(BaseGuard):
    """PostToolUse: verify Agent sub-agent artifacts exist on disk.

    When an Agent tool completes, extracts file paths from the result,
    checks each one exists, and injects a manifest. Missing files get
    a hard warning (not soft warn) so the parent agent knows to re-create.
    """

    name = "agent_artifact"
    category = GuardCategory.QUALITY
    step_back_reason = "agent artifacts missing"

    def check(self, ctx: GuardContext) -> Optional[GuardResult]:
        """PreToolUse: no-op. This guard only acts on PostToolUse."""
        return None

    def on_post_tool(self, ctx: GuardContext) -> Optional[GuardResult]:
        """PostToolUse: verify agent artifacts exist on disk."""
        if ctx.tool_name != "Agent":
            return None

        result_text = ctx.tool_result
        if not result_text:
            return None

        workspace = ctx.workspace
        confirmed, missing = verify_artifacts(result_text, workspace)

        # No paths detected — agent might not have written files
        if not confirmed and not missing:
            return None

        manifest = format_manifest(confirmed, missing)

        if missing:
            # Hard injection — parent MUST see this
            return GuardResult.allow(
                context=manifest,
                artifact_confirmed=len(confirmed),
                artifact_missing=len(missing),
            )

        # All confirmed — light touch
        return GuardResult.allow(
            context=manifest,
            artifact_confirmed=len(confirmed),
            artifact_missing=0,
        )
