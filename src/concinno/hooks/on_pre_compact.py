"""concinno PreCompact hook — write snapshot before CC compaction.

Problem: CC auto-compact is a black box that may drop critical context.
Solution: Write a structured snapshot (≤3K tokens) before compaction.
PostCompact hook reads this snapshot and re-injects via additionalContext.

Snapshot contains: session identity + current task + pending items +
unresolved issues + next_step. This is NOT a full handoff — it's a
minimal checkpoint for context recovery.
"""

from __future__ import annotations

import json
import os
import sys
import time

SNAPSHOT_FILENAME = "compact_snapshot.json"


def _read_json(path: str) -> dict:
    """Read JSON file, return empty dict on failure."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _read_text(path: str, max_bytes: int = 8000) -> str:
    """Read text file with size limit."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read(max_bytes)
    except Exception:
        return ""


def _get_session_info(
    project_dir: str,
    session_id: str,
) -> dict:
    """Get session name and task from instance lock."""
    from concinno.core.config import get_config
    brain_dir = get_config().brain_dir
    lock_path = os.path.join(
        project_dir, brain_dir, "cognition_shared",
        "instance_lock.json",
    )
    lock = _read_json(lock_path)
    sessions = lock.get("sessions", {})
    for name, s in sessions.items():
        if s.get("session_id", "") == session_id:
            return {
                "session_name": name,
                "task": s.get("task", ""),
                "project": s.get("project", ""),
            }
    return {"session_name": "", "task": "", "project": ""}


def _find_handoff_files(project_dir: str) -> list[str]:
    """Find all handoff markdown files."""
    from concinno.core.config import get_config
    brain_dir = get_config().brain_dir
    handoff_dir = os.path.join(
        project_dir, brain_dir, "06_Handoffs",
    )
    if not os.path.isdir(handoff_dir):
        return []
    paths: list[str] = []
    for direntry in os.scandir(handoff_dir):
        if not direntry.is_dir():
            continue
        for f in os.scandir(direntry.path):
            if f.name.startswith("交接_") and f.name.endswith(".md"):
                paths.append(f.path)
    return paths


def _extract_next_step(content: str) -> str:
    """Extract ## next_step section from handoff content."""
    if "## next_step" not in content:
        return ""
    idx = content.index("## next_step")
    section = content[idx:idx + 2000]
    lines = section.split("\n")
    result: list[str] = []
    for i, line in enumerate(lines):
        if i > 0 and line.startswith("## "):
            break
        result.append(line)
    return "\n".join(result[:30])


def _get_active_handoff(project_dir: str, project: str) -> str:
    """Read the active handoff file's next_step section."""
    if not project:
        return ""
    for path in _find_handoff_files(project_dir):
        content = _read_text(path, max_bytes=4000)
        extracted = _extract_next_step(content)
        if extracted:
            return extracted
    return ""


def _get_sentinel_state(
    cache_dir: str,
    session_id: str,
) -> dict:
    """Read sentinel state for edited files and tool usage."""
    path = os.path.join(cache_dir, f"sentinel_{session_id}.json")
    state = _read_json(path)
    if not state:
        return {}
    return {
        "edited_files": [
            os.path.basename(f)
            for f in state.get("edited_files", [])[-10:]
        ],
        "tool_calls": len(state.get("calls", [])),
    }


def _build_snapshot(
    project_dir: str,
    cache_dir: str,
    session_id: str,
) -> dict:
    """Build compact snapshot (target ≤3K tokens).

    Structure mirrors handoff format but minimal:
    - identity: who am I, what am I doing
    - progress: what's done, what's pending
    - unresolved: what's blocked
    - next_step: what to do next
    """
    info = _get_session_info(project_dir, session_id)
    sentinel = _get_sentinel_state(cache_dir, session_id)

    snapshot: dict = {
        "ts": int(time.time()),
        "session_id": session_id[:12],
        "session_name": info.get("session_name", ""),
        "task": info.get("task", ""),
        "project": info.get("project", ""),
        "edited_files": sentinel.get("edited_files", []),
        "tool_calls": sentinel.get("tool_calls", 0),
    }

    # Get handoff next_step if available
    next_step = _get_active_handoff(
        project_dir, info.get("project", ""),
    )
    if next_step:
        snapshot["next_step"] = next_step[:1500]

    # Read zone data for compact count
    zone_file = os.path.join(
        os.path.expanduser("~"), ".claude", ".token_zone.json",
    )
    zone = _read_json(zone_file)
    if zone:
        snapshot["compact_count"] = zone.get("compact_count", 0)
        snapshot["tokens_before"] = zone.get("input_tokens", 0)

    return snapshot


def _save_snapshot(cache_dir: str, snapshot: dict) -> str:
    """Save snapshot to cache dir. Returns path."""
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, SNAPSHOT_FILENAME)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    return path


def _incremental_rag_index(project_dir: str) -> int:
    """Index learnings/discoveries to RAG before compact.

    Returns number of files indexed, 0 on skip/error.
    """
    try:
        from concinno.rag import RAGIndex
    except ImportError:
        return 0

    from concinno.core.config import get_config
    brain_dir = get_config().brain_dir

    targets = [
        os.path.join(brain_dir, "01_Memory", "evolution", "learnings.json"),
        os.path.join(brain_dir, "01_Memory", "evolution", "pending_discoveries.md"),
        os.path.join(brain_dir, "01_Memory", "evolution", "research_log.md"),
    ]

    indexed = 0
    try:
        idx = RAGIndex(project_dir=project_dir)
        for rel in targets:
            full = os.path.join(project_dir, rel)
            if os.path.isfile(full):
                idx.update(full)
                indexed += 1
    except Exception:
        pass
    return indexed


def main(hook_data: dict | None = None) -> None:
    """PreCompact entry point.

    Writes snapshot to .concinno_cache/compact_snapshot.json.
    Also increments compact count in zone file.
    Also indexes learnings to RAG (incremental).
    """
    try:
        if hook_data is None:
            hook_data = json.loads(sys.stdin.read())
    except Exception:
        return

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if not project_dir:
        return

    session_id = hook_data.get("session_id", "")
    cache_dir = os.path.join(project_dir, ".concinno_cache")

    try:
        snapshot = _build_snapshot(project_dir, cache_dir, session_id)
        _save_snapshot(cache_dir, snapshot)
    except Exception:
        pass

    # Incremental RAG index before context is lost
    try:
        _incremental_rag_index(project_dir)
    except Exception:
        pass

    # Increment compact count in zone file
    try:
        from concinno.token_zone import increment_compact_count
        increment_compact_count()
    except Exception:
        # Fallback: manual increment
        zone_file = os.path.join(
            os.path.expanduser("~"), ".claude", ".token_zone.json",
        )
        try:
            with open(zone_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["compact_count"] = data.get("compact_count", 0) + 1
            with open(zone_file, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception:
            pass


if __name__ == "__main__":
    main()
