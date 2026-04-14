"""Tests for cc_cortex.task_orchestrator."""

from __future__ import annotations

import pytest

from cc_cortex.task_orchestrator import TaskOrchestrator


@pytest.fixture
def orch(tmp_path):
    return TaskOrchestrator(str(tmp_path), "test-session-1234")


class TestSetGoal:
    def test_set_goal_only(self, orch):
        orch.set_goal("Build feature X")
        p = orch.progress()
        assert p["goal"] == "Build feature X"
        assert p["total"] == 0
        assert p["completed"] == 0
        assert p["percent"] == 0.0

    def test_set_goal_with_subtasks(self, orch):
        orch.set_goal("Deploy", subtasks=["build", "test", "ship"])
        p = orch.progress()
        assert p["goal"] == "Deploy"
        assert p["total"] == 3
        assert p["remaining"] == 3

    def test_overwrite_goal(self, orch):
        orch.set_goal("Old goal", subtasks=["a"])
        orch.set_goal("New goal", subtasks=["x", "y"])
        p = orch.progress()
        assert p["goal"] == "New goal"
        assert p["total"] == 2

    def test_set_goal_empty_subtasks(self, orch):
        orch.set_goal("Minimal")
        p = orch.progress()
        assert p["subtasks"] == []


class TestCompleteSubtask:
    def test_complete_valid_index(self, orch):
        orch.set_goal("G", subtasks=["a", "b", "c"])
        orch.complete_subtask(1)
        p = orch.progress()
        assert p["completed"] == 1
        assert p["subtasks"][1]["done"] is True

    def test_complete_out_of_range(self, orch):
        orch.set_goal("G", subtasks=["a"])
        orch.complete_subtask(5)  # should not crash
        assert orch.progress()["completed"] == 0

    def test_complete_negative_index(self, orch):
        orch.set_goal("G", subtasks=["a"])
        orch.complete_subtask(-1)  # should not crash
        assert orch.progress()["completed"] == 0

    def test_complete_all(self, orch):
        orch.set_goal("G", subtasks=["a", "b"])
        orch.complete_subtask(0)
        orch.complete_subtask(1)
        p = orch.progress()
        assert p["completed"] == 2
        assert p["percent"] == 100.0


class TestProgress:
    def test_empty_state(self, orch):
        p = orch.progress()
        assert p["goal"] == ""
        assert p["total"] == 0
        assert p["percent"] == 0.0

    def test_partial_progress(self, orch):
        orch.set_goal("G", subtasks=["a", "b", "c", "d"])
        orch.complete_subtask(0)
        orch.complete_subtask(2)
        p = orch.progress()
        assert p["completed"] == 2
        assert p["remaining"] == 2
        assert p["percent"] == 50.0


class TestMilestoneReport:
    def test_empty_report(self, orch):
        assert orch.milestone_report() == ""

    def test_report_format(self, orch):
        orch.set_goal("Ship it", subtasks=["build", "test"])
        orch.complete_subtask(0)
        report = orch.milestone_report()
        assert "Ship it" in report
        assert "1/2" in report
        assert "\u2705" in report  # checkmark for done
        assert "\u2b1c" in report  # square for pending


class TestShouldReport:
    def test_at_interval(self, orch):
        orch.set_goal("G")
        assert orch.should_report(30) is True
        assert orch.should_report(60) is True

    def test_not_at_interval(self, orch):
        orch.set_goal("G")
        assert orch.should_report(15) is False
        assert orch.should_report(31) is False

    def test_no_goal(self, orch):
        assert orch.should_report(30) is False

    def test_zero_tool_count(self, orch):
        orch.set_goal("G")
        assert orch.should_report(0) is False

    def test_custom_interval(self, orch):
        orch.set_goal("G")
        assert orch.should_report(10, interval=10) is True
        assert orch.should_report(10, interval=30) is False
