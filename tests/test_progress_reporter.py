"""Tests for concinno.progress_reporter."""

from __future__ import annotations

import pytest

from concinno.cost_tracker import CostTracker
from concinno.progress_reporter import ProgressReporter
from concinno.task_orchestrator import TaskOrchestrator


@pytest.fixture
def reporter(tmp_path):
    orch = TaskOrchestrator(str(tmp_path), "test-session-1234")
    cost = CostTracker(str(tmp_path), "test-session-1234", budget_usd=5.0)
    return ProgressReporter(orch, cost), orch, cost


class TestGenerateReport:
    def test_empty_report(self, reporter):
        rpt, _orch, _cost = reporter
        report = rpt.generate_report(tool_count=0)
        assert "(not set)" in report
        assert "$0.000" in report

    def test_with_goal_and_subtasks(self, reporter):
        rpt, orch, cost = reporter
        orch.set_goal("Build Aegis", subtasks=["design", "code", "test"])
        orch.complete_subtask(0)
        cost.record(50_000, 10_000)
        report = rpt.generate_report(tool_count=30)
        assert "Build Aegis" in report
        assert "1/3" in report
        assert "Completed" in report
        assert "design" in report
        assert "Remaining" in report
        assert "code" in report

    def test_report_includes_cost(self, reporter):
        rpt, orch, cost = reporter
        orch.set_goal("G")
        cost.record(100_000, 20_000)
        report = rpt.generate_report()
        assert "$" in report
        assert "%" in report

    def test_report_shows_alert_when_high(self, reporter):
        rpt, orch, cost = reporter
        orch.set_goal("G")
        cost.record(1_000_000, 1_000_000)  # $18 >> $5 budget
        report = rpt.generate_report()
        assert "alert" in report.lower() or "Cost alert" in report

    def test_all_completed(self, reporter):
        rpt, orch, _cost = reporter
        orch.set_goal("G", subtasks=["a", "b"])
        orch.complete_subtask(0)
        orch.complete_subtask(1)
        report = rpt.generate_report()
        assert "2/2" in report
        assert "100" in report
        assert "Remaining" not in report

    def test_no_subtasks(self, reporter):
        rpt, orch, _cost = reporter
        orch.set_goal("Simple task")
        report = rpt.generate_report()
        assert "Simple task" in report
        assert "0/0" in report

    def test_tool_count_in_header(self, reporter):
        rpt, _orch, _cost = reporter
        report = rpt.generate_report(tool_count=42)
        assert "#42" in report

    def test_report_separator(self, reporter):
        rpt, _orch, _cost = reporter
        report = rpt.generate_report()
        assert "\u2501" in report


class TestShouldReport:
    def test_delegates_to_orchestrator(self, reporter):
        rpt, orch, _cost = reporter
        orch.set_goal("G")
        assert rpt.should_report(30) is True
        assert rpt.should_report(15) is False

    def test_no_goal(self, reporter):
        rpt, _orch, _cost = reporter
        assert rpt.should_report(30) is False
