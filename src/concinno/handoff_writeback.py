"""concinno.handoff_writeback — Scheduled task report → handoff TODO writeback.

@module handoff_writeback
@responsibility When a scheduled task completes (success or failure), append a
    TODO/error entry to a per-task markdown file under
    ``<handoff_dir>/scheduled/`` so the next interactive session sees the
    output and can act on it.  Without this link the scheduled task is
    invisible to the user's session — a "白跑" (wasted run).
@dependencies stdlib only (pathlib, datetime, re, os, sys, typing)
@exports writeback_scheduled_report, _format_todo_entry, _resolve_handoff_file

Design constraints:
  - **Fail-open**: any I/O error writes a warning to stderr and returns None.
    The scheduler chain must never be interrupted by a writeback failure.
  - **Filename stable / content locale-aware**: filename is always ASCII
    ``scheduled_<task>_<YYYYMMDD>.md``; heading text follows the ``language``
    argument so the user reads in their own language.
  - **Idempotent within day**: same task + same date → entries are *appended*
    (one new ``## ⬜`` section per call).  Two calls on the same day produce
    two sections — no deduplication of content, no overwrite of prior entries.
  - **Truncation**: report bodies longer than ``max_summary_lines`` are cut
    and a ``... (truncated; see full log at <log_path>)`` marker is appended.
  - **Locale**: heading text is looked up via ``concinno.i18n.msg`` if
    available; falls back to English strings hardcoded below so the module
    works even when i18n is not importable.
  - **Session ID**: read from ``CONCINNO_SESSION_ID`` env var (best-effort).
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── i18n integration (best-effort, never raises) ─────────────────


def _i18n_heading(language: str, status: str) -> str:
    """Return the localised heading prefix for a TODO/error section.

    Falls back to English if i18n is unavailable or the key is missing.

    Args:
        language: BCP-47 / underscore locale string, e.g. ``"zh_TW"``.
        status:   ``"ok"`` for success, ``"error"`` for failure.

    Returns:
        A short string used as the ``## <icon> ...`` heading prefix.
    """
    _STATUS_ICON = {"ok": "⬜", "error": "⚠"}
    icon = _STATUS_ICON.get(status, "⬜")

    _FALLBACK_EN = {
        "ok": "Scheduled task output",
        "error": "Scheduled task ERROR",
    }

    try:
        # Temporarily set CC_UX_LANG so msg() picks up the right locale.
        old = os.environ.get("CC_UX_LANG")
        os.environ["CC_UX_LANG"] = language
        try:
            from concinno.i18n import msg as i18n_msg
            from concinno.i18n import reload as i18n_reload

            # Reload so env change takes effect within the same process.
            i18n_reload()
            key = (
                "handoff_writeback.heading_ok"
                if status == "ok"
                else "handoff_writeback.heading_error"
            )
            translated = i18n_msg(key)
            # If key not found, i18n returns the key itself — detect that.
            if translated == key:
                translated = _FALLBACK_EN.get(status, _FALLBACK_EN["ok"])
        finally:
            # Restore env state.
            if old is None:
                os.environ.pop("CC_UX_LANG", None)
            else:
                os.environ["CC_UX_LANG"] = old
    except Exception:
        translated = _FALLBACK_EN.get(status, _FALLBACK_EN["ok"])

    return f"{icon} {translated}"


# ── Internal helpers ──────────────────────────────────────────────


def _sanitize_task_name(task_name: str) -> str:
    """Return a filesystem-safe version of *task_name*.

    Keeps only ASCII word characters and hyphens; collapses runs of
    unsafe characters to a single underscore.

    Args:
        task_name: Raw task name (e.g. ``"self-reflection"``).

    Returns:
        Sanitized string safe for use in a filename.
    """
    safe = re.sub(r"[^\w\-]", "_", task_name, flags=re.ASCII)
    # Collapse consecutive underscores/hyphens to a single underscore.
    safe = re.sub(r"[_\-]{2,}", "_", safe)
    return safe.strip("_") or "task"


def _resolve_handoff_file(handoff_dir: Path, task_name: str, date_iso: str) -> Path:
    """Compute the canonical path for a scheduled-task writeback file.

    The ``scheduled/`` sub-directory keeps writeback files segregated from
    human-authored handoff documents.  Filename is always ASCII so it is
    stable across locale changes.

    Args:
        handoff_dir: Base handoff directory (e.g. ``_AI_BRAIN/06_Handoffs/<project>/``).
        task_name:   Task name; will be sanitized.
        date_iso:    ISO date string ``"YYYYMMDD"`` (compact, no dashes).

    Returns:
        Absolute ``Path`` to the target ``.md`` file (not yet created).

    Example:
        >>> _resolve_handoff_file(Path("/h"), "self-reflection", "20260421")
        PosixPath('/h/scheduled/scheduled_self-reflection_20260421.md')
    """
    safe = _sanitize_task_name(task_name)
    filename = f"scheduled_{safe}_{date_iso}.md"
    return handoff_dir / "scheduled" / filename


def _format_todo_entry(
    task_name: str,
    report: str,
    language: str = "en",
    status: str = "ok",
    max_summary_lines: int = 50,
    log_path: Optional[str] = None,
    session_id: Optional[str] = None,
    timestamp: Optional[str] = None,
    **_meta: object,
) -> str:
    """Render a single TODO/error markdown section.

    This is a **pure function** — it never touches the filesystem.  All
    file-writing logic lives in :func:`writeback_scheduled_report`.

    Args:
        task_name:         Human-readable task identifier.
        report:            Raw text output from the scheduled task.
        language:          Locale code used for the heading.
        status:            ``"ok"`` (success) or ``"error"`` (failure).
        max_summary_lines: Maximum body lines before truncation.
        log_path:          Optional path to full log for the truncation marker.
        session_id:        Optional session ID to embed in the heading.
        timestamp:         ISO-8601 timestamp string; defaults to now (UTC).

    Returns:
        A complete markdown section string starting with a ``## …`` heading.
    """
    if timestamp is None:
        timestamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M")

    heading_prefix = _i18n_heading(language, status)
    session_str = f"session={session_id}" if session_id else "session=unknown"
    heading = f"## {heading_prefix} — {task_name} — {timestamp} ({session_str})"

    # Split report into lines, truncate if needed.
    lines = report.splitlines() if report else []
    truncated = False
    if len(lines) > max_summary_lines:
        lines = lines[:max_summary_lines]
        truncated = True

    body = "\n".join(lines)

    if truncated:
        if log_path:
            body += f"\n\n... (truncated; see full log at {log_path})"
        else:
            body += "\n\n... (truncated)"

    return f"{heading}\n\n{body}\n"


# ── Public API ────────────────────────────────────────────────────

_FILE_HEADER_TEMPLATE = "# Scheduled Task Outputs — {task_name} — {date}\n\n"
"""Top-of-file header written once when the daily file is first created."""


def writeback_scheduled_report(
    task_name: str,
    report: str,
    handoff_dir: Path,
    language: str = "en",
    max_summary_lines: int = 50,
    log_path: Optional[str] = None,
    success: bool = True,
) -> Optional[Path]:
    """Append a TODO (or error) entry to the scheduled-task handoff file.

    Creates ``<handoff_dir>/scheduled/scheduled_<task>_<YYYYMMDD>.md`` on
    first call for the day; subsequent calls on the same day **append** a
    new section rather than overwriting.

    This function is **fail-open**: any filesystem error is printed to
    ``stderr`` and ``None`` is returned.  The caller (scheduler) must treat
    a ``None`` return as a non-fatal warning.

    Args:
        task_name:         Name of the scheduled task (e.g. ``"self-reflection"``).
        report:            Text output produced by the task (stdout/preview).
        handoff_dir:       Root of the project's handoff directory.
        language:          Locale code for heading text (e.g. ``"zh_TW"``).
        max_summary_lines: Body lines kept before truncation.  Default 50.
        log_path:          Path to the full rotated log; embedded in truncation marker.
        success:           ``True`` for a normal run, ``False`` for an error run.

    Returns:
        Path to the written file on success, ``None`` on any failure.

    Example:
        >>> p = writeback_scheduled_report("scavenger", "found 3 items", Path("/h"))
        >>> p.name.startswith("scheduled_scavenger_")
        True
    """
    status = "ok" if success else "error"
    session_id: Optional[str] = os.environ.get("CONCINNO_SESSION_ID")
    now = datetime.now(tz=timezone.utc)
    date_compact = now.strftime("%Y%m%d")
    date_display = now.strftime("%Y-%m-%d")
    timestamp = now.strftime("%Y-%m-%dT%H:%M")

    target = _resolve_handoff_file(handoff_dir, task_name, date_compact)

    try:
        # Ensure directory exists.
        target.parent.mkdir(parents=True, exist_ok=True)

        # Build the new entry.
        entry = _format_todo_entry(
            task_name=task_name,
            report=report,
            language=language,
            status=status,
            max_summary_lines=max_summary_lines,
            log_path=log_path,
            session_id=session_id,
            timestamp=timestamp,
        )

        if target.exists():
            # Append to existing file (idempotent append — new section each call).
            existing = target.read_text(encoding="utf-8")
            new_content = existing.rstrip("\n") + "\n\n" + entry
            target.write_text(new_content, encoding="utf-8")
        else:
            # Create new file with header + first entry.
            header = _FILE_HEADER_TEMPLATE.format(
                task_name=task_name, date=date_display
            )
            target.write_text(header + entry, encoding="utf-8")

        return target

    except OSError as exc:
        _warn(f"[concinno.handoff_writeback] write failed for {target}: {exc}")
        return None
    except Exception as exc:  # noqa: BLE001
        _warn(f"[concinno.handoff_writeback] unexpected error: {exc}")
        return None


# ── Internal utilities ────────────────────────────────────────────


def _warn(message: str) -> None:
    """Write *message* to stderr (best-effort — never raises)."""
    try:
        print(message, file=sys.stderr, flush=True)
    except Exception:
        pass
