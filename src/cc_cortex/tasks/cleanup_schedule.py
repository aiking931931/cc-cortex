"""cc_cortex.tasks.cleanup_schedule — Daily cleanup task.

@module cleanup_schedule
@responsibility Register a daily cleanup task that archives dead handoffs,
    squashes old auto-commits, and cleans stale temp files.
@dependencies cc_cortex.cleanup (run_cleanup)
@exports TASK_CONFIG, run_cleanup_task
"""

from __future__ import annotations

import os

from cc_cortex.cleanup import run_cleanup

# ── Task configuration ──────────────────────────

TASK_CONFIG = {
    "name": "cleanup",
    "prompt_file": "cleanup-prompt.txt",
    "model": "claude-sonnet-4-6",
    "log_name": "cleanup.log",
    "allowed_tools": "Read,Glob,Grep,Bash",
    "max_budget_usd": "0.30",
    "timeout_sec": 300,
    "min_interval_hours": 24,  # 1440 min
}


def run_cleanup_task(
    repo_dir: str | None = None,
    handoff_dir: str | None = None,
    log_dir: str | None = None,
    dry_run: bool = False,
) -> list[str]:
    """Run cleanup operations and return summary lines.

    Calls the existing ``cc_cortex.cleanup.run_cleanup`` with sensible
    defaults. This function is the bridge between the scheduler task
    definition and the actual cleanup logic.
    """
    repo = repo_dir or os.environ.get("CLAUDE_PROJECT_DIR", ".")
    results = run_cleanup(
        repo_dir=repo,
        handoff_dir=handoff_dir or "",
        log_dir=log_dir or "",
        dry_run=dry_run,
        squash_git=True,
        aggressive_gc=False,
    )
    lines: list[str] = []
    for r in results:
        status = "FAIL" if r.error else "OK"
        summary = r.error or f"{r.items_cleaned}/{r.items_found} cleaned"
        lines.append(f"[{status}] {r.action}: {summary}")
    return lines
