"""cc_cortex.agent_supervisor — Supervised subagent execution framework.

@module agent_supervisor
@responsibility Define expected outputs before spawning subagents, verify
    results after completion. Breaks the CC L1 limitation (subagent black box)
    by establishing a contract between parent and child agents.
@dependencies cc_cortex.core.state_store
@exports SupervisedTask, AgentSupervisor, verify_task

Design principle (CC/CCC boundary):
  CC spawns subagents blindly (L1 limitation: no prompt visibility).
  CCC adds a supervision layer: parent defines checkpoints BEFORE spawn,
  SubagentStop hook verifies AFTER completion. If verification fails,
  parent gets actionable context (not just "file missing").

Three-level verification:
  L1 Existence: files exist on disk (already in on_subagent_stop.py)
  L2 Content:   files contain expected patterns/keywords
  L3 Logic:     output satisfies semantic constraints (future: LLM judge)

Usage::

    from cc_cortex.agent_supervisor import AgentSupervisor, SupervisedTask

    sup = AgentSupervisor(cache_dir="/path/to/cache")

    # Before spawning subagent
    task = SupervisedTask(
        agent_id="research-gemma4",
        expected_files=["research_report.md"],
        expected_patterns=["Gemma 4", "VRAM", "benchmark"],
        expected_keywords=["Apache 2.0"],
    )
    sup.register(task)

    # After subagent completes (called by SubagentStop hook)
    result = sup.verify("research-gemma4", workspace="/path")
    # result.passed = True/False
    # result.failures = ["Missing keyword: Apache 2.0"]
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional

from cc_cortex.core.state_store import StateStore

_NS = "agent_supervisor"


@dataclass
class SupervisedTask:
    """Contract for a subagent task.

    Defines what the subagent should produce. Registered before spawn,
    verified after completion.
    """

    agent_id: str
    description: str = ""

    # L1: Expected files (relative to workspace)
    expected_files: list[str] = field(default_factory=list)

    # L2: Expected patterns in output (regex)
    expected_patterns: list[str] = field(default_factory=list)

    # L2: Expected keywords in output (literal match)
    expected_keywords: list[str] = field(default_factory=list)

    # L2: Minimum output length (characters)
    min_output_length: int = 0

    # L3: Semantic constraints (future: LLM-as-judge)
    semantic_constraints: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialize for state store."""
        return {
            "agent_id": self.agent_id,
            "description": self.description,
            "expected_files": self.expected_files,
            "expected_patterns": self.expected_patterns,
            "expected_keywords": self.expected_keywords,
            "min_output_length": self.min_output_length,
            "semantic_constraints": self.semantic_constraints,
        }

    @classmethod
    def from_dict(cls, data: dict) -> SupervisedTask:
        """Deserialize from state store. Safe against missing keys."""
        if not data or "agent_id" not in data:
            return cls(agent_id="unknown")
        return cls(**{
            k: v for k, v in data.items()
            if k in cls.__dataclass_fields__
        })


@dataclass
class VerificationResult:
    """Result of verifying a supervised task."""

    passed: bool
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)

    def summary(self) -> str:
        """Human-readable summary for injection."""
        if self.passed:
            return f"✅ Agent verified ({len(self.warnings)} warnings)"
        fails = "\n".join(f"  ❌ {f}" for f in self.failures[:5])
        return f"⚠ Agent verification FAILED:\n{fails}"


class AgentSupervisor:
    """Manage supervised subagent tasks.

    Register tasks before spawn, verify after completion.
    State persisted via StateStore (survives process death).
    """

    def __init__(self, cache_dir: str):
        self._store = StateStore(cache_dir)

    def register(self, task: SupervisedTask) -> None:
        """Register a task contract before spawning subagent."""
        tasks = self._store.read(_NS, "tasks", default={})
        tasks[task.agent_id] = task.to_dict()
        self._store.write(_NS, "tasks", tasks)

    def get_task(self, agent_id: str) -> Optional[SupervisedTask]:
        """Retrieve a registered task."""
        tasks = self._store.read(_NS, "tasks", default={})
        data = tasks.get(agent_id)
        if data:
            return SupervisedTask.from_dict(data)
        return None

    def verify(
        self,
        agent_id: str,
        workspace: str = "",
        agent_output: str = "",
    ) -> VerificationResult:
        """Verify a completed subagent against its contract.

        Args:
            agent_id: The subagent identifier.
            workspace: Project root for file resolution.
            agent_output: The subagent's result text.

        Returns:
            VerificationResult with pass/fail and details.
        """
        task = self.get_task(agent_id)
        if task is None:
            return VerificationResult(
                passed=True,
                warnings=["No contract registered — unmonitored agent"],
            )

        failures: list[str] = []
        warnings: list[str] = []

        # L1: File existence (path traversal safe)
        for fpath in task.expected_files:
            abs_path = (
                os.path.join(workspace, fpath) if not os.path.isabs(fpath)
                else fpath
            )
            # P0 fix: prevent path traversal
            if workspace and not os.path.realpath(abs_path).startswith(
                os.path.realpath(workspace)
            ):
                failures.append(f"Path traversal blocked: {fpath}")
                continue
            if not os.path.isfile(abs_path):
                failures.append(f"Missing file: {fpath}")

        # L2: Output length
        if task.min_output_length > 0:
            if len(agent_output) < task.min_output_length:
                failures.append(
                    f"Output too short: {len(agent_output)} < "
                    f"{task.min_output_length}"
                )

        # L2: Pattern matching (regex, ReDoS-safe)
        _MAX_OUTPUT_FOR_REGEX = 50_000  # Limit to prevent ReDoS
        safe_output = agent_output[:_MAX_OUTPUT_FOR_REGEX]
        for pattern in task.expected_patterns:
            try:
                compiled = re.compile(pattern, re.IGNORECASE)
                if not compiled.search(safe_output):
                    failures.append(f"Missing pattern: {pattern}")
            except re.error:
                warnings.append(f"Invalid regex: {pattern}")

        # L2: Keyword matching (literal)
        for keyword in task.expected_keywords:
            if keyword.lower() not in agent_output.lower():
                failures.append(f"Missing keyword: {keyword}")

        # L3: Semantic constraints (future)
        if task.semantic_constraints:
            warnings.append(
                f"{len(task.semantic_constraints)} semantic constraints "
                "skipped (LLM judge not yet implemented)"
            )

        # Clean up completed task
        self._complete(agent_id)

        return VerificationResult(
            passed=len(failures) == 0,
            failures=failures,
            warnings=warnings,
            details={
                "agent_id": agent_id,
                "description": task.description,
                "checks_run": (
                    len(task.expected_files)
                    + len(task.expected_patterns)
                    + len(task.expected_keywords)
                    + (1 if task.min_output_length > 0 else 0)
                ),
            },
        )

    def _complete(self, agent_id: str) -> None:
        """Remove completed task from registry."""
        tasks = self._store.read(_NS, "tasks", default={})
        tasks.pop(agent_id, None)
        self._store.write(_NS, "tasks", tasks)

    def pending_tasks(self) -> list[str]:
        """List agent IDs with pending (unverified) tasks."""
        tasks = self._store.read(_NS, "tasks", default={})
        return list(tasks.keys())


def verify_task(
    cache_dir: str,
    agent_id: str,
    workspace: str = "",
    agent_output: str = "",
) -> VerificationResult:
    """Convenience: verify a task without creating a supervisor instance."""
    sup = AgentSupervisor(cache_dir)
    return sup.verify(agent_id, workspace, agent_output)
