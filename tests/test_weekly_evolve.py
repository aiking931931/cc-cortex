"""Tests for weekly_evolve + cleanup_schedule task definitions."""

from __future__ import annotations

import re

import pytest


class TestWeeklyEvolveConfig:
    """TASK_CONFIG is valid and complete."""

    def test_config_has_required_keys(self) -> None:
        from cc_cortex.tasks.weekly_evolve import TASK_CONFIG

        required = {
            "name", "prompt_file", "model", "log_name",
            "allowed_tools", "max_budget_usd", "timeout_sec",
            "min_interval_hours",
        }
        assert required.issubset(TASK_CONFIG.keys())

    def test_config_name_is_weekly_evolve(self) -> None:
        from cc_cortex.tasks.weekly_evolve import TASK_CONFIG

        assert TASK_CONFIG["name"] == "weekly_evolve"

    def test_interval_is_seven_days(self) -> None:
        from cc_cortex.tasks.weekly_evolve import TASK_CONFIG

        assert TASK_CONFIG["min_interval_hours"] == 168


class TestWeeklyEvolvePrompt:
    """Prompt template renders correctly."""

    def test_render_with_explicit_date(self) -> None:
        from cc_cortex.tasks.weekly_evolve import render_prompt

        result = render_prompt("2026-04-16")
        assert "2026-04-16" in result
        assert "weekly_digest_2026-04-16" in result

    def test_render_default_date(self) -> None:
        from cc_cortex.tasks.weekly_evolve import render_prompt

        result = render_prompt()
        # Should contain a date-like pattern.
        assert re.search(r"\d{4}-\d{2}-\d{2}", result)

    def test_prompt_contains_key_instructions(self) -> None:
        from cc_cortex.tasks.weekly_evolve import render_prompt

        result = render_prompt("2026-01-01")
        assert "WebSearch" in result
        assert "clawsights.com" in result
        assert "git add" in result


class TestCleanupScheduleConfig:
    """cleanup_schedule TASK_CONFIG validation."""

    def test_config_has_required_keys(self) -> None:
        from cc_cortex.tasks.cleanup_schedule import TASK_CONFIG

        required = {
            "name", "prompt_file", "model", "log_name",
            "allowed_tools", "max_budget_usd", "timeout_sec",
            "min_interval_hours",
        }
        assert required.issubset(TASK_CONFIG.keys())

    def test_config_name_is_cleanup(self) -> None:
        from cc_cortex.tasks.cleanup_schedule import TASK_CONFIG

        assert TASK_CONFIG["name"] == "cleanup"

    def test_interval_is_daily(self) -> None:
        from cc_cortex.tasks.cleanup_schedule import TASK_CONFIG

        assert TASK_CONFIG["min_interval_hours"] == 24


class TestCLIEvolveSmoke:
    """CLI ``evolve`` subcommand is registered and parseable."""

    def test_evolve_subcommand_exists(self) -> None:
        """Importing the CLI should not fail, and evolve should be recognized."""
        import sys
        from unittest.mock import patch

        from cc_cortex.cli.main import main

        with patch.object(sys, "argv", ["cc-cortex", "evolve", "--help"]), \
             pytest.raises(SystemExit) as exc:
            main()
        # argparse exits 0 on --help
        assert exc.value.code == 0
