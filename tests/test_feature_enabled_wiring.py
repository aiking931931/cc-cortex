"""Regression tests for 2.7.0 F2 — FEATURE_META.enabled wiring.

Before 2.7.0, `FEATURE_META` had 33 entries but only 5-6 of them
consulted ``cfg.feature(name, "enabled")`` at runtime. Users who ran
``concinno config set boundary_guard enabled false`` saw the JSON
update and the guard keep running.

2.7.0 routes enabled flags through two sinks:

1. **Centralized pipeline dispatch** — :class:`Pipeline._feature_enabled`
   looks up ``guard.feature_name or guard.name`` in cc_config.json and
   skips the guard when ``enabled`` is false. Every BaseGuard subclass
   benefits at once.
2. **Hook-level direct calls** — features that gate module functions
   (clarity_gate, prompt_guard, insight_engine, streak_ux,
   session_summary, delivery_gate, bash_background_gate, python_c_gate)
   read cfg.feature directly at their hook entry points.

These tests pin the invariant: every listed feature's ``enabled=false``
setting stops the guard from running. Breaking any of these silently
takes us back to the 2.6.x ghost-config bug.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from concinno.guards.base import (
    BaseGuard,
    GuardCategory,
    GuardContext,
    GuardResult,
)
from concinno.guards.pipeline import GuardPipeline as Pipeline

# ── Centralized pipeline dispatch ─────────────────────────────


class _ShoutyGuard(BaseGuard):
    """Guard that always DENIES — easy to observe skip vs run."""

    name = "_test_shouty"
    category = GuardCategory.SECURITY  # SECURITY bypasses circuit-breaker cooldown (test isolation)

    def check(self, ctx: GuardContext) -> GuardResult | None:
        return GuardResult.deny("shouty fired", context="")

    def on_post_tool(self, ctx: GuardContext) -> GuardResult | None:
        return GuardResult.allow(context="shouty post")

    def on_stop(self, ctx: GuardContext) -> GuardResult | None:
        return GuardResult.allow(context="shouty stop")


class _AliasedGuard(BaseGuard):
    """Guard whose ``feature_name`` diverges from ``name``.

    Mirrors real-world divergence like
    ``ReadFirstGuard.name = "read_first"`` vs
    ``FEATURE_META["read_first_gate"]``.
    """

    name = "_test_alias_guard"
    feature_name = "_test_alias_feature"
    category = GuardCategory.SECURITY  # SECURITY bypasses circuit-breaker cooldown (test isolation)

    def check(self, ctx: GuardContext) -> GuardResult | None:
        return GuardResult.deny("alias fired", context="")


def _ctx() -> GuardContext:
    return GuardContext(
        tool_name="Edit",
        tool_input={"file_path": "a.py"},
        session_id="sid",
        cache_dir="/tmp",
        hook_event="PreToolUse",
    )


def _cfg_stub(features: dict[str, dict[str, Any]]):
    """Build a MagicMock config whose ``feature()`` returns the mapping."""

    def _feature(name: str, key: str = "enabled") -> Any:
        feat = features.get(name, {})
        if key == "enabled":
            return feat.get("enabled", True)
        return feat.get(key)

    cfg = MagicMock()
    cfg.feature.side_effect = _feature
    return cfg


def test_pipeline_skips_guard_when_feature_disabled():
    """enabled=false → guard.check() is never called."""
    p = Pipeline()
    guard = _ShoutyGuard()
    p.register(guard)
    cfg = _cfg_stub({"_test_shouty": {"enabled": False}})
    with patch("concinno.core.config.get_config", return_value=cfg):
        result = p.run_pre_tool(_ctx())
    assert result.get("permissionDecision") == "allow", result


def test_pipeline_runs_guard_when_feature_enabled():
    """enabled=true (default) → guard fires as normal."""
    p = Pipeline()
    guard = _ShoutyGuard()
    p.register(guard)
    cfg = _cfg_stub({"_test_shouty": {"enabled": True}})
    with patch("concinno.core.config.get_config", return_value=cfg):
        result = p.run_pre_tool(_ctx())
    assert result.get("permissionDecision") == "deny"


def test_pipeline_feature_name_override():
    """feature_name on guard → pipeline looks up that key, not name."""
    p = Pipeline()
    guard = _AliasedGuard()
    p.register(guard)
    # Disable the feature key, leave the name key ENABLED. If the
    # override is broken the guard will still fire.
    cfg = _cfg_stub(
        {
            "_test_alias_feature": {"enabled": False},
            "_test_alias_guard": {"enabled": True},
        },
    )
    with patch("concinno.core.config.get_config", return_value=cfg):
        result = p.run_pre_tool(_ctx())
    assert result.get("permissionDecision") == "allow"


def test_pipeline_defaults_true_when_feature_absent():
    """Feature key absent from cc_config → treat as enabled."""
    p = Pipeline()
    guard = _ShoutyGuard()
    p.register(guard)
    cfg = _cfg_stub({})  # no entries at all
    with patch("concinno.core.config.get_config", return_value=cfg):
        result = p.run_pre_tool(_ctx())
    assert result.get("permissionDecision") == "deny"


def test_pipeline_post_tool_respects_feature_flag():
    """enabled=false also skips on_post_tool callbacks."""
    p = Pipeline()
    guard = _ShoutyGuard()
    p.register(guard)
    cfg = _cfg_stub({"_test_shouty": {"enabled": False}})
    with patch("concinno.core.config.get_config", return_value=cfg):
        ctx = GuardContext(
            tool_name="Edit", tool_input={}, session_id="s",
            cache_dir="/tmp", hook_event="PostToolUse",
        )
        result = p.run_post_tool(ctx)
    # No context emitted because on_post_tool() never ran.
    assert "shouty post" not in result.get("additionalContext", "")


def test_pipeline_stop_respects_feature_flag():
    """enabled=false also skips on_stop callbacks."""
    p = Pipeline()
    guard = _ShoutyGuard()
    p.register(guard)
    cfg = _cfg_stub({"_test_shouty": {"enabled": False}})
    with patch("concinno.core.config.get_config", return_value=cfg):
        ctx = GuardContext(
            tool_name="", tool_input={}, session_id="s",
            cache_dir="/tmp", hook_event="Stop",
        )
        result = p.run_stop(ctx)
    assert "shouty stop" not in result.get("additionalContext", "")


def test_pipeline_failopen_when_cfg_crashes():
    """cfg.feature() raising → ``_feature_enabled`` treats guard as enabled.

    We assert at the predicate level rather than running the whole
    pipeline: the equilibrium breaker is cache-backed and can trip on
    shared state from prior tests, which would mask the fail-open
    contract with an unrelated ALLOW. Testing the predicate directly
    pins the real invariant we care about.
    """
    p = Pipeline()
    guard = _ShoutyGuard()
    p.register(guard)
    bad_cfg = MagicMock()
    bad_cfg.feature.side_effect = RuntimeError("cc_config.json missing")
    with patch("concinno.core.config.get_config", return_value=bad_cfg):
        # Both calls must fail-open: enabled (True) and active (True).
        assert p._feature_enabled(guard) is True
        assert p._is_guard_active(guard) is True


# ── Divergent feature_name on real guards ─────────────────────
#
# These pin the actual concinno guards where name != feature key.
# If someone renames a guard class without updating feature_name,
# the pipeline dispatch silently starts consulting the wrong key.


def test_read_first_guard_uses_read_first_gate_feature():
    from concinno.pre_tool_guards import ReadFirstGuard

    assert ReadFirstGuard.feature_name == "read_first_gate"


def test_lint_guard_uses_linting_feature():
    from concinno.linting import LintGuard

    assert LintGuard.feature_name == "linting"


def test_hijack_guard_uses_hijack_gate_feature():
    from concinno.sentinel import HijackGuard

    assert HijackGuard.feature_name == "hijack_gate"


def test_consecutive_fail_guard_uses_consecutive_fail_gate_feature():
    from concinno.sentinel import ConsecutiveFailGuard

    assert ConsecutiveFailGuard.feature_name == "consecutive_fail_gate"


def test_sentinel_guard_uses_sentinel_gate_feature():
    from concinno.sentinel import SentinelGuard

    assert SentinelGuard.feature_name == "sentinel_gate"


def test_prompt_injection_guard_uses_prompt_guard_feature():
    from concinno.prompt_injection_guard import PromptInjectionGuard

    assert PromptInjectionGuard.feature_name == "prompt_guard"


def test_agent_gate_uses_agent_cap_feature():
    from concinno.agent_gate import AgentGateGuard

    assert AgentGateGuard.feature_name == "agent_cap"


def test_handoff_guard_uses_handoff_format_feature():
    from concinno.handoff_validator import HandoffGuard

    assert HandoffGuard.feature_name == "handoff_format"


# ── Hook-level wiring sanity ──────────────────────────────────
#
# Three representative hook-level features. We don't re-test every
# one — the goal is to pin the pattern: hook function reads
# cfg.feature(name, "enabled") at entry and bails when false.


def test_streak_ux_respects_feature_flag(tmp_path):
    """on_post_tool._run_streak_ux skips when streak_ux.enabled = false."""
    from concinno.hooks import on_post_tool

    fragments: list[str] = []
    cfg = _cfg_stub({"streak_ux": {"enabled": False}})
    with patch("concinno.core.config.get_config", return_value=cfg):
        on_post_tool._run_streak_ux(
            tool_name="Edit",
            tool_input={"file_path": "x.py"},
            guard_ctx="",
            hook_data={"session_id": "s"},
            fragments=fragments,
        )
    # With streak_ux disabled we MUST NOT append anything.
    assert fragments == []


def test_session_summary_respects_feature_flag():
    """_session_summary returns early when session_summary disabled."""
    from concinno.hooks import on_stop

    cfg = _cfg_stub({"session_summary": {"enabled": False}})
    # Also guard against handoff_engine generating output.
    with patch(
        "concinno.core.config.get_config", return_value=cfg,
    ), patch(
        "concinno.handoff_engine.generate_session_summary",
        return_value="SHOULD_NOT_APPEAR",
    ) as gen:
        on_stop._session_summary({"session_id": "s"})
        assert gen.call_count == 0, \
            "generate_session_summary must not be called when feature disabled"


def test_auto_delivery_respects_feature_flag():
    """_build_auto_delivery skips auto_delivery_gate when disabled."""
    from concinno.hooks import on_stop

    run = on_stop._build_auto_delivery({"session_id": "s"})
    cfg = _cfg_stub({"delivery_gate": {"enabled": False}})
    with patch(
        "concinno.core.config.get_config", return_value=cfg,
    ), patch(
        "concinno.delivery.auto_delivery_gate",
        return_value="SHOULD_NOT_APPEAR",
    ) as mock_gate:
        assert run() is None
        assert mock_gate.call_count == 0
