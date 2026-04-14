"""Tests for initial_intent_probe — Probe user's root purpose on first write."""

from cc_cortex.core.state_store import StateStore
from cc_cortex.guards.base import GuardAction, GuardContext
from cc_cortex.initial_intent_probe import InitialIntentProbe


def _ctx(
    tmp_path,
    tool_name="Edit",
    tool_input=None,
    *,
    hook_event="PreToolUse",
    tool_result="",
    session_id="test-session",
):
    return GuardContext(
        tool_name=tool_name,
        tool_input=tool_input or {"file_path": "test.py", "new_string": "content"},
        session_id=session_id,
        cache_dir=str(tmp_path),
        hook_event=hook_event,
        tool_result=tool_result,
        workspace="",
    )


# ── Simple tasks skip ─────────────────────────────────────────────


class TestSimpleTaskSkip:
    def test_simple_task_returns_none(self, tmp_path):
        guard = InitialIntentProbe()
        store = StateStore(str(tmp_path))
        store.write("c0_route", "test-session", {"complexity": "simple"})
        ctx = _ctx(tmp_path)
        result = guard.check(ctx)
        assert result is None

    def test_simple_task_sets_probe_done(self, tmp_path):
        guard = InitialIntentProbe()
        store = StateStore(str(tmp_path))
        store.write("c0_route", "test-session", {"complexity": "simple"})
        guard.check(_ctx(tmp_path))
        state = store.read("intent_probe", "test-session", default={})
        assert state.get("probe_done") is True
        assert state.get("skip_reason") == "simple_task"


# ── Complicated tasks trigger probe ──────────────────────────────


class TestComplicatedProbe:
    def test_complicated_injects_probe(self, tmp_path):
        guard = InitialIntentProbe()
        store = StateStore(str(tmp_path))
        store.write("c0_route", "test-session", {"complexity": "complicated"})
        ctx = _ctx(tmp_path)
        result = guard.check(ctx)
        assert result is not None
        assert result.action == GuardAction.ALLOW
        assert "ROOT PURPOSE" in result.context
        assert "不迎合" not in result.context  # Only for complex+

    def test_probe_fires_once_per_session(self, tmp_path):
        guard = InitialIntentProbe()
        store = StateStore(str(tmp_path))
        store.write("c0_route", "test-session", {"complexity": "complicated"})

        # First call — injects probe
        result1 = guard.check(_ctx(tmp_path))
        assert result1 is not None

        # Second call — already probed, returns None
        result2 = guard.check(_ctx(tmp_path))
        assert result2 is None


# ── Complex tasks get honesty reminder ───────────────────────────


class TestComplexHonestyReminder:
    def test_complex_includes_honesty(self, tmp_path):
        guard = InitialIntentProbe()
        store = StateStore(str(tmp_path))
        store.write("c0_route", "test-session", {"complexity": "complex"})
        ctx = _ctx(tmp_path)
        result = guard.check(ctx)
        assert result is not None
        assert "不迎合" in result.context
        assert "問錯問題" in result.context

    def test_chaotic_includes_honesty(self, tmp_path):
        guard = InitialIntentProbe()
        store = StateStore(str(tmp_path))
        store.write("c0_route", "test-session", {"complexity": "chaotic"})
        ctx = _ctx(tmp_path)
        result = guard.check(ctx)
        assert result is not None
        assert "不迎合" in result.context


# ── Non-write tools skip ─────────────────────────────────────────


class TestNonWriteToolsSkip:
    def test_read_tool_returns_none(self, tmp_path):
        guard = InitialIntentProbe()
        ctx = _ctx(tmp_path, tool_name="Read", tool_input={"file_path": "x.py"})
        assert guard.check(ctx) is None

    def test_grep_tool_returns_none(self, tmp_path):
        guard = InitialIntentProbe()
        ctx = _ctx(tmp_path, tool_name="Grep", tool_input={"pattern": "x"})
        assert guard.check(ctx) is None

    def test_glob_tool_returns_none(self, tmp_path):
        guard = InitialIntentProbe()
        ctx = _ctx(tmp_path, tool_name="Glob", tool_input={"pattern": "*.py"})
        assert guard.check(ctx) is None


# ── Edge cases ───────────────────────────────────────────────────


class TestEdgeCases:
    def test_no_cache_dir_returns_none(self):
        guard = InitialIntentProbe()
        ctx = GuardContext(
            tool_name="Edit",
            tool_input={"file_path": "x.py", "new_string": "y"},
            session_id="s",
            cache_dir="",
            hook_event="PreToolUse",
        )
        assert guard.check(ctx) is None

    def test_default_complexity_is_complicated(self, tmp_path):
        """When C0 route has no state, default to complicated (triggers probe)."""
        guard = InitialIntentProbe()
        # No c0_route state written — defaults to "complicated"
        ctx = _ctx(tmp_path)
        result = guard.check(ctx)
        assert result is not None
        assert result.action == GuardAction.ALLOW

    def test_bash_triggers_probe(self, tmp_path):
        guard = InitialIntentProbe()
        store = StateStore(str(tmp_path))
        store.write("c0_route", "test-session", {"complexity": "complicated"})
        ctx = _ctx(tmp_path, tool_name="Bash", tool_input={"command": "echo hi"})
        result = guard.check(ctx)
        assert result is not None

    def test_on_post_tool_returns_none(self, tmp_path):
        guard = InitialIntentProbe()
        ctx = _ctx(tmp_path, hook_event="PostToolUse")
        assert guard.on_post_tool(ctx) is None
