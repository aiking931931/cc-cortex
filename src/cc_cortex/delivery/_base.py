"""Shared enums and data models for delivery gate.

@module delivery._base
@responsibility Enums, data classes
@dependencies (none — stdlib only)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

# ── Enums ──────────────────────────────────────────────────


class DeliveryState(str, Enum):
    """Three-state honest reporting."""

    PASS = "pass"  # ✅ All criteria met, evidence attached
    PARTIAL = "partial"  # ⏸ Some criteria met, blockers documented
    FAIL = "fail"  # ❌ Cannot meet criteria, attempts documented


class CriterionType(str, Enum):
    """Criterion category for dual verification."""

    PRIMARY = "primary"  # Functional: did the right thing
    SAFETY = "safety"  # Robust: didn't break anything


# ── Data Classes ───────────────────────────────────────────


@dataclass
class Criterion:
    """A single pass/fail condition."""

    description: str
    criterion_type: CriterionType
    passed: Optional[bool] = None
    evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "type": self.criterion_type.value,
            "passed": self.passed,
            "evidence": self.evidence,
        }


@dataclass
class ExitCriteria:
    """Binary pass/fail completion standard defined BEFORE work starts.

    Attributes:
        task: One-line task description.
        criteria: List of individual pass/fail conditions.
        created_at: Epoch timestamp when criteria were defined.
        task_id: Optional unique identifier for audit correlation.
    """

    task: str
    criteria: list[Criterion] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    task_id: str = ""

    @property
    def primary_criteria(self) -> list[Criterion]:
        return [c for c in self.criteria if c.criterion_type == CriterionType.PRIMARY]

    @property
    def safety_criteria(self) -> list[Criterion]:
        return [c for c in self.criteria if c.criterion_type == CriterionType.SAFETY]

    @property
    def is_empty(self) -> bool:
        return len(self.criteria) == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "task_id": self.task_id,
            "criteria": [c.to_dict() for c in self.criteria],
            "created_at": self.created_at,
        }


@dataclass
class VerificationResult:
    """Result of verifying work against ExitCriteria.

    Attributes:
        criteria: The criteria with pass/fail filled in.
        all_primary_passed: True if every primary criterion passed.
        all_safety_passed: True if every safety criterion passed.
        verified_at: Epoch timestamp of verification.
    """

    criteria: ExitCriteria
    verified_at: float = field(default_factory=time.time)

    @property
    def all_primary_passed(self) -> bool:
        return all(
            c.passed is True for c in self.criteria.primary_criteria
        )

    @property
    def all_safety_passed(self) -> bool:
        return all(
            c.passed is True for c in self.criteria.safety_criteria
        )

    @property
    def all_passed(self) -> bool:
        return self.all_primary_passed and self.all_safety_passed

    @property
    def passed_count(self) -> int:
        return sum(1 for c in self.criteria.criteria if c.passed is True)

    @property
    def total_count(self) -> int:
        return len(self.criteria.criteria)

    @property
    def failed_criteria(self) -> list[Criterion]:
        return [c for c in self.criteria.criteria if c.passed is not True]

    def to_dict(self) -> dict[str, Any]:
        return {
            "criteria": self.criteria.to_dict(),
            "all_primary_passed": self.all_primary_passed,
            "all_safety_passed": self.all_safety_passed,
            "all_passed": self.all_passed,
            "passed_count": self.passed_count,
            "total_count": self.total_count,
            "verified_at": self.verified_at,
        }


@dataclass
class DeliveryReport:
    """Three-state honest report.

    Attributes:
        state: pass / partial / fail.
        task: Task description.
        summary: One-line summary.
        details: Per-criterion breakdown.
        blockers: What couldn't be done and why (for partial/fail).
        attempts: What was tried (for fail).
    """

    state: DeliveryState
    task: str
    summary: str
    details: list[dict[str, Any]] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    attempts: list[str] = field(default_factory=list)

    @property
    def emoji(self) -> str:
        return {"pass": "✅", "partial": "⏸", "fail": "❌"}[self.state.value]

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "state": self.state.value,
            "task": self.task,
            "summary": self.summary,
            "details": self.details,
        }
        if self.blockers:
            d["blockers"] = self.blockers
        if self.attempts:
            d["attempts"] = self.attempts
        return d

    def format_text(self) -> str:
        """Human-readable report."""
        lines = [f"{self.emoji} {self.task}: {self.summary}"]
        for d in self.details:
            mark = "✅" if d.get("passed") else "❌"
            lines.append(f"  {mark} [{d.get('type', '?')}] {d.get('description', '')}")
            if d.get("evidence"):
                lines.append(f"      Evidence: {d['evidence']}")
        if self.blockers:
            lines.append("  Blockers:")
            for b in self.blockers:
                lines.append(f"    - {b}")
        if self.attempts:
            lines.append("  Attempted:")
            for a in self.attempts:
                lines.append(f"    - {a}")
        return "\n".join(lines)
