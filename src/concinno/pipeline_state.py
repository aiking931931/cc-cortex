"""concinno.pipeline_state — Think→Plan→Build→Review→Test→Ship pipeline state.

@module pipeline_state
@responsibility Manage pipeline state file (.claude/pipeline-state.json) that connects
    Skills across the Think→Plan→Build→Review→Test→Ship workflow. Each skill reads
    the previous phase's output and writes its own results.
@dependencies json, pathlib, datetime
@exports load_state, save_state, clear_state, get_next_suggestion, PIPELINE_PHASES

I trace the thread from thought to ship. No step stands alone.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ── Pipeline Phase Definitions ─────────────────────────────

PIPELINE_PHASES = ("think", "plan", "build", "review", "qa", "ship", "shipped")

_PHASE_ORDER = {phase: i for i, phase in enumerate(PIPELINE_PHASES)}

# ── State File Path ────────────────────────────────────────


def _state_path(project_dir: Optional[str | Path] = None) -> Path:
    """Resolve pipeline state file path.

    Args:
        project_dir: Project root directory. If None, uses cwd.

    Returns:
        Path to .claude/pipeline-state.json
    """
    root = Path(project_dir) if project_dir else Path.cwd()
    return root / ".claude" / "pipeline-state.json"


# ── Core API ───────────────────────────────────────────────


def load_state(project_dir: Optional[str | Path] = None) -> dict[str, Any]:
    """Load pipeline state from disk.

    Args:
        project_dir: Project root directory.

    Returns:
        Pipeline state dict, or empty dict if no state file exists.
    """
    path = _state_path(project_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(
    phase: str,
    data: dict[str, Any],
    *,
    feature: Optional[str] = None,
    project_dir: Optional[str | Path] = None,
) -> Path:
    """Save pipeline state for a phase.

    Merges phase data into existing state. Each phase's data is stored
    under its own key, preserving prior phases.

    Args:
        phase: Pipeline phase name (must be in PIPELINE_PHASES).
        data: Phase-specific data to store.
        feature: Feature name being worked on.
        project_dir: Project root directory.

    Returns:
        Path to the written state file.

    Raises:
        ValueError: If phase is not a valid pipeline phase.
    """
    if phase not in _PHASE_ORDER:
        msg = f"Invalid phase '{phase}'. Must be one of: {PIPELINE_PHASES}"
        raise ValueError(msg)

    path = _state_path(project_dir)
    state = load_state(project_dir)

    state["current_phase"] = phase
    state["timestamp"] = datetime.now(tz=timezone.utc).isoformat()
    state[phase] = data

    if feature:
        state["feature"] = feature

    state["next_suggested"] = get_next_suggestion(phase, data)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def clear_state(project_dir: Optional[str | Path] = None) -> bool:
    """Clear pipeline state (after ship or to start fresh).

    Args:
        project_dir: Project root directory.

    Returns:
        True if state file was deleted, False if it didn't exist.
    """
    path = _state_path(project_dir)
    if path.exists():
        path.unlink()
        return True
    return False


def get_phase_data(
    phase: str, project_dir: Optional[str | Path] = None
) -> dict[str, Any]:
    """Get data for a specific phase.

    Args:
        phase: Pipeline phase name.
        project_dir: Project root directory.

    Returns:
        Phase data dict, or empty dict if phase not yet executed.
    """
    state = load_state(project_dir)
    return state.get(phase, {})


def is_phase_complete(
    phase: str, project_dir: Optional[str | Path] = None
) -> bool:
    """Check if a specific phase has been completed.

    Args:
        phase: Pipeline phase name.
        project_dir: Project root directory.

    Returns:
        True if phase data exists in state.
    """
    state = load_state(project_dir)
    return phase in state and isinstance(state[phase], dict)


# ── Next Step Suggestion ───────────────────────────────────


def get_next_suggestion(phase: str, phase_data: dict[str, Any]) -> str:
    """Suggest the next pipeline step based on current phase and its results.

    Args:
        phase: Current phase name.
        phase_data: Current phase's result data.

    Returns:
        Human-readable suggestion for next step.
    """
    if phase == "think":
        criteria = phase_data.get("exit_criteria", [])
        if len(criteria) <= 2:
            return "Requirements clear. Start building."
        if len(criteria) <= 5:
            return "Consider /review after implementation."
        return "Recommend Plan mode before coding — complex requirements."

    if phase == "review":
        verdict = phase_data.get("verdict", "").upper()
        if verdict == "SHIP":
            return "/qa — run targeted tests on changed code."
        if verdict == "REVISE":
            return "Fix CRITICAL findings, then re-run /review."
        return "BLOCKED — requires design discussion before proceeding."

    if phase == "qa":
        verdict = phase_data.get("verdict", "").upper()
        if verdict == "PASS":
            return "/ship — all checks passed, ready to release."
        return "Fix failing tests, then re-run /qa."

    if phase == "ship":
        return "Pipeline complete. Monitor deployment."

    if phase == "shipped":
        return "Feature shipped. Start next /think cycle."

    return f"Continue to next phase after {phase}."


# ── Summary ────────────────────────────────────────────────


def pipeline_summary(project_dir: Optional[str | Path] = None) -> str:
    """Generate a one-line pipeline progress summary.

    Args:
        project_dir: Project root directory.

    Returns:
        Summary string like "Think ✅ → Review ✅ → QA ⬜ → Ship ⬜"
    """
    state = load_state(project_dir)
    parts = []
    for phase in PIPELINE_PHASES:
        if phase in ("plan", "build"):
            continue  # Implicit phases, not tracked
        icon = "✅" if phase in state else "⬜"
        label = phase.capitalize()
        if phase == state.get("current_phase"):
            icon = "🔄"
        parts.append(f"{label} {icon}")
    return " → ".join(parts)
