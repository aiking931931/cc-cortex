"""Tests for concinno.confidence_gate — ConfidenceGate."""

from __future__ import annotations

from concinno.confidence_gate import (
    ConfidenceGate,
    detect_uncertainty,
    is_irreversible,
)
from concinno.guards.base import GuardContext


def _ctx(
    tool_name, tool_input, cache_dir,
    hook_event="PreToolUse", result="",
):
    return GuardContext(
        tool_name=tool_name,
        tool_input=tool_input,
        session_id="test-session",
        cache_dir=cache_dir,
        hook_event=hook_event,
        tool_result=result,
    )


# ── detect_uncertainty ───────────────────────────────────────


class TestDetectUncertainty:
    def test_zh_markers(self):
        m = detect_uncertainty("這可能是 bug，或許需要重構")
        assert "可能" in m
        assert "或許" in m

    def test_en_markers(self):
        m = detect_uncertainty("I think this might work, but not sure")
        assert any("think" in x.lower() for x in m)
        assert any("might" in x.lower() for x in m)
        assert any("not sure" in x.lower() for x in m)

    def test_no_markers(self):
        assert detect_uncertainty("This is a definitive fix.") == []

    def test_empty(self):
        assert detect_uncertainty("") == []

    def test_mixed_zh_en(self):
        m = detect_uncertainty("maybe 這大概可以")
        assert len(m) >= 2


# ── is_irreversible ──────────────────────────────────────────


class TestIsIrreversible:
    def test_read_tools(self):
        assert is_irreversible("Read", {}) is False
        assert is_irreversible("Grep", {}) is False
        assert is_irreversible("Glob", {}) is False

    def test_edit_reversible(self):
        assert is_irreversible("Edit", {"old_string": "a"}) is False

    def test_bash_rm(self):
        assert is_irreversible("Bash", {"command": "rm -rf /tmp/x"}) is True

    def test_bash_git_push(self):
        assert is_irreversible(
            "Bash", {"command": "git push origin main"},
        ) is True

    def test_bash_git_reset_hard(self):
        assert is_irreversible(
            "Bash", {"command": "git reset --hard HEAD~1"},
        ) is True

    def test_bash_safe_command(self):
        assert is_irreversible(
            "Bash", {"command": "ls -la"},
        ) is False

    def test_bash_drop_table(self):
        assert is_irreversible(
            "Bash", {"command": "psql -c 'DROP TABLE users'"},
        ) is True

    def test_write_not_irreversible(self):
        assert is_irreversible("Write", {"file_path": "x.py"}) is False


# ── ConfidenceGate integration ───────────────────────────────


class TestConfidenceGate:
    def test_no_cache_dir(self):
        gate = ConfidenceGate()
        ctx = _ctx("Bash", {"command": "rm -rf x"}, "")
        assert gate.check(ctx) is None

    def test_reversible_always_passes(self, tmp_path):
        gate = ConfidenceGate()
        # Seed uncertainty
        ctx_post = _ctx(
            "Read", {}, str(tmp_path),
            hook_event="PostToolUse",
            result="I think this might be wrong",
        )
        gate.on_post_tool(ctx_post)

        # Edit should pass even with uncertainty
        ctx_pre = _ctx(
            "Edit",
            {"old_string": "a", "new_string": "b"},
            str(tmp_path),
        )
        assert gate.check(ctx_pre) is None

    def test_deny_uncertain_plus_irreversible(self, tmp_path):
        gate = ConfidenceGate()
        # Seed uncertainty via PostToolUse
        ctx_post = _ctx(
            "Read", {}, str(tmp_path),
            hook_event="PostToolUse",
            result="maybe this is the right file, not sure",
        )
        gate.on_post_tool(ctx_post)

        # Now try irreversible
        ctx_pre = _ctx(
            "Bash",
            {"command": "rm -rf old_module/"},
            str(tmp_path),
        )
        result = gate.check(ctx_pre)
        assert result is not None
        assert result.action.value == "deny"
        ctx_lower = result.context.lower()
        assert (
            "confidence" in ctx_lower
            or "verify" in ctx_lower
            or "查證" in result.context
        )

    def test_allow_when_no_uncertainty(self, tmp_path):
        gate = ConfidenceGate()
        ctx = _ctx(
            "Bash",
            {"command": "rm -rf old/"},
            str(tmp_path),
        )
        assert gate.check(ctx) is None

    def test_uncertainty_decays(self, tmp_path):
        gate = ConfidenceGate(decay_calls=2)
        # Seed uncertainty
        ctx_post = _ctx(
            "Read", {}, str(tmp_path),
            hook_event="PostToolUse",
            result="或許可以這樣做",
        )
        gate.on_post_tool(ctx_post)

        rm_input = {"command": "rm -rf x"}

        # First call: deny
        r1 = gate.check(_ctx("Bash", rm_input, str(tmp_path)))
        assert r1 is not None

        # Second call: deny (calls_since=1, threshold=2)
        r2 = gate.check(_ctx("Bash", rm_input, str(tmp_path)))
        assert r2 is not None

        # Third call: decayed (calls_since=2 >= threshold=2)
        r3 = gate.check(_ctx("Bash", rm_input, str(tmp_path)))
        assert r3 is None

    def test_post_tool_no_result(self, tmp_path):
        gate = ConfidenceGate()
        ctx = _ctx("Read", {}, str(tmp_path),
                    hook_event="PostToolUse", result="")
        assert gate.on_post_tool(ctx) is None

    def test_guard_metadata(self):
        gate = ConfidenceGate()
        assert gate.name == "confidence_gate"
        assert gate.category.value == 2  # QUALITY
        assert gate.step_back_reason != ""

    def test_post_tool_no_cache(self):
        gate = ConfidenceGate()
        ctx = _ctx("Read", {}, "",
                    hook_event="PostToolUse", result="maybe wrong")
        assert gate.on_post_tool(ctx) is None
