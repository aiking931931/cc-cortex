"""Tests for concinno agent_gate — research/execution split + spawn counting."""

from __future__ import annotations

import os
import tempfile

from concinno.agent_gate import (
    _check_prompt_quality,
    check,
    gate_agent_cap,
    is_research_agent,
)

# Standard execution prompt with delivery keywords (passes quality gate)
_EXEC = "fix the bug, add tests, export from index"
_EXEC2 = "build feature X with test coverage and export"

# ── is_research_agent() classification ─────────────────


class TestClassification:
    def test_explore_is_research(self):
        assert is_research_agent({"subagent_type": "Explore", "prompt": "find files"})

    def test_plan_is_research(self):
        assert is_research_agent({"subagent_type": "Plan", "prompt": "design arch"})

    def test_claude_code_guide_is_research(self):
        assert is_research_agent({"subagent_type": "claude-code-guide", "prompt": "how to"})

    def test_general_with_research_prompt(self):
        assert is_research_agent({"prompt": "研究一下這個 API 的用法"})
        assert is_research_agent({"prompt": "analyze the codebase structure"})
        assert is_research_agent({"prompt": "summarize the handoff files"})

    def test_general_with_exec_prompt(self):
        assert not is_research_agent({"prompt": "edit the config file"})
        assert not is_research_agent({"prompt": "fix the bug in main.py"})
        assert not is_research_agent({"prompt": "建立一個新的模組"})

    def test_general_with_mixed_prompt_exec_wins(self):
        # Has both research and exec keywords — exec wins
        assert not is_research_agent({"prompt": "analyze and fix the bug"})

    def test_general_with_no_keywords(self):
        # No recognized keywords — defaults to execution (conservative)
        assert not is_research_agent({"prompt": "do stuff"})

    def test_empty_input(self):
        assert not is_research_agent({})
        assert not is_research_agent({"prompt": ""})


# ── check() — research agents bypass cap ────────────────


class TestCheckResearch:
    def test_research_always_allowed(self):
        with tempfile.TemporaryDirectory() as td:
            sid = "sess1234"
            # Spawn 20 research agents — all should pass
            for i in range(20):
                r = check(
                    "Agent",
                    {"subagent_type": "Explore", "prompt": "search stuff"},
                    sid, td, max_spawns=4,
                )
                assert r["permissionDecision"] == "allow"
                assert r["agent_type"] == "research"

    def test_research_does_not_increment_exec_counter(self):
        with tempfile.TemporaryDirectory() as td:
            sid = "sess1234"
            for _ in range(10):
                check("Agent", {"subagent_type": "Explore", "prompt": "x"}, sid, td)
            r = check("Agent", {"prompt": _EXEC}, sid, td)
            assert r["count"] == 1
            assert r["agent_type"] == "execution"


# ── check() — execution agents counting ─────────────────


class TestCheckExecution:
    def test_non_agent_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            assert check("Read", {}, "sess1234", td) is None
            assert check("Bash", {}, "sess1234", td) is None

    def test_no_session_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            assert check("Agent", {}, "", td) is None

    def test_increments_count(self):
        with tempfile.TemporaryDirectory() as td:
            r1 = check("Agent", {"prompt": _EXEC}, "sess1234", td)
            assert r1 is not None
            assert r1["count"] == 1
            assert r1["level"] == "info"
            assert r1["permissionDecision"] == "allow"
            assert r1["agent_type"] == "execution"

            r2 = check("Agent", {"prompt": _EXEC2}, "sess1234", td)
            assert r2["count"] == 2

    def test_warning_at_threshold(self):
        with tempfile.TemporaryDirectory() as td:
            sid = "sess1234"
            for _ in range(2):
                check("Agent", {"prompt": _EXEC}, sid, td)
            r = check("Agent", {"prompt": _EXEC}, sid, td)
            assert r["count"] == 3
            assert r["level"] == "warning"

    def test_critical_at_threshold(self):
        with tempfile.TemporaryDirectory() as td:
            sid = "sess1234"
            for _ in range(3):
                check("Agent", {"prompt": _EXEC}, sid, td)
            r = check("Agent", {"prompt": _EXEC}, sid, td)
            assert r["count"] == 4
            assert r["level"] == "critical"

    def test_custom_thresholds(self):
        with tempfile.TemporaryDirectory() as td:
            sid = "sessAAAA"
            check("Agent", {"prompt": _EXEC}, sid, td, thresholds=[2, 4])
            r = check("Agent", {"prompt": _EXEC}, sid, td, thresholds=[2, 4])
            assert r["count"] == 2
            assert r["level"] == "warning"

    def test_misuse_read_hint(self):
        with tempfile.TemporaryDirectory() as td:
            p = "fix bug, read the file src/main.py, add tests, export"
            r = check("Agent", {"prompt": p}, "s1234567", td)
            assert any("Read tool" in h for h in r["hints"])

    def test_misuse_search_hint(self):
        with tempfile.TemporaryDirectory() as td:
            p = "modify and search for class Foo, add test, export"
            r = check("Agent", {"prompt": p}, "s1234567", td)
            assert any("Grep" in h for h in r["hints"])

    def test_explore_agent_search_no_hint(self):
        with tempfile.TemporaryDirectory() as td:
            r = check(
                "Agent",
                {"prompt": "search for patterns", "subagent_type": "Explore"},
                "s1234567", td,
            )
            assert not any("Grep" in h for h in r["hints"])

    def test_separate_sessions(self):
        with tempfile.TemporaryDirectory() as td:
            check("Agent", {"prompt": _EXEC}, "sessAAAA", td)
            check("Agent", {"prompt": _EXEC}, "sessAAAA", td)
            r = check("Agent", {"prompt": _EXEC}, "sessBBBB", td)
            assert r["count"] == 1  # Different session, fresh count


