"""Tests for cc_cortex.scheduler."""

import json
import os
import tempfile
from datetime import datetime
from unittest.mock import patch

from cc_cortex.scheduler import (
    TaskConfig,
    _check_dedup,
    _rotate_log,
    _update_last_run,
    launch_task,
    load_task_config,
)

# ── Config Loading ──


class TestLoadTaskConfig:
    def test_loads_defaults(self):
        cfg = load_task_config("self-reflection")
        assert cfg is not None
        assert cfg.name == "self-reflection"
        assert cfg.model == "claude-sonnet-4-6"
        assert cfg.max_budget_usd == "0.50"
        assert cfg.timeout_sec == 600
        assert cfg.enabled is True

    def test_unknown_task(self):
        assert load_task_config("nonexistent") is None

    def test_config_override(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump({
                "tasks": {
                    "self-reflection": {
                        "model": "claude-opus-4-6",
                        "max_budget_usd": "2.00",
                    }
                }
            }, f)
        try:
            cfg = load_task_config("self-reflection", config_path=path)
            assert cfg.model == "claude-opus-4-6"
            assert cfg.max_budget_usd == "2.00"
        finally:
            os.unlink(path)

    def test_disabled_task(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump({"tasks": {"scavenger": {"enabled": False}}}, f)
        try:
            cfg = load_task_config("scavenger", config_path=path)
            assert cfg.enabled is False
        finally:
            os.unlink(path)


class TestCheckDedup:
    def test_no_config(self):
        assert _check_dedup("self-reflection", 20, None) is None

    def test_too_recent(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        with os.fdopen(fd, "w") as f:
            json.dump({"tasks": {"self-reflection": {"last_run_timestamp": now}}}, f)
        try:
            result = _check_dedup("self-reflection", 20, path)
            assert result is not None
            assert "ago" in result
        finally:
            os.unlink(path)

    def test_old_enough(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            data = {"tasks": {"self-reflection": {
                "last_run_timestamp": "2020-01-01T00:00:00",
            }}}
            json.dump(data, f)
        try:
            result = _check_dedup("self-reflection", 20, path)
            assert result is None
        finally:
            os.unlink(path)


class TestUpdateLastRun:
    def test_updates_timestamp(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump({"tasks": {"self-reflection": {}}}, f)
        try:
            _update_last_run("self-reflection", path)
            with open(path) as f:
                data = json.load(f)
            assert "last_run_timestamp" in data["tasks"]["self-reflection"]
        finally:
            os.unlink(path)

    def test_creates_task_entry(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump({"tasks": {}}, f)
        try:
            _update_last_run("scavenger", path)
            with open(path) as f:
                data = json.load(f)
            assert "scavenger" in data["tasks"]
        finally:
            os.unlink(path)


class TestRotateLog:
    def test_rotates_large_log(self):
        fd, path = tempfile.mkstemp(suffix=".log")
        with os.fdopen(fd, "w") as f:
            for i in range(1000):
                f.write(f"line {i}\n")
        try:
            _rotate_log(path, keep_lines=100)
            with open(path) as f:
                lines = f.readlines()
            assert len(lines) == 100
        finally:
            os.unlink(path)

    def test_small_log_unchanged(self):
        fd, path = tempfile.mkstemp(suffix=".log")
        with os.fdopen(fd, "w") as f:
            for i in range(50):
                f.write(f"line {i}\n")
        try:
            _rotate_log(path, keep_lines=100)
            with open(path) as f:
                lines = f.readlines()
            assert len(lines) == 50
        finally:
            os.unlink(path)


class TestLaunchTask:
    def test_disabled_task_skipped(self):
        task = TaskConfig(
            name="test",
            prompt_file="test.txt",
            model="claude-sonnet-4-6",
            log_name="test.log",
            allowed_tools="Read",
            max_budget_usd="0.10",
            timeout_sec=60,
            min_interval_hours=1,
            enabled=False,
        )
        result = launch_task(task, "/tmp", "/tmp")
        assert result.skipped is True
        assert "disabled" in result.skip_reason

    @patch("cc_cortex.scheduler._check_active_sessions", return_value="active session")
    def test_active_session_skipped(self, mock_check):
        task = TaskConfig(
            name="test",
            prompt_file="test.txt",
            model="claude-sonnet-4-6",
            log_name="test.log",
            allowed_tools="Read",
            max_budget_usd="0.10",
            timeout_sec=60,
            min_interval_hours=0,
            enabled=True,
        )
        result = launch_task(task, "/tmp", "/tmp")
        assert result.skipped is True


class TestSkillInstaller:
    def test_install_skills(self):
        from cc_cortex.skills.installer import install_skills

        with tempfile.TemporaryDirectory() as td:
            installed = install_skills(td)
            assert len(installed) >= 1
            for path in installed:
                assert os.path.exists(path)
                assert path.endswith("SKILL.md")
