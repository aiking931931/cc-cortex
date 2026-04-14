"""cc_cortex.progress_reporter -- Milestone-triggered progress reporting.

@module progress_reporter
@responsibility Generate formatted progress reports from orchestrator + cost data
@dependencies cc_cortex.task_orchestrator, cc_cortex.cost_tracker
@exports ProgressReporter
"""

from __future__ import annotations

from cc_cortex.cost_tracker import CostTracker
from cc_cortex.task_orchestrator import TaskOrchestrator


class ProgressReporter:
    """Generate progress reports at milestones.

    Integrates TaskOrchestrator + CostTracker into formatted reports.
    """

    def __init__(
        self,
        orchestrator: TaskOrchestrator,
        cost_tracker: CostTracker,
    ) -> None:
        self._orch = orchestrator
        self._cost = cost_tracker

    def generate_report(self, tool_count: int = 0) -> str:
        """Generate a formatted progress report."""
        p = self._orch.progress()
        s = self._cost.stats()

        completed_labels = [
            t["label"] for t in p["subtasks"] if t.get("done")
        ]
        remaining_labels = [
            t["label"] for t in p["subtasks"] if not t.get("done")
        ]

        header = f"\U0001f4ca Progress Report (tool call #{tool_count})"
        sep = "\u2501" * 24
        goal_line = f"Goal: {p['goal'] or '(not set)'}"
        progress_line = (
            f"Progress: {p['completed']}/{p['total']} ({p['percent']}%)"
        )
        cost_line = (
            f"Cost: ${s['estimated_usd']:.3f} / "
            f"${s['budget_usd']:.2f} ({s['percent_used']}%)"
        )

        lines = [header, sep, goal_line, progress_line, cost_line, sep]

        if completed_labels:
            lines.append(
                "\u2705 Completed: " + ", ".join(completed_labels)
            )
        if remaining_labels:
            lines.append(
                "\u2b1c Remaining: " + ", ".join(remaining_labels)
            )

        alert = self._cost.alert_message()
        if alert:
            lines.append(f"\u26a0\ufe0f {alert}")

        return "\n".join(lines)

    def should_report(self, tool_count: int) -> bool:
        """Delegate to orchestrator.should_report()."""
        return self._orch.should_report(tool_count)
