"""Tests for the auto-capture-goal step in on_prompt_submit.

Exercises ``_auto_capture_goal`` directly: first prompt seeds the
TaskOrchestrator, subsequent prompts don't clobber an existing goal,
empty/short/missing-context inputs are silent no-ops.
"""

from __future__ import annotations

import os
import tempfile

from concinno.hooks.on_prompt_submit import _auto_capture_goal
from concinno.task_orchestrator import TaskOrchestrator


def _fresh_context() -> tuple[str, str]:
    tmp = tempfile.mkdtemp()
    cache_dir = os.path.join(tmp, ".concinno_cache")
    session_id = "sess-goalcap01"
    return cache_dir, session_id


class TestAutoCaptureGoal:
    def test_first_prompt_seeds_goal(self):
        cache_dir, session_id = _fresh_context()
        _auto_capture_goal(
            "Refactor the RAG namespace routing layer",
            cache_dir,
            session_id,
        )
        orch = TaskOrchestrator(cache_dir=cache_dir, session_id=session_id)
        assert orch.progress()["goal"] == (
            "Refactor the RAG namespace routing layer"
        )

    def test_second_prompt_does_not_clobber(self):
        cache_dir, session_id = _fresh_context()
        _auto_capture_goal("first goal text", cache_dir, session_id)
        _auto_capture_goal("a totally different ask", cache_dir, session_id)
        orch = TaskOrchestrator(cache_dir=cache_dir, session_id=session_id)
        assert orch.progress()["goal"] == "first goal text"

    def test_explicit_goal_is_preserved(self):
        """If the user already set a goal via CLI, auto-capture stays silent."""
        cache_dir, session_id = _fresh_context()
        orch = TaskOrchestrator(cache_dir=cache_dir, session_id=session_id)
        orch.set_goal("explicit goal", subtasks=["a", "b"])

        _auto_capture_goal("random chit-chat", cache_dir, session_id)

        p = orch.progress()
        assert p["goal"] == "explicit goal"
        assert p["total"] == 2

    def test_long_prompt_is_truncated(self):
        cache_dir, session_id = _fresh_context()
        long = "x" * 200
        _auto_capture_goal(long, cache_dir, session_id)
        orch = TaskOrchestrator(cache_dir=cache_dir, session_id=session_id)
        goal = orch.progress()["goal"]
        assert goal.endswith("...")
        # 80 char truncation + ellipsis
        assert len(goal) <= 83

    def test_short_prompt_ignored(self):
        cache_dir, session_id = _fresh_context()
        _auto_capture_goal("ok", cache_dir, session_id)
        orch = TaskOrchestrator(cache_dir=cache_dir, session_id=session_id)
        assert orch.progress()["goal"] == ""

    def test_empty_prompt_ignored(self):
        cache_dir, session_id = _fresh_context()
        _auto_capture_goal("", cache_dir, session_id)
        _auto_capture_goal("   \n  ", cache_dir, session_id)
        orch = TaskOrchestrator(cache_dir=cache_dir, session_id=session_id)
        assert orch.progress()["goal"] == ""

    def test_missing_cache_dir_is_noop(self):
        _auto_capture_goal("any text", "", "sess")
        # Did not raise — success

    def test_missing_session_id_is_noop(self):
        tmp = tempfile.mkdtemp()
        _auto_capture_goal("any text", tmp, "")
        # Did not raise — success
