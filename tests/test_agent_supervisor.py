"""Tests for concinno.agent_supervisor — supervised subagent framework."""

from __future__ import annotations

import os
import tempfile

from concinno.agent_supervisor import (
    AgentSupervisor,
    SupervisedTask,
    VerificationResult,
    verify_task,
)


def _make_supervisor():
    """Create supervisor with temp cache."""
    return AgentSupervisor(cache_dir=tempfile.mkdtemp())


class TestSupervisedTask:
    def test_create_default(self):
        task = SupervisedTask(agent_id="test-1")
        assert task.agent_id == "test-1"
        assert task.expected_files == []

    def test_roundtrip(self):
        task = SupervisedTask(
            agent_id="a1",
            description="Research Gemma 4",
            expected_files=["report.md"],
            expected_keywords=["Apache 2.0"],
        )
        data = task.to_dict()
        restored = SupervisedTask.from_dict(data)
        assert restored.agent_id == "a1"
        assert restored.expected_files == ["report.md"]
        assert restored.expected_keywords == ["Apache 2.0"]


class TestVerificationResult:
    def test_passed_summary(self):
        r = VerificationResult(passed=True)
        assert "✅" in r.summary()

    def test_failed_summary(self):
        r = VerificationResult(passed=False, failures=["Missing file: x.md"])
        assert "FAILED" in r.summary()
        assert "x.md" in r.summary()


class TestAgentSupervisor:
    def test_register_and_get(self):
        sup = _make_supervisor()
        task = SupervisedTask(agent_id="a1", description="test")
        sup.register(task)
        got = sup.get_task("a1")
        assert got is not None
        assert got.agent_id == "a1"

    def test_get_nonexistent(self):
        sup = _make_supervisor()
        assert sup.get_task("nope") is None

    def test_verify_no_contract(self):
        sup = _make_supervisor()
        result = sup.verify("unknown")
        assert result.passed
        assert "unmonitored" in result.warnings[0]

    def test_verify_file_exists(self):
        sup = _make_supervisor()
        tmpdir = tempfile.mkdtemp()
        # Create expected file
        fpath = os.path.join(tmpdir, "report.md")
        with open(fpath, "w") as f:
            f.write("# Report")
        task = SupervisedTask(
            agent_id="a1",
            expected_files=["report.md"],
        )
        sup.register(task)
        result = sup.verify("a1", workspace=tmpdir)
        assert result.passed

    def test_verify_file_missing(self):
        sup = _make_supervisor()
        tmpdir = tempfile.mkdtemp()
        task = SupervisedTask(
            agent_id="a1",
            expected_files=["missing.md"],
        )
        sup.register(task)
        result = sup.verify("a1", workspace=tmpdir)
        assert not result.passed
        assert "Missing file" in result.failures[0]

    def test_verify_keyword_present(self):
        sup = _make_supervisor()
        task = SupervisedTask(
            agent_id="a1",
            expected_keywords=["Apache 2.0"],
        )
        sup.register(task)
        result = sup.verify(
            "a1", agent_output="License: Apache 2.0, MIT",
        )
        assert result.passed

    def test_verify_keyword_missing(self):
        sup = _make_supervisor()
        task = SupervisedTask(
            agent_id="a1",
            expected_keywords=["Apache 2.0"],
        )
        sup.register(task)
        result = sup.verify("a1", agent_output="License: MIT only")
        assert not result.passed
        assert "Apache 2.0" in result.failures[0]

    def test_verify_pattern_match(self):
        sup = _make_supervisor()
        task = SupervisedTask(
            agent_id="a1",
            expected_patterns=[r"AIME.*\d+\.?\d*%"],
        )
        sup.register(task)
        result = sup.verify("a1", agent_output="AIME score: 89.2%")
        assert result.passed

    def test_verify_pattern_miss(self):
        sup = _make_supervisor()
        task = SupervisedTask(
            agent_id="a1",
            expected_patterns=[r"AIME.*\d+\.?\d*%"],
        )
        sup.register(task)
        result = sup.verify("a1", agent_output="No score here")
        assert not result.passed

    def test_verify_min_length(self):
        sup = _make_supervisor()
        task = SupervisedTask(
            agent_id="a1",
            min_output_length=100,
        )
        sup.register(task)
        short = sup.verify("a1", agent_output="too short")
        assert not short.passed
        long_result = sup.verify("a1", agent_output="x" * 200)
        # Task was cleaned up after first verify, re-register
        sup.register(task)
        long_result = sup.verify("a1", agent_output="x" * 200)
        assert long_result.passed

    def test_verify_cleans_up(self):
        sup = _make_supervisor()
        task = SupervisedTask(agent_id="a1")
        sup.register(task)
        assert "a1" in sup.pending_tasks()
        sup.verify("a1")
        assert "a1" not in sup.pending_tasks()

    def test_pending_tasks(self):
        sup = _make_supervisor()
        sup.register(SupervisedTask(agent_id="a1"))
        sup.register(SupervisedTask(agent_id="a2"))
        assert set(sup.pending_tasks()) == {"a1", "a2"}

    def test_semantic_constraints_warning(self):
        sup = _make_supervisor()
        task = SupervisedTask(
            agent_id="a1",
            semantic_constraints=["Must be factual"],
        )
        sup.register(task)
        result = sup.verify("a1")
        assert result.passed  # Semantic skipped, not a failure
        assert "skipped" in result.warnings[0]

    def test_convenience_verify_task(self):
        cache = tempfile.mkdtemp()
        sup = AgentSupervisor(cache)
        sup.register(SupervisedTask(
            agent_id="conv",
            expected_keywords=["hello"],
        ))
        result = verify_task(cache, "conv", agent_output="hello world")
        assert result.passed
