"""Tests for concinno.pipeline_state — Pipeline state management."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from concinno.pipeline_state import (
    PIPELINE_PHASES,
    clear_state,
    get_next_suggestion,
    get_phase_data,
    is_phase_complete,
    load_state,
    pipeline_summary,
    save_state,
)


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    """Create a temporary project directory with .claude/ subdirectory."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    return tmp_path


# ── load_state ──


class TestLoadState:
    def test_empty_when_no_file(self, tmp_project: Path) -> None:
        assert load_state(tmp_project) == {}

    def test_loads_valid_json(self, tmp_project: Path) -> None:
        state_file = tmp_project / ".claude" / "pipeline-state.json"
        state_file.write_text('{"current_phase": "think"}', encoding="utf-8")
        result = load_state(tmp_project)
        assert result["current_phase"] == "think"

    def test_returns_empty_on_invalid_json(self, tmp_project: Path) -> None:
        state_file = tmp_project / ".claude" / "pipeline-state.json"
        state_file.write_text("not json {{{", encoding="utf-8")
        assert load_state(tmp_project) == {}


# ── save_state ──


class TestSaveState:
    def test_creates_state_file(self, tmp_project: Path) -> None:
        path = save_state("think", {"target": "users"}, project_dir=tmp_project)
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["current_phase"] == "think"
        assert data["think"]["target"] == "users"

    def test_preserves_prior_phases(self, tmp_project: Path) -> None:
        save_state("think", {"target": "users"}, project_dir=tmp_project)
        save_state(
            "review",
            {"verdict": "SHIP", "critical_count": 0},
            project_dir=tmp_project,
        )
        state = load_state(tmp_project)
        assert "think" in state
        assert "review" in state
        assert state["current_phase"] == "review"

    def test_sets_feature_name(self, tmp_project: Path) -> None:
        save_state(
            "think",
            {"target": "devs"},
            feature="dark-mode",
            project_dir=tmp_project,
        )
        state = load_state(tmp_project)
        assert state["feature"] == "dark-mode"

    def test_invalid_phase_raises(self, tmp_project: Path) -> None:
        with pytest.raises(ValueError, match="Invalid phase"):
            save_state("invalid_phase", {}, project_dir=tmp_project)

    def test_sets_timestamp(self, tmp_project: Path) -> None:
        save_state("think", {}, project_dir=tmp_project)
        state = load_state(tmp_project)
        assert "timestamp" in state

    def test_sets_next_suggested(self, tmp_project: Path) -> None:
        save_state("think", {"exit_criteria": ["a"]}, project_dir=tmp_project)
        state = load_state(tmp_project)
        assert "next_suggested" in state


# ── clear_state ──


class TestClearState:
    def test_deletes_existing_file(self, tmp_project: Path) -> None:
        save_state("think", {}, project_dir=tmp_project)
        assert clear_state(tmp_project) is True
        assert load_state(tmp_project) == {}

    def test_returns_false_when_no_file(self, tmp_project: Path) -> None:
        assert clear_state(tmp_project) is False


# ── get_phase_data ──


class TestGetPhaseData:
    def test_returns_phase_data(self, tmp_project: Path) -> None:
        save_state("think", {"target": "devs"}, project_dir=tmp_project)
        assert get_phase_data("think", tmp_project)["target"] == "devs"

    def test_returns_empty_for_missing_phase(self, tmp_project: Path) -> None:
        assert get_phase_data("review", tmp_project) == {}


# ── is_phase_complete ──


class TestIsPhaseComplete:
    def test_true_when_complete(self, tmp_project: Path) -> None:
        save_state("think", {"target": "devs"}, project_dir=tmp_project)
        assert is_phase_complete("think", tmp_project) is True

    def test_false_when_not_run(self, tmp_project: Path) -> None:
        assert is_phase_complete("review", tmp_project) is False


# ── get_next_suggestion ──


class TestGetNextSuggestion:
    def test_think_simple(self) -> None:
        result = get_next_suggestion("think", {"exit_criteria": ["a"]})
        assert "Start building" in result

    def test_think_medium(self) -> None:
        result = get_next_suggestion("think", {"exit_criteria": list("abcde")})
        assert "/review" in result

    def test_think_complex(self) -> None:
        result = get_next_suggestion(
            "think", {"exit_criteria": list("abcdefgh")}
        )
        assert "Plan mode" in result

    def test_review_ship(self) -> None:
        result = get_next_suggestion("review", {"verdict": "SHIP"})
        assert "/qa" in result

    def test_review_revise(self) -> None:
        result = get_next_suggestion("review", {"verdict": "REVISE"})
        assert "CRITICAL" in result

    def test_review_block(self) -> None:
        result = get_next_suggestion("review", {"verdict": "BLOCK"})
        assert "BLOCKED" in result

    def test_qa_pass(self) -> None:
        result = get_next_suggestion("qa", {"verdict": "PASS"})
        assert "/ship" in result

    def test_qa_fail(self) -> None:
        result = get_next_suggestion("qa", {"verdict": "FAIL"})
        assert "Fix" in result

    def test_ship(self) -> None:
        result = get_next_suggestion("ship", {})
        assert "complete" in result.lower() or "Monitor" in result

    def test_shipped(self) -> None:
        result = get_next_suggestion("shipped", {})
        assert "/think" in result


# ── pipeline_summary ──


class TestPipelineSummary:
    def test_empty_state(self, tmp_project: Path) -> None:
        summary = pipeline_summary(tmp_project)
        assert "Think" in summary
        assert "Ship" in summary
        assert "⬜" in summary

    def test_with_completed_phases(self, tmp_project: Path) -> None:
        save_state("think", {"target": "devs"}, project_dir=tmp_project)
        save_state("review", {"verdict": "SHIP"}, project_dir=tmp_project)
        summary = pipeline_summary(tmp_project)
        assert "✅" in summary
        assert "🔄" in summary  # Current phase indicator


# ── PIPELINE_PHASES ──


class TestPipelinePhases:
    def test_ordered(self) -> None:
        assert PIPELINE_PHASES[0] == "think"
        assert PIPELINE_PHASES[-1] == "shipped"

    def test_all_present(self) -> None:
        required = {"think", "review", "qa", "ship", "shipped"}
        assert required.issubset(set(PIPELINE_PHASES))
