"""Tests for guards.base — GuardAction, GuardCategory, GuardContext, GuardResult, BaseGuard."""

from __future__ import annotations

import pytest

from concinno.guards.base import (
    BaseGuard,
    GuardAction,
    GuardCategory,
    GuardContext,
    GuardResult,
    _extract_target_path,
)

# ── GuardAction ──────────────────────────────────────────────


class TestGuardAction:
    def test_values(self):
        assert GuardAction.ALLOW.value == "allow"
        assert GuardAction.DENY.value == "deny"

    def test_no_warn(self):
        """No WARN/ADVISE — soft warnings are negative ROI."""
        names = [a.name for a in GuardAction]
        assert "WARN" not in names
        assert "ADVISE" not in names


# ── GuardCategory ────────────────────────────────────────────


class TestGuardCategory:
    def test_order(self):
        """Security < Quality < Cognitive (execution order)."""
        assert GuardCategory.SECURITY.value < GuardCategory.QUALITY.value
        assert GuardCategory.QUALITY.value < GuardCategory.COGNITIVE.value

    def test_three_layers_only(self):
        assert len(GuardCategory) == 3


# ── GuardContext ─────────────────────────────────────────────


class TestGuardContext:
    def test_from_hook_data_basic(self):
        ctx = GuardContext.from_hook_data({
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "session_id": "test-123",
        })
        assert ctx.tool_name == "Bash"
        assert ctx.tool_input == {"command": "ls"}
        assert ctx.session_id == "test-123"

    def test_from_hook_data_empty(self):
        ctx = GuardContext.from_hook_data({})
        assert ctx.tool_name == ""
        assert ctx.tool_input == {}

    def test_from_hook_data_none(self):
        ctx = GuardContext.from_hook_data(None)
        assert ctx.tool_name == ""

    def test_from_hook_data_invalid_tool_input(self):
        ctx = GuardContext.from_hook_data({"tool_input": "not a dict"})
        assert ctx.tool_input == {}

    def test_frozen(self):
        ctx = GuardContext.from_hook_data({"tool_name": "Read"})
        with pytest.raises(AttributeError):
            ctx.tool_name = "Write"


# ── GuardResult ──────────────────────────────────────────────


class TestGuardResult:
    def test_allow_factory(self):
        r = GuardResult.allow(context="helpful info")
        assert r.action == GuardAction.ALLOW
        assert r.context == "helpful info"
        assert r.reason == ""

    def test_deny_factory(self):
        r = GuardResult.deny(reason="dangerous", context="details")
        assert r.action == GuardAction.DENY
        assert r.reason == "dangerous"
        assert r.context == "details"

    def test_to_hook_dict_allow(self):
        r = GuardResult.allow(context="ctx")
        d = r.to_hook_dict()
        assert d["permissionDecision"] == "allow"
        assert d["additionalContext"] == "ctx"

    def test_to_hook_dict_deny(self):
        r = GuardResult.deny(reason="bad", context="more")
        d = r.to_hook_dict()
        assert d["permissionDecision"] == "deny"
        assert d["reason"] == "bad"

    def test_to_hook_dict_allow_no_context(self):
        r = GuardResult.allow()
        d = r.to_hook_dict()
        assert d == {"permissionDecision": "allow"}

    def test_frozen(self):
        r = GuardResult.allow()
        with pytest.raises(AttributeError):
            r.action = GuardAction.DENY


# ── BaseGuard ABC ────────────────────────────────────────────


