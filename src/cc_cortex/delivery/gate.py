"""DeliveryGate — Enterprise delivery verification framework.

@module delivery.gate
@responsibility D1-D6: define_done, verify, report, retry, audit, gate_check
@dependencies delivery._base
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

from ._base import (
    Criterion,
    CriterionType,
    DeliveryReport,
    DeliveryState,
    ExitCriteria,
    VerificationResult,
)

logger = logging.getLogger(__name__)


class DeliveryGate:
    """Enterprise delivery verification framework.

    Usage::

        gate = DeliveryGate(audit_dir="/path/to/logs")
        criteria = gate.define_done("Fix auth bug",
            primary=["login test passes"],
            safety=["no regression in user tests"])
        # ... do work, gather evidence ...
        result = gate.verify(criteria, evidence={"login test passes": True})
        report = gate.report(criteria, result)
        gate.audit_log(criteria, result, report)
    """

    def __init__(self, audit_dir: str = "") -> None:
        self._audit_dir = audit_dir
        # In-memory task registry (per-process)
        self._active: dict[str, ExitCriteria] = {}
        self._results: dict[str, VerificationResult] = {}

    # ── D1: ExitCriteria Definition ────────────────────────

    def define_done(
        self,
        task: str,
        primary: Optional[list[str]] = None,
        safety: Optional[list[str]] = None,
        task_id: str = "",
    ) -> ExitCriteria:
        """Define binary pass/fail completion standard BEFORE starting work.

        Args:
            task: One-line task description.
            primary: Functional criteria (did the right thing).
            safety: Safety criteria (didn't break anything).
            task_id: Optional ID for audit correlation.

        Returns:
            ExitCriteria instance with all criteria registered.

        Raises:
            ValueError: If both primary and safety are empty.
        """
        criteria_list: list[Criterion] = []
        for desc in (primary or []):
            criteria_list.append(Criterion(desc, CriterionType.PRIMARY))
        for desc in (safety or []):
            criteria_list.append(Criterion(desc, CriterionType.SAFETY))

        if not criteria_list:
            raise ValueError(
                "At least one criterion required. "
                "Define what 'done' means before starting."
            )

        task_id = task_id or f"task_{int(time.time() * 1000)}"
        ec = ExitCriteria(
            task=task,
            criteria=criteria_list,
            task_id=task_id,
        )
        self._active[task_id] = ec
        logger.debug("ExitCriteria defined: %s (%d criteria)", task, len(criteria_list))
        return ec

    # ── D2: Mechanical Verifier + Dual Verification ───────

    @staticmethod
    def _evaluate_evidence(val: Any) -> tuple[bool, str]:
        """Evaluate a single evidence value into (passed, evidence_text)."""
        if isinstance(val, bool):
            return val, "passed" if val else "failed"
        if isinstance(val, int):
            return val == 0, f"exit code {val}"
        if isinstance(val, str):
            return bool(val), val or "no evidence"
        if val is None:
            return False, "not verified"
        return bool(val), str(val)

    def verify(
        self,
        criteria: ExitCriteria,
        evidence: Optional[dict[str, Any]] = None,
    ) -> VerificationResult:
        """Verify work against ExitCriteria using mechanical (binary) checks.

        Args:
            criteria: The ExitCriteria to verify against.
            evidence: Dict mapping criterion description -> pass/fail value.

        Returns:
            VerificationResult with all criteria evaluated.
        """
        evidence = evidence or {}
        for criterion in criteria.criteria:
            key = criterion.description
            if key in evidence:
                criterion.passed, criterion.evidence = self._evaluate_evidence(
                    evidence[key],
                )
            else:
                criterion.passed = None
                criterion.evidence = "not evaluated"

        result = VerificationResult(criteria=criteria)
        self._results[criteria.task_id] = result
        logger.debug(
            "Verification: %s — %d/%d passed",
            criteria.task, result.passed_count, result.total_count,
        )
        return result

    # ── D3: Three-State Report Generator ──────────────────

    def report(
        self,
        criteria: ExitCriteria,
        result: VerificationResult,
        blockers: Optional[list[str]] = None,
        attempts: Optional[list[str]] = None,
    ) -> DeliveryReport:
        """Generate honest three-state delivery report.

        Args:
            criteria: The original ExitCriteria.
            result: The VerificationResult from verify().
            blockers: What couldn't be done and why (for partial/fail).
            attempts: What was tried before giving up (for fail).

        Returns:
            DeliveryReport with state, summary, and per-criterion details.
        """
        # Determine state
        if result.all_passed:
            state = DeliveryState.PASS
            summary = f"{result.passed_count}/{result.total_count} criteria passed"
        elif result.passed_count > 0:
            state = DeliveryState.PARTIAL
            failed = result.failed_criteria
            summary = (
                f"{result.passed_count}/{result.total_count} passed. "
                f"Blocked: {', '.join(c.description for c in failed[:3])}"
            )
        else:
            state = DeliveryState.FAIL
            summary = f"0/{result.total_count} criteria met"

        details = [c.to_dict() for c in criteria.criteria]

        return DeliveryReport(
            state=state,
            task=criteria.task,
            summary=summary,
            details=details,
            blockers=blockers or [],
            attempts=attempts or [],
        )

    # ── D4: Karpathy Loop ─────────────────────────────────

    def should_retry(
        self,
        result: VerificationResult,
        max_iterations: int = 5,
        current_iteration: int = 0,
    ) -> bool:
        """Determine if the Karpathy Loop should iterate again.

        Returns True if:
          - Not all criteria passed
          - Haven't exceeded max iterations
          - At least one criterion is fixable (has evidence, not "not evaluated")

        Args:
            result: Current VerificationResult.
            max_iterations: Circuit breaker limit.
            current_iteration: Current loop count.
        """
        if result.all_passed:
            return False
        if current_iteration >= max_iterations:
            logger.info(
                "Karpathy Loop: max iterations (%d) reached for %s",
                max_iterations, result.criteria.task,
            )
            return False
        # At least one failed criterion must have been evaluated (fixable)
        fixable = [
            c for c in result.failed_criteria
            if c.evidence != "not evaluated"
        ]
        return len(fixable) > 0

    def rollback_decision(self, result: VerificationResult) -> bool:
        """Determine if results warrant a rollback.

        Rollback if safety criteria failed (broke something existing).
        Primary failures don't warrant rollback (just incomplete).
        """
        return not result.all_safety_passed

    # ── D5: Audit Log ──────────────────────────────────────

    @staticmethod
    def _build_audit_entry(
        criteria: ExitCriteria, result: VerificationResult,
        report: "DeliveryReport", extra: Optional[dict[str, Any]],
    ) -> dict:
        """Build audit log entry dict."""
        entry = {
            "timestamp": time.time(),
            "task_id": criteria.task_id,
            "task": criteria.task,
            "state": report.state.value,
            "passed": result.passed_count,
            "total": result.total_count,
            "primary_passed": result.all_primary_passed,
            "safety_passed": result.all_safety_passed,
            "criteria": criteria.to_dict(),
            "report": report.to_dict(),
        }
        if extra:
            entry["extra"] = extra
        return entry

    def audit_log(
        self,
        criteria: ExitCriteria,
        result: VerificationResult,
        report: DeliveryReport,
        extra: Optional[dict[str, Any]] = None,
    ) -> str:
        """Append immutable audit entry to JSONL log.

        Returns:
            Path to audit log file (empty string if write failed).
        """
        audit_dir = self._audit_dir
        if not audit_dir:
            workspace = os.environ.get("CLAUDE_PROJECT_DIR", "")
            if not workspace:
                return ""
            audit_dir = os.path.join(
                workspace, ".cc_cortex_cache", "delivery_audit",
            )
        entry = self._build_audit_entry(criteria, result, report, extra)
        try:
            os.makedirs(audit_dir, exist_ok=True)
            log_path = os.path.join(audit_dir, "delivery_audit.jsonl")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            logger.debug("Audit logged: %s → %s", criteria.task_id, log_path)
            return log_path
        except Exception as e:
            logger.warning("Audit log write failed: %s", e)
            return ""

    # ── D6: Gate Check (for PreToolUse integration) ────────

    def gate_check(
        self,
        task_id: str = "",
    ) -> Optional[str]:
        """Check if a task can be submitted (all criteria verified and passed).

        Returns:
            None if submission is allowed (all passed or no criteria defined).
            String with deny reason if submission should be blocked.
        """
        if not task_id:
            # No task registered = no gate (fail-open)
            return None

        criteria = self._active.get(task_id)
        if criteria is None:
            return None  # Unknown task = fail-open

        result = self._results.get(task_id)
        if result is None:
            return (
                f"Task '{criteria.task}' has ExitCriteria defined but no verification run. "
                f"Run verify() before submitting."
            )

        if not result.all_passed:
            failed = result.failed_criteria
            desc = "; ".join(c.description for c in failed[:3])
            return (
                f"Task '{criteria.task}' failed "
                f"{len(failed)}/{result.total_count} criteria: "
                f"{desc}. Fix and re-verify before submitting."
            )

        return None  # All good

    # ── Introspection ──────────────────────────────────────

    def active_tasks(self) -> dict[str, dict[str, Any]]:
        """List active tasks and their verification status."""
        out: dict[str, dict[str, Any]] = {}
        for tid, criteria in self._active.items():
            result = self._results.get(tid)
            out[tid] = {
                "task": criteria.task,
                "criteria_count": len(criteria.criteria),
                "verified": result is not None,
                "all_passed": result.all_passed if result else None,
                "passed_count": result.passed_count if result else 0,
            }
        return out

    def clear_task(self, task_id: str) -> bool:
        """Remove a completed task from the active registry."""
        removed = task_id in self._active
        self._active.pop(task_id, None)
        self._results.pop(task_id, None)
        return removed
