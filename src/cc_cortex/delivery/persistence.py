"""Persistence — cross-hook state for delivery gate.

@module delivery.persistence
@responsibility Save/load DeliveryGate state, on_stop_check
@dependencies delivery._base, delivery.gate
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from ._base import (
    Criterion,
    CriterionType,
    ExitCriteria,
    VerificationResult,
)
from .gate import DeliveryGate

logger = logging.getLogger(__name__)


def _state_path(cache_dir: str = "") -> str:
    """Return path to delivery state JSON."""
    if not cache_dir:
        project = os.environ.get("CLAUDE_PROJECT_DIR", ".")
        cache_dir = os.path.join(project, ".cc_cortex_cache")
    return os.path.join(cache_dir, "delivery_state.json")


def save_state(gate: DeliveryGate, cache_dir: str = "") -> None:
    """Persist active tasks + results to JSON for cross-hook access."""
    path = _state_path(cache_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data: dict[str, Any] = {}
    for tid, criteria in gate._active.items():
        entry: dict[str, Any] = {"criteria": criteria.to_dict()}
        result = gate._results.get(tid)
        if result:
            entry["result"] = result.to_dict()
        data[tid] = entry
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        logger.debug("delivery save_state failed", exc_info=True)


def load_state(cache_dir: str = "") -> DeliveryGate:
    """Load persisted delivery state into a new DeliveryGate."""
    path = _state_path(cache_dir)
    gate = DeliveryGate()
    if not os.path.isfile(path):
        return gate
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return gate
    for tid, entry in data.items():
        cd = entry.get("criteria", {})
        criteria_list: list[Criterion] = []
        for c in cd.get("criteria", []):
            criteria_list.append(Criterion(
                description=c.get("description", ""),
                criterion_type=CriterionType(
                    c.get("type", "primary")
                ),
                passed=c.get("passed"),
                evidence=c.get("evidence", ""),
            ))
        ec = ExitCriteria(
            task=cd.get("task", ""),
            criteria=criteria_list,
            created_at=cd.get("created_at", 0),
            task_id=tid,
        )
        gate._active[tid] = ec
        rd = entry.get("result")
        if rd:
            # Rebuild VerificationResult from persisted criteria
            gate._results[tid] = VerificationResult(
                criteria=ec,
                verified_at=rd.get("verified_at", 0),
            )
    return gate


def on_stop_check(cache_dir: str = "") -> str:
    """Check delivery state at session end. Returns stderr report.

    Returns empty string if nothing to report (all passed or no tasks).
    """
    gate = load_state(cache_dir)
    tasks = gate.active_tasks()
    if not tasks:
        return ""

    lines: list[str] = []
    unverified = []
    failed = []
    passed = []

    for _tid, info in tasks.items():
        if not info["verified"]:
            unverified.append(info["task"])
        elif not info["all_passed"]:
            failed.append(
                f"{info['task']} "
                f"({info['passed_count']}/{info['criteria_count']})"
            )
        else:
            passed.append(info["task"])

    if unverified:
        lines.append(f"⚠ {len(unverified)} task(s) defined but NEVER verified:")
        for t in unverified:
            lines.append(f"  - {t}")

    if failed:
        lines.append(f"❌ {len(failed)} task(s) incomplete:")
        for t in failed:
            lines.append(f"  - {t}")

    if passed:
        lines.append(f"✅ {len(passed)} task(s) fully verified")

    if not lines:
        return ""
    return "\n".join(lines)