class TestBaseGuard:
    def test_cannot_instantiate_abstract(self):
        with pytest.raises(TypeError):
            BaseGuard()

    def test_concrete_guard(self):
        class MyGuard(BaseGuard):
            name = "test_guard"
            category = GuardCategory.QUALITY

            def check(self, ctx):
                return None

        g = MyGuard()
        assert g.name == "test_guard"
        assert g.category == GuardCategory.QUALITY
        assert g.step_back_reason == ""

    def test_on_post_tool_default_none(self):
        class MyGuard(BaseGuard):
            name = "g"
            category = GuardCategory.QUALITY
            def check(self, ctx):
                return None

        g = MyGuard()
        ctx = GuardContext.from_hook_data({})
        assert g.on_post_tool(ctx) is None

    def test_on_stop_default_none(self):
        class MyGuard(BaseGuard):
            name = "g"
            category = GuardCategory.QUALITY
            def check(self, ctx):
                return None

        g = MyGuard()
        ctx = GuardContext.from_hook_data({})
        assert g.on_stop(ctx) is None


# ── _extract_target_path ────────────────────────────────────


class TestExtractTargetPath:
    def test_file_path_from_read(self):
        ctx = GuardContext.from_hook_data({
            "tool_name": "Read",
            "tool_input": {"file_path": "src/api/handler.ts"},
        })
        assert _extract_target_path(ctx) == "src/api/handler.ts"

    def test_file_path_from_write(self):
        ctx = GuardContext.from_hook_data({
            "tool_name": "Write",
            "tool_input": {"file_path": "src/utils/helper.py"},
        })
        assert _extract_target_path(ctx) == "src/utils/helper.py"

    def test_path_from_grep(self):
        ctx = GuardContext.from_hook_data({
            "tool_name": "Grep",
            "tool_input": {"pattern": "TODO", "path": "src/"},
        })
        assert _extract_target_path(ctx) == "src/"

    def test_bash_extracts_path(self):
        ctx = GuardContext.from_hook_data({
            "tool_name": "Bash",
            "tool_input": {"command": "cat src/main.py"},
        })
        assert _extract_target_path(ctx) == "src/main.py"

    def test_no_path_returns_empty(self):
        ctx = GuardContext.from_hook_data({
            "tool_name": "Bash",
            "tool_input": {"command": "echo hello"},
        })
        assert _extract_target_path(ctx) == ""


# ── Path-scoped guards ──────────────────────────────────────


class TestPathScope:
    def _make_guard(self, scopes):
        class ScopedGuard(BaseGuard):
            name = "scoped"
            category = GuardCategory.QUALITY
            path_scope = scopes

            def check(self, ctx):
                return GuardResult.deny("blocked")

        return ScopedGuard()

    def test_empty_scope_always_matches(self):
        g = self._make_guard([])
        ctx = GuardContext.from_hook_data({
            "tool_name": "Read",
            "tool_input": {"file_path": "anything.py"},
        })
        assert g.matches_path_scope(ctx) is True

    def test_matching_glob(self):
        g = self._make_guard(["src/api/**/*.ts"])
        ctx = GuardContext.from_hook_data({
            "tool_name": "Edit",
            "tool_input": {"file_path": "src/api/handlers/auth.ts"},
        })
        assert g.matches_path_scope(ctx) is True

    def test_non_matching_glob(self):
        g = self._make_guard(["src/api/**/*.ts"])
        ctx = GuardContext.from_hook_data({
            "tool_name": "Edit",
            "tool_input": {"file_path": "src/utils/helper.py"},
        })
        assert g.matches_path_scope(ctx) is False

    def test_multiple_scopes_any_match(self):
        g = self._make_guard(["src/api/*", "src/handlers/*"])
        ctx = GuardContext.from_hook_data({
            "tool_name": "Read",
            "tool_input": {"file_path": "src/handlers/auth.py"},
        })
        assert g.matches_path_scope(ctx) is True

    def test_no_path_info_defaults_to_match(self):
        g = self._make_guard(["src/**"])
        ctx = GuardContext.from_hook_data({
            "tool_name": "Bash",
            "tool_input": {"command": "echo hello"},
        })
        assert g.matches_path_scope(ctx) is True

    def test_windows_path_normalized(self):
        g = self._make_guard(["src/api/*"])
        ctx = GuardContext.from_hook_data({
            "tool_name": "Read",
            "tool_input": {"file_path": "src\\api\\handler.ts"},
        })
        assert g.matches_path_scope(ctx) is True
