"""Tests for concinno.hook_api — HookResult + Pipeline public API."""

from __future__ import annotations

from concinno.hook_api import HookResult, Pipeline, _normalize

# ── HookResult ────────────────────────────────────────────


class TestHookResult:
    def test_deny(self):
        r = HookResult.deny("bad command")
        assert r.denied
        assert not r.allowed
        assert r.to_dict() == {"permissionDecision": "deny", "reason": "bad command"}

    def test_allow(self):
        r = HookResult.allow()
        assert r.allowed
        assert not r.denied
        assert r.to_dict() == {"permissionDecision": "allow"}

    def test_allow_with_context(self):
        r = HookResult.allow("some info")
        assert r.to_dict() == {
            "permissionDecision": "allow",
            "additionalContext": "some info",
        }

    def test_warn(self):
        r = HookResult.warn("check this")
        assert r.allowed
        assert r.to_dict() == {
            "permissionDecision": "allow",
            "additionalContext": "check this",
        }

    def test_deny_with_context(self):
        r = HookResult.deny("reason", context="extra")
        assert r.to_dict() == {
            "permissionDecision": "deny",
            "reason": "reason",
            "additionalContext": "extra",
        }

    def test_frozen(self):
        r = HookResult.deny("x")
        try:
            r.decision = "allow"  # type: ignore[misc]
            assert False, "Should raise"
        except AttributeError:
            pass


# ── _normalize ────────────────────────────────────────────


class TestNormalize:
    def test_none(self):
        assert _normalize(None) is None

    def test_hook_result_passthrough(self):
        r = HookResult.deny("x")
        assert _normalize(r) is r

    def test_dict_deny(self):
        r = _normalize({"permissionDecision": "deny", "reason": "bad"})
        assert r is not None
        assert r.denied
        assert r.reason == "bad"

    def test_dict_allow(self):
        r = _normalize({"permissionDecision": "allow", "additionalContext": "info"})
        assert r is not None
        assert r.allowed
        assert r.context == "info"

    def test_string_becomes_warn(self):
        r = _normalize("warning text")
        assert r is not None
        assert r.allowed
        assert r.context == "warning text"

    def test_unknown_type(self):
        assert _normalize(42) is None


# ── Pipeline ──────────────────────────────────────────────


class TestPipeline:
    def test_empty_pipeline_allows(self):
        p = Pipeline()
        result = p.run("Bash", {"command": "ls"})
        assert result["permissionDecision"] == "allow"

    def test_deny_guard_blocks(self):
        def blocker(tool_name, tool_input, **ctx):
            return HookResult.deny("blocked")

        p = Pipeline()
        p.add_deny_guard("blocker", blocker)
        result = p.run("Bash", {"command": "rm -rf /"})
        assert result["permissionDecision"] == "deny"
        assert result["reason"] == "blocked"

    def test_deny_short_circuits(self):
        calls = []

        def first(tool_name, tool_input, **ctx):
            calls.append("first")
            return HookResult.deny("stop here")

        def second(tool_name, tool_input, **ctx):
            calls.append("second")
            return HookResult.deny("never reached")

        p = Pipeline()
        p.add_deny_guard("first", first)
        p.add_deny_guard("second", second)
        p.run("Bash", {})
        assert calls == ["first"]

    def test_none_return_skipped(self):
        def noop(tool_name, tool_input, **ctx):
            return None

        def blocker(tool_name, tool_input, **ctx):
            return HookResult.deny("got here")

        p = Pipeline()
        p.add_deny_guard("noop", noop)
        p.add_deny_guard("blocker", blocker)
        result = p.run("Bash", {})
        assert result["permissionDecision"] == "deny"

    def test_warn_guards_collect(self):
        def warn1(tool_name, tool_input, **ctx):
            return HookResult.warn("warning 1")

        def warn2(tool_name, tool_input, **ctx):
            return "warning 2"

        p = Pipeline()
        p.add_warn_guard("w1", warn1)
        p.add_warn_guard("w2", warn2)
        result = p.run("Edit", {})
        assert result["permissionDecision"] == "allow"
        assert "warning 1" in result["additionalContext"]
        assert "warning 2" in result["additionalContext"]

    def test_dict_guard_compat(self):
        """Guards returning plain dicts (existing modules) work."""
        def dict_guard(tool_name, tool_input, **ctx):
            return {"permissionDecision": "deny", "reason": "old style"}

        p = Pipeline()
        p.add_deny_guard("old", dict_guard)
        result = p.run("Bash", {})
        assert result["permissionDecision"] == "deny"

    def test_exception_fail_open(self):
        def crasher(tool_name, tool_input, **ctx):
            raise RuntimeError("boom")

        def after(tool_name, tool_input, **ctx):
            return HookResult.deny("after crash")

        p = Pipeline()
        p.add_deny_guard("crasher", crasher)
        p.add_deny_guard("after", after)
        result = p.run("Bash", {})
        assert result["permissionDecision"] == "deny"
        assert result["reason"] == "after crash"

    def test_list_guards(self):
        p = Pipeline()
        p.add_deny_guard("a", lambda *a, **k: None)
        p.add_warn_guard("b", lambda *a, **k: None)
        assert p.list_guards() == {"deny": ["a"], "warn": ["b"]}

    def test_chaining(self):
        p = Pipeline()
        ret = p.add_deny_guard("a", lambda *a, **k: None)
        assert ret is p  # returns self for chaining

    def test_ctx_passed_to_guards(self):
        received = {}

        def guard(tool_name, tool_input, **ctx):
            received.update(ctx)
            return None

        p = Pipeline()
        p.add_deny_guard("g", guard)
        p.run("Bash", {}, session_id="abc", custom="val")
        assert received["session_id"] == "abc"
        assert received["custom"] == "val"

    def test_real_destruction_guard(self):
        """Integration: real destruction_guard works in pipeline."""
        from concinno.destruction_guard import evaluate

        p = Pipeline()
        p.add_deny_guard("destruction", evaluate)
        r = p.run("Bash", {"command": "rm -rf /"})
        assert r["permissionDecision"] != "allow"