# ── check() — execution deny ────────────────────────────


class TestCheckDeny:
    def test_deny_when_over_max_spawns(self):
        with tempfile.TemporaryDirectory() as td:
            sid = "sess1234"
            for i in range(3):
                r = check("Agent", {"prompt": _EXEC}, sid, td, max_spawns=3)
                assert r["permissionDecision"] == "allow", f"spawn {i+1}"

            r = check("Agent", {"prompt": _EXEC}, sid, td, max_spawns=3)
            assert r["permissionDecision"] == "deny"
            assert r["count"] == 4
            assert r["level"] == "deny"
            assert "4/3" in r["reason"]
            assert r["agent_type"] == "execution"

    def test_deny_includes_hints(self):
        with tempfile.TemporaryDirectory() as td:
            sid = "sess1234"
            for _ in range(2):
                check("Agent", {"prompt": _EXEC}, sid, td, max_spawns=2)
            p = "edit the file foo.py, add test, export from index"
            r = check("Agent", {"prompt": p}, sid, td, max_spawns=2)
            assert r["permissionDecision"] == "deny"
            assert len(r["hints"]) > 0

    def test_default_max_spawns_is_4(self):
        with tempfile.TemporaryDirectory() as td:
            sid = "sessAAAA"
            for i in range(4):
                r = check("Agent", {"prompt": _EXEC}, sid, td)
                assert r["permissionDecision"] == "allow", f"spawn {i+1}"
            r = check("Agent", {"prompt": _EXEC}, sid, td)
            assert r["permissionDecision"] == "deny"

    def test_research_still_allowed_after_exec_cap(self):
        """Even after exec cap is hit, research agents pass through."""
        with tempfile.TemporaryDirectory() as td:
            sid = "sess1234"
            for _ in range(4):
                check("Agent", {"prompt": _EXEC}, sid, td, max_spawns=4)
            r = check("Agent", {"prompt": _EXEC}, sid, td, max_spawns=4)
            assert r["permissionDecision"] == "deny"
            r = check(
                "Agent",
                {"subagent_type": "Explore", "prompt": "find patterns"},
                sid, td, max_spawns=4,
            )
            assert r["permissionDecision"] == "allow"
            assert r["agent_type"] == "research"


# ── _check_prompt_quality() — delivery awareness gate ────


class TestPromptQuality:
    def test_research_agent_skipped(self):
        """Research agents bypass prompt quality check."""
        missing = _check_prompt_quality(
            {"subagent_type": "Explore", "prompt": "implement module"},
        )
        assert missing == []

    def test_non_code_task_skipped(self):
        """Non-code prompts bypass quality check."""
        missing = _check_prompt_quality({"prompt": "do stuff"})
        assert missing == []

    def test_code_with_all_keywords_passes(self):
        """Code task with test + export keywords passes."""
        missing = _check_prompt_quality({"prompt": _EXEC})
        assert len(missing) < 2

    def test_code_missing_both_denies(self):
        """Code task missing both test and export → deny."""
        missing = _check_prompt_quality(
            {"prompt": "implement a new feature for the system"},
        )
        assert len(missing) >= 2

    def test_code_missing_one_passes(self):
        """Code task missing only one dimension → allow."""
        missing = _check_prompt_quality(
            {"prompt": "implement feature with pytest coverage"},
        )
        assert len(missing) < 2

    def test_integrated_deny_via_check(self):
        """check() denies code task agent with no delivery keywords."""
        with tempfile.TemporaryDirectory() as td:
            r = check(
                "Agent",
                {"prompt": "implement a brand new module"},
                "sess1234", td,
            )
            assert r["permissionDecision"] == "deny"
            assert "missing" in r["reason"].lower()


# ── gate_agent_cap() — backward compat ─────────────────


class TestGateAgentCap:
    def test_non_agent_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            assert gate_agent_cap("Read", "sess1234", td) is None

    def test_no_session_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            assert gate_agent_cap("Agent", "", td) is None

    def test_no_counter_file_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            assert gate_agent_cap("Agent", "sess1234", td) is None

    def test_under_cap_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            sid = "sess1234"
            count_file = os.path.join(td, f"{sid[:8]}_agent_count")
            with open(count_file, "w") as f:
                f.write("3")
            assert gate_agent_cap("Agent", sid, td, max_spawns=4) is None

    def test_at_cap_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            sid = "sess1234"
            count_file = os.path.join(td, f"{sid[:8]}_agent_count")
            with open(count_file, "w") as f:
                f.write("4")
            assert gate_agent_cap("Agent", sid, td, max_spawns=4) is None

    def test_over_cap_denies(self):
        with tempfile.TemporaryDirectory() as td:
            sid = "sess1234"
            count_file = os.path.join(td, f"{sid[:8]}_agent_count")
            with open(count_file, "w") as f:
                f.write("5")
            r = gate_agent_cap("Agent", sid, td, max_spawns=4)
            assert r is not None
            assert r["permissionDecision"] == "deny"
            assert "5/4" in r["reason"]

    def test_custom_max_spawns(self):
        with tempfile.TemporaryDirectory() as td:
            sid = "sess1234"
            count_file = os.path.join(td, f"{sid[:8]}_agent_count")
            with open(count_file, "w") as f:
                f.write("4")
            r = gate_agent_cap("Agent", sid, td, max_spawns=3)
            assert r is not None
            assert r["permissionDecision"] == "deny"

    def test_corrupt_file_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            sid = "sess1234"
            count_file = os.path.join(td, f"{sid[:8]}_agent_count")
            with open(count_file, "w") as f:
                f.write("not_a_number")
            assert gate_agent_cap("Agent", sid, td) is None
