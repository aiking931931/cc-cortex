"""cc_cortex.task_orchestrator -- Task decomposition and tracking.

@module task_orchestrator
@responsibility Track task decomposition and progress within a session
@dependencies cc_cortex.core.state_store
@exports TaskOrchestrator
"""

from __future__ import annotations

from cc_cortex.core.state_store import StateStore

_NAMESPACE = "task_orchestrator"


class TaskOrchestrator:
    """Track task decomposition and progress within a session.

    Not a guard -- a utility that other guards/hooks can query.
    Stores state in StateStore namespace 'task_orchestrator'.
    """

    def __init__(self, cache_dir: str, session_id: str) -> None:
        self._store = StateStore(cache_dir)
        self._session_id = session_id

    def _read(self) -> dict:
        return self._store.read(_NAMESPACE, self._session_id, default={})

    def _write(self, data: dict) -> None:
        self._store.write(_NAMESPACE, self._session_id, data)

    def set_goal(self, goal: str, subtasks: list[str] | None = None) -> None:
        """Set the session goal. Optionally decompose into subtasks."""
        items = []
        if subtasks:
            items = [{"label": s, "done": False} for s in subtasks]
        data = self._read()
        data["goal"] = goal
        data["subtasks"] = items
        self._write(data)

    def complete_subtask(self, index: int) -> None:
        """Mark a subtask as complete by index."""
        data = self._read()
        subtasks = data.get("subtasks", [])
        if 0 <= index < len(subtasks):
            subtasks[index]["done"] = True
            data["subtasks"] = subtasks
            self._write(data)

    def progress(self) -> dict:
        """Return progress summary.

        Returns:
            Dict with keys: goal, total, completed, remaining, percent, subtasks.
        """
        data = self._read()
        goal = data.get("goal", "")
        subtasks = data.get("subtasks", [])
        total = len(subtasks)
        completed = sum(1 for s in subtasks if s.get("done"))
        remaining = total - completed
        percent = round(completed / total * 100, 1) if total > 0 else 0.0
        return {
            "goal": goal,
            "total": total,
            "completed": completed,
            "remaining": remaining,
            "percent": percent,
            "subtasks": subtasks,
        }

    def milestone_report(self) -> str:
        """Generate a progress report string for injection."""
        p = self.progress()
        if not p["goal"]:
            return ""
        lines = [
            f"Goal: {p['goal']}",
            f"Progress: {p['completed']}/{p['total']} ({p['percent']}%)",
        ]
        for i, s in enumerate(p["subtasks"]):
            mark = "\u2705" if s["done"] else "\u2b1c"
            lines.append(f"  {mark} {i + 1}. {s['label']}")
        return "\n".join(lines)

    def should_report(self, tool_count: int, interval: int = 30) -> bool:
        """Whether it's time for a progress report (every N tools).

        Returns True when tool_count is a positive multiple of interval
        and there is an active goal.
        """
        if tool_count <= 0 or interval <= 0:
            return False
        if tool_count % interval != 0:
            return False
        return bool(self._read().get("goal"))
