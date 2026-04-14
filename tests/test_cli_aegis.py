"""Tests for the ``cc aegis`` CLI subcommand family.

Exercises cmd_aegis_status / cmd_aegis_goal end-to-end: they must wire
TaskOrchestrator + CostTracker + ProgressReporter together correctly
and gracefully handle a missing session context.
"""

from __future__ import annotations

import argparse
import os
import tempfile

from cc_cortex.cli.main import (
    _aegis_context,
    cmd_aegis_goal,
    cmd_aegis_status,
)
from cc_cortex.task_orchestrator import TaskOrchestrator


def _patch_env(monkeypatch, project_dir: str, session_id: str) -> None:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", project_dir)
    monkeypatch.setenv("CC_SESSION_ID", session_id)


class TestAegisContext:
    def test_resolves_from_env(self, monkeypatch):
        tmp = tempfile.mkdtemp()
        _patch_env(monkeypatch, tmp, "sess-aaaa")
        ctx = _aegis_context()
        assert ctx is not None
        cache_dir, session_id = ctx
        assert session_id == "sess-aaaa"
        assert cache_dir.endswith(".cc_cortex_cache")

    def test_missing_env_returns_none(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        monkeypatch.delenv("CC_SESSION_ID", raising=False)
        assert _aegis_context() is None


class TestAegisStatus:
    def test_no_context_prints_hint(self, monkeypatch, capsys):
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        monkeypatch.delenv("CC_SESSION_ID", raising=False)
        cmd_aegis_status(argparse.Namespace())
        out = capsys.readouterr().out
        assert "No session context" in out

    def test_empty_state_shows_not_set(self, monkeypatch, capsys):
        tmp = tempfile.mkdtemp()
        _patch_env(monkeypatch, tmp, "sess-empty")
        cmd_aegis_status(argparse.Namespace())
        out = capsys.readouterr().out
        assert "Progress Report" in out
        assert "(not set)" in out
        assert "0/0" in out

    def test_with_seeded_goal(self, monkeypatch, capsys):
        tmp = tempfile.mkdtemp()
        _patch_env(monkeypatch, tmp, "sess-seed")
        cache_dir = os.path.join(tmp, ".cc_cortex_cache")
        # Seed a goal + one subtask done
        orch = TaskOrchestrator(
            cache_dir=cache_dir, session_id="sess-seed",
        )
        orch.set_goal("ship feature X", subtasks=["design", "code", "test"])
        orch.complete_subtask(0)

        cmd_aegis_status(argparse.Namespace())
        out = capsys.readouterr().out

        assert "ship feature X" in out
        assert "1/3" in out
        assert "design" in out
        assert "code" in out


class TestAegisGoal:
    def test_sets_goal(self, monkeypatch, capsys):
        tmp = tempfile.mkdtemp()
        _patch_env(monkeypatch, tmp, "sess-goal")

        ns = argparse.Namespace(
            goal="reach prod",
            subtask=["spec", "impl", "review"],
        )
        cmd_aegis_goal(ns)
        out = capsys.readouterr().out
        assert "OK: goal set" in out
        assert "3 subtask" in out

        # Verify persistence
        cache_dir = os.path.join(tmp, ".cc_cortex_cache")
        orch = TaskOrchestrator(
            cache_dir=cache_dir, session_id="sess-goal",
        )
        p = orch.progress()
        assert p["goal"] == "reach prod"
        assert p["total"] == 3
        assert p["completed"] == 0

    def test_goal_without_subtasks(self, monkeypatch, capsys):
        tmp = tempfile.mkdtemp()
        _patch_env(monkeypatch, tmp, "sess-goal2")
        ns = argparse.Namespace(goal="just a goal", subtask=None)
        cmd_aegis_goal(ns)
        out = capsys.readouterr().out
        assert "OK: goal set" in out
        assert "0 subtask" in out

    def test_no_context_exits(self, monkeypatch):
        import pytest

        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        monkeypatch.delenv("CC_SESSION_ID", raising=False)
        ns = argparse.Namespace(goal="x", subtask=None)
        with pytest.raises(SystemExit) as exc:
            cmd_aegis_goal(ns)
        assert exc.value.code == 1
