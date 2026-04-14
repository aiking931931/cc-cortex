"""cc_cortex.session_format — Session ID format enforcement + task-pool ABCD validation.

@module session_format
@responsibility Validate session ID formats in handoff/task-pool files and enforce
    ABCD parallel dispatch requires a task-pool.md in the same directory.
@dependencies cc_cortex.constants
@exports check_session_id, check_taskpool_required

Usage:
    from cc_cortex.session_format import check_session_id, check_taskpool_required
    error = check_session_id(tool_name, tool_input, prefixes, handoff_prefixes)
    error = check_taskpool_required(tool_name, tool_input, handoff_prefixes)
"""

import os
import re
from typing import Optional

from cc_cortex.constants import WRITE_TOOLS
from cc_cortex.i18n import msg as i18n_msg
from cc_cortex.i18n import patterns as i18n_patterns

# Files that need session ID checking
SESSION_CHECK_BASENAMES = frozenset([
    "task-pool.md", "L0-signals.md", "L1-findings.md",
])


def _build_session_patterns(prefixes: str):
    """Build strict and loose session ID patterns from prefix string."""
    strict = re.compile(rf"(?:{prefixes})_[a-f0-9]{{4}}_\d{{4}}")
    loose = re.compile(rf"(?:{prefixes})_[A-Za-z0-9_]+")
    return strict, loose


_ABCD_PATTERN: re.Pattern | None = None


def _get_abcd_pattern() -> re.Pattern:
    """Build ABCD dispatch detection pattern from i18n."""
    global _ABCD_PATTERN
    if _ABCD_PATTERN is None:
        parts = list(i18n_patterns("session_format.abcd_patterns"))
        # Always include universal markers
        parts.extend([r"Phase-[A-D]", r"\b[A-D]\s*\("])
        _ABCD_PATTERN = re.compile("|".join(parts), re.IGNORECASE)
    return _ABCD_PATTERN


def check_session_id(
    tool_name: str,
    tool_input: dict,
    prefixes: str = "CC|IA|PS|TR|BK|EV|AQ|SC|TK|GN",
    handoff_prefixes: tuple = (),
    check_basenames: frozenset = SESSION_CHECK_BASENAMES,
) -> Optional[str]:
    """Check if session IDs in written content match the expected format.

    Returns error message string if invalid, None if OK.
    """
    if tool_name not in WRITE_TOOLS:
        return None
    if not isinstance(tool_input, dict):
        return None

    file_path = (
        tool_input.get("file_path")
        or tool_input.get("path")
        or tool_input.get("notebook_path")
        or ""
    )
    basename = os.path.basename(file_path)

    needs_check = (
        basename in check_basenames
        or any(basename.startswith(p) for p in handoff_prefixes)
    )
    if not needs_check:
        return None

    # Get content being written
    if tool_name == "Edit":
        content = tool_input.get("new_string", "")
    elif tool_name == "Write":
        content = tool_input.get("content", "")
    else:
        return None

    if not content:
        return None

    strict, loose = _build_session_patterns(prefixes)

    all_matches = loose.findall(content)
    if not all_matches:
        return None

    bad = [m for m in all_matches if not strict.fullmatch(m)]
    if not bad:
        return None

    bad_list = ", ".join(set(bad))
    return i18n_msg(
        "session_format.bad_id",
        bad_list=bad_list,
        prefixes=prefixes,
    )


def check_taskpool_required(
    tool_name: str,
    tool_input: dict,
    handoff_prefixes: tuple = (),
) -> Optional[str]:
    """Check if ABCD dispatch in handoff files has a corresponding task-pool.md.

    Returns error message string if task-pool is missing, None if OK.
    """
    if tool_name not in WRITE_TOOLS:
        return None
    if not isinstance(tool_input, dict):
        return None

    file_path = tool_input.get("file_path") or tool_input.get("path") or ""
    basename = os.path.basename(file_path)

    # Only check handoff files (not task-pool itself)
    if not any(basename.startswith(p) for p in handoff_prefixes):
        return None
    if basename == "task-pool.md":
        return None

    # Get content being written
    if tool_name == "Edit":
        content = tool_input.get("new_string", "")
    elif tool_name == "Write":
        content = tool_input.get("content", "")
    else:
        return None

    if not content:
        return None

    # Check for ABCD dispatch pattern
    if not _get_abcd_pattern().search(content):
        return None

    # ABCD pattern found — check for task-pool.md in same directory
    parent_dir = os.path.dirname(file_path)
    if not parent_dir:
        return None

    taskpool_path = os.path.join(parent_dir, "task-pool.md")
    if os.path.isfile(taskpool_path):
        return None

    return i18n_msg(
        "session_format.missing_taskpool",
        basename=basename,
        taskpool_path=taskpool_path,
    )
