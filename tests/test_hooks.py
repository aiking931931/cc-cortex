"""Tests for cc-cortex hook templates — throttle, pre_tool, post_tool."""

from __future__ import annotations

import json

from cc_cortex.hooks.on_post_tool import _classify, _extract_streak_count, _throttle
from cc_cortex.hooks.on_pre_tool import main as pre_tool_main

# ─── Three-tier classification ───────────────────────────────────

class TestClassify:
    def test_critical_error(self):
        assert _classify("⚠ Error: ruff found 3 issues") == "CRITICAL"

    def test_critical_fix(self):
        assert _classify("修復: 已自動修正 2 個問題") == "CRITICAL"

    def test_critical_checkmark(self):
        """✅ messages are always CRITICAL so user always sees fix confirmations."""
        assert _classify("✅ foo.py fixed 1/1 | 🔥x3") == "CRITICAL"

    def test_critical_checkmark_no_fire(self):
        """✅ without 🔥 is still CRITICAL."""
        assert _classify("✅ foo.py fixed 1/1") == "CRITICAL"

    def test_milestone_streak(self):
        """Pure 🔥 without ✅ is MILESTONE (combo messages)."""
        assert _classify("🔥x50") == "MILESTONE"

    def test_info_normal(self):
        assert _classify("CodeGuard: all clean") == "INFO"

    def test_info_empty(self):
        assert _classify("") == "INFO"


class TestExtractStreak:
    def test_normal(self):
        assert _extract_streak_count("🔥x50 連擊！") == 50

    def test_no_match(self):
        assert _extract_streak_count("no streak here") == 0

    def test_x100(self):
        assert _extract_streak_count("🔥x100 milestone!") == 100


class TestThrottle:
    def test_critical_always_passes(self):
        lines = ["⚠ Error: something broke"]
        result = _throttle(lines)
        assert len(result) == 2  # message + note
        assert "[SHOW USER VERBATIM]" in result[0]

    def test_fix_with_checkmark_always_passes(self):
        """✅ fix messages are CRITICAL — always shown to user."""
        lines = ["✅ foo.py fixed 1/1 | 🔥x3"]
        result = _throttle(lines)
        assert len(result) == 2  # message + note
        assert "[SHOW USER VERBATIM]" in result[0]
        assert "foo.py" in result[0]

    def test_milestone_at_interval(self):
        lines = ["🔥x10"]
        result = _throttle(lines)
        assert len(result) == 1
        assert "[SHOW USER VERBATIM]" in result[0]

    def test_milestone_skipped(self):
        lines = ["🔥x3"]  # 3 is not a multiple of 5
        result = _throttle(lines)
        assert len(result) == 0

    def test_milestone_named_25(self):
        lines = ["🔥x25"]
        result = _throttle(lines)
        assert len(result) == 1

    def test_info_passes_through(self):
        lines = ["CodeGuard: all clean"]
        result = _throttle(lines)
        assert result == ["CodeGuard: all clean"]

    def test_mixed(self):
        lines = [
            "⚠ Error: ruff issue",
            "🔥x7",  # not interval, skipped
            "CodeGuard: ok",
        ]
        result = _throttle(lines)
        assert len(result) == 3  # error(2 lines) + info(1)


# ─── Pre-tool hook ───────────────────────────────────────────────

class TestPreToolHook:
    def test_safe_command(self, capsys):
        pre_tool_main({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["permissionDecision"] == "allow"

    def test_destructive_blocked(self, capsys):
        pre_tool_main({"tool_name": "Bash", "tool_input": {"command": "rm -rf src/"}})
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["permissionDecision"] == "deny"

    def test_non_bash_allowed(self, capsys):
        pre_tool_main({"tool_name": "Read", "tool_input": {"file_path": "/etc/passwd"}})
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["permissionDecision"] == "allow"


    def test_destructive_command_denied_with_stderr(self, capsys):
        """Full integration: rm -rf triggers deny + Pipeline stderr marker."""
        pre_tool_main({"tool_name": "Bash", "tool_input": {"command": "rm -rf src/"}})
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["permissionDecision"] == "deny"
