"""concinno.equilibrium_guard — Write-and-clean for handoff files.

@module equilibrium_guard
@responsibility Enforce Equilibrium Rule: every handoff write triggers atomic cleanup.
    Recent records trimmed to KEEP_RECENT (3). Old session entries purged.
    Runs PostToolUse — file already written, guard reads->cleans->rewrites.
@dependencies concinno.guards.base, concinno.i18n
@exports EquilibriumGuard, cleanup_handoff, KEEP_RECENT
"""

from __future__ import annotations

import os
import re

from concinno.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

# ── Configuration ────────────────────────────────────────────────

KEEP_RECENT = 3  # Max recent session entries to keep


def _handoff_prefixes() -> tuple[str, ...]:
    """Get handoff file prefixes from all active locales."""
    from concinno.i18n import patterns
    result = patterns("handoff_prefixes")
    return tuple(result) if result else ("handoff_",)


# Pattern: top-level record line like "- 03-21a: **Title**"
# Supports both full-width and half-width colons
_RECORD_RE = re.compile(r"^- \d{2}-\d{2}[a-z]?[：:]")


def _recent_header_re() -> re.Pattern[str]:
    """Build section header regex from i18n patterns."""
    from concinno.i18n import patterns
    headers = patterns("recent_section_headers")
    alternatives = "|".join(re.escape(h) for h in headers) if headers else "recent|progress"
    return re.compile(rf"^###?\s*.*({alternatives})", re.IGNORECASE)

# Next section header (stops parsing recent records)
_NEXT_SECTION_RE = re.compile(r"^##\s+")


# ── Core cleanup ─────────────────────────────────────────────────


def _is_handoff_file(file_path: str) -> bool:
    """Check if file_path points to a handoff markdown file."""
    if not file_path:
        return False
    basename = os.path.basename(file_path)
    if not basename.endswith(".md"):
        return False
    return any(basename.startswith(p) for p in _handoff_prefixes())


def _find_recent_section(lines: list[str]) -> tuple[int, int]:
    """Find the start and end line indices of the recent records section.

    Returns (section_start, section_end) where section_start is the line
    AFTER the header, and section_end is the line BEFORE the next section.
    Returns (-1, -1) if not found.
    """
    start = -1
    for i, line in enumerate(lines):
        if _recent_header_re().match(line.strip()):
            start = i + 1
            # Skip blank lines after header
            while start < len(lines) and not lines[start].strip():
                start += 1
            break

    if start < 0:
        return -1, -1

    end = len(lines)
    for i in range(start, len(lines)):
        stripped = lines[i].strip()
        if _NEXT_SECTION_RE.match(stripped) and not _recent_header_re().match(stripped):
            end = i
            break

    return start, end


def _next_non_blank(lines: list[str], start: int, end: int) -> int:
    """Return index of next non-blank line in [start, end), or -1."""
    for j in range(start, end):
        if lines[j].strip():
            return j
    return -1


def _is_record_boundary(lines: list[str], blank_idx: int, end: int) -> bool:
    """Check if a blank line separates two records."""
    nxt = _next_non_blank(lines, blank_idx + 1, end)
    return nxt >= 0 and bool(_RECORD_RE.match(lines[nxt].strip()))


def _parse_records(lines: list[str], start: int, end: int) -> list[tuple[int, int]]:
    """Parse record boundaries within the recent section.

    Each record starts with a top-level "- DD-DDx：" line and includes
    all indented continuation lines (sub-items).

    Returns list of (record_start, record_end) tuples.
    """
    records: list[tuple[int, int]] = []
    current_start = -1

    for i in range(start, end):
        line = lines[i]
        if _RECORD_RE.match(line.strip()):
            if current_start >= 0:
                records.append((current_start, i))
            current_start = i
        elif not line.strip() and current_start >= 0:
            if _is_record_boundary(lines, i, end):
                records.append((current_start, i))
                current_start = -1

    if current_start >= 0:
        records.append((current_start, end))

    return records


def cleanup_handoff(content: str, keep: int = KEEP_RECENT) -> tuple[str, int]:
    """Clean a handoff file's recent records section.

    Args:
        content: Full file content.
        keep: Number of recent records to keep.

    Returns:
        (cleaned_content, removed_count). If removed_count == 0,
        content is returned unchanged (no rewrite needed).
    """
    lines = content.split("\n")
    start, end = _find_recent_section(lines)

    if start < 0:
        return content, 0

    records = _parse_records(lines, start, end)

    if len(records) <= keep:
        return content, 0

    # Keep the first `keep` records (newest are listed first)
    to_remove = records[keep:]
    removed_count = len(to_remove)

    # Calculate line range to remove
    remove_start = to_remove[0][0]
    remove_end = to_remove[-1][1]

    # Build cleaned content
    cleaned_lines = lines[:remove_start] + lines[remove_end:]

    # Remove trailing blank lines at end of section
    # (but keep at least one blank line before next section)
    result = "\n".join(cleaned_lines)
    return result, removed_count


# ── Atomic I/O ───────────────────────────────────────────────────


def _atomic_rewrite(file_path: str, content: str) -> None:
    """Write content to file_path via tmp+replace (atomic on same FS)."""
    tmp_path = file_path + f".tmp.{os.getpid()}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, file_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ── Guard ────────────────────────────────────────────────────────


class EquilibriumGuard(BaseGuard):
    """PostToolUse: write-and-clean for handoff files.

    Equilibrium Rule: every write = simultaneous cleanup. No cleanup = no write.
    Detects handoff file writes, trims old records, rewrites atomically.
    """

    name = "equilibrium_guard"
    category = GuardCategory.QUALITY

    def check(self, ctx: GuardContext) -> GuardResult | None:
        return None  # PostToolUse only

    @staticmethod
    def _extract_handoff_path(ctx: GuardContext) -> str:
        """Return file_path if ctx targets a handoff .md, else empty string."""
        if ctx.tool_name not in ("Write", "Edit"):
            return ""
        path = ctx.tool_input.get("file_path", "")
        if not _is_handoff_file(path):
            return ""
        if not os.path.isfile(path):
            return ""
        return path

    def on_post_tool(self, ctx: GuardContext) -> GuardResult | None:
        """After Write/Edit on handoff file → read, clean, rewrite."""
        file_path = self._extract_handoff_path(ctx)
        if not file_path:
            return None

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()

            cleaned, removed = cleanup_handoff(content)
            if removed == 0:
                return None

            _atomic_rewrite(file_path, cleaned)

            from concinno.i18n import msg
            return GuardResult.allow(
                context=msg(
                    "equilibrium_guard.cleanup",
                    file=os.path.basename(file_path),
                    count=removed,
                    keep=KEEP_RECENT,
                ),
            )

        except Exception:
            return None  # fail-open
