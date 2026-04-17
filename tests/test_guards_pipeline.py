"""Tests for guards.pipeline — GuardPipeline execution engine."""

from __future__ import annotations

import os
import tempfile

import pytest

from concinno.guards.base import (
    BaseGuard,
    GuardCategory,
    GuardContext,
    GuardResult,
)
from concinno.guards.pipeline import GuardPipeline

# ── Test Helpers ─────────────────────────────────────────────


def _ctx(**kwargs) -> GuardContext:
    data = {"tool_name": "Bash", "tool_input": {"command": "ls"}}
    data.update(kwargs)
    return GuardContext.from_hook_data(data)


class AllowGuard(BaseGuard):
    name = "allow_guard"
    category = GuardCategory.QUALITY

    def __init__(self, context: str = ""):
        self._context = context

    def check(self, ctx):
        if self._context:
            return GuardResult.allow(context=self._context)
        return None


class DenyGuard(BaseGuard):
    name = "deny_guard"
    category = GuardCategory.QUALITY

    def __init__(self, reason: str = "blocked"):
        self._reason = reason

    def check(self, ctx):
        return GuardResult.deny(reason=self._reason)


class CrashGuard(BaseGuard):
    name = "crash_guard"
    category = GuardCategory.QUALITY

    def check(self, ctx):
        raise RuntimeError("boom")


class PostToolGuard(BaseGuard):
    name = "post_guard"
    category = GuardCategory.QUALITY

    def __init__(self, context: str = "lint ok"):
        self._context = context

    def check(self, ctx):
        return None

    def on_post_tool(self, ctx):
        return GuardResult.allow(context=self._context)


class StopGuard(BaseGuard):
    name = "stop_guard"
    category = GuardCategory.QUALITY

    def check(self, ctx):
        return None

    def on_stop(self, ctx):
        return GuardResult.allow(context="delivery check")


# ── Registration ─────────────────────────────────────────────


class TestRegistration:
    def test_register_returns_self(self):
        pipe = GuardPipeline()
        result = pipe.register(AllowGuard())
        assert result is pipe

    def test_register_non_baseguard_raises(self):
        pipe = GuardPipeline()
        with pytest.raises(TypeError, match="BaseGuard"):
            pipe.register("not a guard")

    def test_register_empty_name_raises(self):
        class NoName(BaseGuard):
            name = ""
            category = GuardCategory.QUALITY
            def check(self, ctx):
                return None

        pipe = GuardPipeline()
        with pytest.raises(ValueError, match="name is empty"):
            pipe.register(NoName())

    def test_auto_sort_by_category(self):
        """Guards auto-sort: Security before Quality before Cognitive."""
        class SecGuard(BaseGuard):
            name = "sec"
            category = GuardCategory.SECURITY
            def check(self, ctx):
                return None

        class CogGuard(BaseGuard):
            name = "cog"
            category = GuardCategory.COGNITIVE
            def check(self, ctx):
                return None

        pipe = GuardPipeline()
        pipe.register(CogGuard())
        pipe.register(AllowGuard())
        pipe.register(SecGuard())

        guards = pipe.list_guards()
        assert list(guards.keys()) == ["security", "quality", "cognitive"]

    def test_chain_registration(self):
        pipe = GuardPipeline()
        pipe.register(AllowGuard()).register(DenyGuard())
        assert pipe.guard_count == 2


# ── PreToolUse ───────────────────────────────────────────────


class TestRunPreTool:
    def test_empty_pipeline_allows(self):
        pipe = GuardPipeline()
        result = pipe.run_pre_tool(_ctx())
        assert result["permissionDecision"] == "allow"

    def test_all_pass_allows(self):
        pipe = GuardPipeline()
        pipe.register(AllowGuard())
        result = pipe.run_pre_tool(_ctx())
        assert result["permissionDecision"] == "allow"

    def test_deny_short_circuits(self):
        """First DENY wins, subsequent guards not called."""
        pipe = GuardPipeline()
        pipe.register(DenyGuard(reason="first deny"))
        pipe.register(AllowGuard(context="should not appear"))
        result = pipe.run_pre_tool(_ctx())
        assert result["permissionDecision"] == "deny"
        assert "first deny" in result.get("reason", "")

    def test_context_collected(self):
        pipe = GuardPipeline()
        pipe.register(AllowGuard(context="hint A"))
        result = pipe.run_pre_tool(_ctx())
        assert "hint A" in result.get("additionalContext", "")

    def test_multiple_contexts_joined(self):
        class Allow2(AllowGuard):
            name = "allow2"

        pipe = GuardPipeline()
        pipe.register(AllowGuard(context="ctx1"))
        pipe.register(Allow2(context="ctx2"))
        result = pipe.run_pre_tool(_ctx())
        ctx = result.get("additionalContext", "")
        assert "ctx1" in ctx
        assert "ctx2" in ctx

    def test_no_context_when_empty(self):
        pipe = GuardPipeline()
        pipe.register(AllowGuard())
        result = pipe.run_pre_tool(_ctx())
        assert "additionalContext" not in result


