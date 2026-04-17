"""concinno PostCompact hook — re-inject critical state after compaction.

Problem: Context compaction drops task progress, WIREDO status, and
unresolved issues. The model loses track of what it was doing.

Solution: On PostCompact, re-inject from persisted state:
  1. PreCompact snapshot (task + next_step + edited files)
  2. Current task list + progress
  3. WIREDO dimension status (if code session)
  4. Unresolved issues from delivery state
  5. Session identity (name, ID)

Snapshot lifecycle: PreCompact writes → PostCompact reads + injects +
deletes. Stale snapshots (>1h) are auto-cleaned.
"""

from __future__ import annotations

import json
import os
import sys


def _read_json(path: str) -> dict:
    """Read JSON file, return empty dict on failure."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _get_task_progress(cache_dir: str, session_id: str) -> str:
    """Read sentinel state for task/edit progress."""
    state_path = os.path.join(cache_dir, f"sentinel_{session_id}.json")
    state = _read_json(state_path)
    if not state:
        return ""

    edited = state.get("edited_files", [])
    calls = state.get("calls", [])
    tool_counts: dict[str, int] = {}
    for c in calls:
        t = c.get("tool", "unknown")
        tool_counts[t] = tool_counts.get(t, 0) + 1

    parts: list[str] = []
    if edited:
        parts.append(f"Files edited this session: {len(edited)}")
        for f in edited[-5:]:
            parts.append(f"  - {os.path.basename(f)}")
    if tool_counts:
        summary = ", ".join(f"{k}:{v}" for k, v in sorted(
            tool_counts.items(), key=lambda x: -x[1],
        )[:5])
        parts.append(f"Tool usage: {summary}")
    return "\n".join(parts)


def _get_wiredo_status(cache_dir: str, session_id: str) -> str:
    """Check if WIREDO dimensions have issues."""
    try:
        from concinno.delivery.wiredo import wiredo_full_check
        results = wiredo_full_check(cache_dir, session_id)
    except Exception:
        return ""

    if not results:
        return ""

    dim_names = {
        "W": "Wired", "I": "Inherited",
        "R": "Responsive", "E": "Extensible",
        "D": "Defended", "O": "Observable",
    }
    parts: list[str] = ["WIREDO status:"]
    for dim in "WIREDO":
        passed, lines = results.get(dim, (True, []))
        mark = "✅" if passed else "❌"
        parts.append(f"  {mark} {dim} — {dim_names.get(dim, dim)}")
    return "\n".join(parts)


def _get_delivery_state(cache_dir: str) -> str:
    """Read delivery state for unresolved issues."""
    state_path = os.path.join(cache_dir, "delivery_state.json")
    state = _read_json(state_path)
    if not state:
        return ""

    parts: list[str] = []
    for task_id, data in state.items():
        status = data.get("status", "unknown")
        desc = data.get("task", task_id)[:60]
        parts.append(f"  [{status}] {desc}")

    if parts:
        return "Active delivery gates:\n" + "\n".join(parts[:5])
    return ""


def _get_session_identity(project_dir: str, session_id: str) -> str:
    """Get session name from instance lock."""
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
            task = s.get("task", "")
            return f"Session: {name} | Task: {task[:50]}"
    return f"Session ID: {session_id[:12]}" if session_id else ""


def _is_persona_mode(project_dir: str) -> bool:
    """Check if persona_mode is enabled in cc_config."""
    try:
        cfg_path = os.path.join(project_dir, ".claude", "hooks", "cc_config.json")
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg.get("token_guard", {}).get("persona_mode", False)
    except Exception:
        return False


def _get_persona_state(cache_dir: str, session_id: str) -> str:
    """Read persona state for re-injection after compact.

    Persona sessions need character context restored, not task lists.
    """
    # Read persona state saved by PreCompact or session_start
    state_path = os.path.join(cache_dir, f"persona_{session_id}.json")
    state = _read_json(state_path)
    if not state:
        # Fallback: try generic persona state
        state_path = os.path.join(cache_dir, "persona_state.json")
        state = _read_json(state_path)
    if not state:
        return ""

    parts: list[str] = [
        "🔄 角色狀態已恢復（壓縮後自動重注入）：",
    ]
    if state.get("character"):
        parts.append(f"角色：{state['character']}")
    if state.get("personality"):
        parts.append(f"性格：{state['personality']}")
    if state.get("emotional_state"):
        parts.append(f"當前情緒：{state['emotional_state']}")
    if state.get("conversation_topic"):
        parts.append(f"話題：{state['conversation_topic']}")
    if state.get("relationship"):
        parts.append(f"關係：{state['relationship']}")
    if state.get("custom_context"):
        parts.append(state["custom_context"])

    return "\n".join(parts) if len(parts) > 1 else ""


def _read_and_clean_snapshot(cache_dir: str) -> dict:
    """Read PreCompact snapshot and delete it (one-shot consumption).

    Also cleans stale snapshots older than 1 hour.
    """
    from concinno.hooks.on_pre_compact import SNAPSHOT_FILENAME
    path = os.path.join(cache_dir, SNAPSHOT_FILENAME)
    snapshot: dict = {}

    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                snapshot = json.load(f)
            # Clean: delete after reading
            os.remove(path)
    except Exception:
        pass

    # Clean stale snapshots (>1h) in case PreCompact ran but PostCompact didn't
    try:
        import time
        for entry in os.scandir(cache_dir):
            if not entry.name.startswith("compact_snapshot"):
                continue
            if time.time() - entry.stat().st_mtime > 3600:
                os.remove(entry.path)
    except Exception:
        pass

    return snapshot


def _format_snapshot(snapshot: dict) -> str:
    """Format snapshot dict into injection text (≤3K tokens)."""
    if not snapshot:
        return ""

    parts: list[str] = ["📸 壓縮前快照："]

    task = snapshot.get("task", "")
    if task:
        parts.append(f"任務：{task}")

    name = snapshot.get("session_name", "")
    if name:
        parts.append(f"Session：{name}")

    project = snapshot.get("project", "")
    if project:
        parts.append(f"專案：{project}")

    edited = snapshot.get("edited_files", [])
    if edited:
        files = ", ".join(edited[-5:])
        parts.append(f"已編輯：{files}")

    calls = snapshot.get("tool_calls", 0)
    if calls:
        parts.append(f"工具呼叫：{calls} 次")

    count = snapshot.get("compact_count", 0)
    tokens = snapshot.get("tokens_before", 0)
    if count or tokens:
        parts.append(f"壓縮次數：C{count} | 壓縮前：{tokens // 1000}K")

    next_step = snapshot.get("next_step", "")
    if next_step:
        # Trim to keep under budget
        trimmed = next_step[:800]
        parts.append(f"\n{trimmed}")

    return "\n".join(parts)


def _build_context(
    project_dir: str, cache_dir: str, session_id: str,
) -> str:
    """Build re-injection context after compaction.

    Priority: snapshot (PreCompact) > sentinel state > WIREDO > delivery.
    Persona mode: inject character state only.
    """
    # Persona mode: character state only (preserve immersion)
    if _is_persona_mode(project_dir):
        persona_ctx = _get_persona_state(cache_dir, session_id)
        if persona_ctx:
            return persona_ctx
        identity = _get_session_identity(project_dir, session_id)
        if identity:
            return f"🔄 Session resumed after compaction.\n{identity}"
        return ""

    # Normal mode: snapshot + state re-injection
    parts: list[str] = [
        "🔄 壓縮後上下文恢復（關鍵狀態已還原）：",
    ]

    # 1. PreCompact snapshot (highest priority — structured checkpoint)
    snapshot = _read_and_clean_snapshot(cache_dir)
    snapshot_text = _format_snapshot(snapshot)
    if snapshot_text:
        parts.append(snapshot_text)

    # 2. Session identity
    identity = _get_session_identity(project_dir, session_id)
    if identity:
        parts.append(identity)

    # 3. Task progress from sentinel
    progress = _get_task_progress(cache_dir, session_id)
    if progress:
        parts.append(progress)

    # 4. WIREDO status
    wiredo = _get_wiredo_status(cache_dir, session_id)
    if wiredo:
        parts.append(wiredo)

    # 5. Delivery state
    delivery = _get_delivery_state(cache_dir)
    if delivery:
        parts.append(delivery)

    if len(parts) <= 1:
        return ""

    return "\n".join(parts)


def main(hook_data: dict | None = None) -> None:
    """PostCompact entry point."""
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

    context = _build_context(project_dir, cache_dir, session_id)
    if not context:
        return

    output = json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostCompact",
            "additionalContext": context,
        }
    }, ensure_ascii=False)

    if hasattr(sys.stdout, "buffer"):
        sys.stdout.buffer.write(output.encode("utf-8"))
        sys.stdout.buffer.flush()
    else:
        sys.stdout.write(output)
        sys.stdout.flush()


if __name__ == "__main__":
    main()