# ── Fail Policy ──────────────────────────────────────────────


class SecurityCrashGuard(BaseGuard):
    """SECURITY guard that crashes — used to test fail-closed."""

    name = "security_crash"
    category = GuardCategory.SECURITY

    def check(self, ctx):
        raise RuntimeError("security boom")


class TestFailPolicy:
    def test_quality_crash_fail_open(self):
        """QUALITY guard crash → skip, don't block user."""
        pipe = GuardPipeline()
        pipe.register(CrashGuard())
        result = pipe.run_pre_tool(_ctx())
        assert result["permissionDecision"] == "allow"

    def test_quality_crash_before_deny(self):
        """Crash guard skipped, deny guard still runs."""
        pipe = GuardPipeline()
        pipe.register(CrashGuard())
        pipe.register(DenyGuard())
        result = pipe.run_pre_tool(_ctx())
        assert result["permissionDecision"] == "deny"

    def test_security_crash_fail_closed(self):
        """SECURITY guard crash → deny (fail-closed)."""
        pipe = GuardPipeline()
        pipe.register(SecurityCrashGuard())
        result = pipe.run_pre_tool(_ctx())
        assert result["permissionDecision"] == "deny"
        assert "security_crash" in result.get("reason", "")

    def test_security_crash_blocks_subsequent_guards(self):
        """SECURITY crash short-circuits — subsequent guards don't run."""
        pipe = GuardPipeline()
        pipe.register(SecurityCrashGuard())
        pipe.register(AllowGuard())  # should never run
        result = pipe.run_pre_tool(_ctx())
        assert result["permissionDecision"] == "deny"

    def test_security_crash_records_failure(self):
        """SECURITY crash still records health failure."""
        pipe = GuardPipeline()
        pipe.register(SecurityCrashGuard())
        pipe.run_pre_tool(_ctx())
        assert pipe._health.get("security_crash", 0) >= 1


# ── Health Tracking ──────────────────────────────────────────


class TestHealthTracking:
    def test_auto_disable_after_max_failures(self):
        pipe = GuardPipeline(max_failures=2)
        pipe.register(CrashGuard())

        # First 2 calls: crash guard crashes but pipeline allows
        pipe.run_pre_tool(_ctx())
        pipe.run_pre_tool(_ctx())

        # After 2 failures, guard is auto-disabled
        assert pipe._health.get("crash_guard", 0) >= 2

    def test_health_persistence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "health.json")

            # Save health
            pipe1 = GuardPipeline()
            pipe1._health = {"bad_guard": 5}
            pipe1.save_health(path)

            # Load health
            pipe2 = GuardPipeline()
            pipe2.load_health(path)
            assert pipe2._health == {"bad_guard": 5}

    def test_load_health_missing_file(self):
        pipe = GuardPipeline()
        pipe.load_health("/nonexistent/path.json")
        assert pipe._health == {}

    def test_success_resets_health(self):
        pipe = GuardPipeline()
        pipe._health = {"allow_guard": 3}
        pipe.register(AllowGuard())
        pipe.run_pre_tool(_ctx())
        assert "allow_guard" not in pipe._health


# ── PostToolUse ──────────────────────────────────────────────


class TestRunPostTool:
    def test_empty_pipeline(self):
        pipe = GuardPipeline()
        result = pipe.run_post_tool(_ctx())
        assert result == {}

    def test_collects_post_tool_context(self):
        pipe = GuardPipeline()
        pipe.register(PostToolGuard(context="ruff clean"))
        result = pipe.run_post_tool(_ctx())
        assert "ruff clean" in result.get("additionalContext", "")

    def test_crash_in_post_tool_skipped(self):
        class CrashPost(BaseGuard):
            name = "crash_post"
            category = GuardCategory.QUALITY
            def check(self, ctx):
                return None
            def on_post_tool(self, ctx):
                raise RuntimeError("post boom")

        pipe = GuardPipeline()
        pipe.register(CrashPost())
        result = pipe.run_post_tool(_ctx())
        assert result == {} or "additionalContext" not in result


# ── Stop ─────────────────────────────────────────────────────


class TestRunStop:
    def test_collects_stop_context(self):
        pipe = GuardPipeline()
        pipe.register(StopGuard())
        result = pipe.run_stop(_ctx())
        assert "delivery check" in result.get("additionalContext", "")


# ── Introspection ────────────────────────────────────────────


class TestIntrospection:
    def test_list_guards(self):
        pipe = GuardPipeline()
        pipe.register(AllowGuard())
        guards = pipe.list_guards()
        assert "quality" in guards
        assert "allow_guard" in guards["quality"]

    def test_guard_count(self):
        pipe = GuardPipeline()
        assert pipe.guard_count == 0
        pipe.register(AllowGuard())
        assert pipe.guard_count == 1

    def test_repr(self):
        pipe = GuardPipeline()
        pipe.register(AllowGuard())
        r = repr(pipe)
        assert "GuardPipeline" in r
        assert "quality=1" in r
