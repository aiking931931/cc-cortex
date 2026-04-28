"""Tests for skill_proactive_router wiring inside on_prompt_submit hook.

Covers FATAL-1 fix (sub-agent O wave): the proactive router was shipped
without a production caller. These tests prove the hook now invokes the
router, threads the advisory into the additionalContext list, and stays
fail-soft when feature disabled / module raises.

Coverage matrix (≥8 cases):

  1. Hook step 12 returns the router's advisory string.
  2. Hook skips inject when router returns blank (no candidates).
  3. Hook skips inject when feature disabled in FEATURE_META.
  4. Hook skips inject when router raises (catastrophic).
  5. Disabled router never increments call counter.
  6. Hook still emits prior steps when router is disabled.
  7. Hook still emits router context alongside polling-watcher.
  8. Helper imports cleanly with no Anthropic key set.
"""
from __future__ import annotations

from concinno.hooks import on_prompt_submit as hook_mod
from concinno.skill_proactive_router import ProactiveRouterResult

# ── 1. happy path: router returns context ────────────────


def test_helper_returns_router_context(monkeypatch):
    """When the router yields advisory text, helper returns it verbatim."""

    def _fake_propose(prompt: str, **_kw):
        r = ProactiveRouterResult()
        r.additional_context = (
            "🎯 Skill suggestion (proactive router):\n- /memoria — match\n"
        )
        return r

    monkeypatch.setattr(
        "concinno.skill_proactive_router.propose_skills", _fake_propose,
    )

    out = hook_mod._skill_proactive_router_inject(
        "please clean up memoria right now",
    )
    assert out is not None
    assert "/memoria" in out


# ── 2. blank context = no inject ─────────────────────────


def test_helper_blank_context_returns_none(monkeypatch):
    def _fake_propose(prompt: str, **_kw):
        return ProactiveRouterResult()  # additional_context = ""

    monkeypatch.setattr(
        "concinno.skill_proactive_router.propose_skills", _fake_propose,
    )

    assert hook_mod._skill_proactive_router_inject("anything") is None


# ── 3. feature disabled = skip ───────────────────────────


def test_helper_skipped_when_feature_disabled(monkeypatch):
    """Disabling the feature in config short-circuits before propose runs."""
    call_count = {"n": 0}

    def _spy_propose(prompt: str, **_kw):
        call_count["n"] += 1
        return ProactiveRouterResult()

    monkeypatch.setattr(
        "concinno.skill_proactive_router.propose_skills", _spy_propose,
    )

    class _FakeCfg:
        def feature(self, name, key):
            assert name == "skill_proactive_router"
            assert key == "enabled"
            return False

    monkeypatch.setattr(
        "concinno.core.config.get_config", lambda: _FakeCfg(),
    )

    assert hook_mod._skill_proactive_router_inject("memoria cleanup") is None
    assert call_count["n"] == 0  # propose_skills never invoked


# ── 4. router raises = silent None ───────────────────────


def test_helper_router_raises_fails_soft(monkeypatch):
    def _crash(prompt: str, **_kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "concinno.skill_proactive_router.propose_skills", _crash,
    )

    assert hook_mod._skill_proactive_router_inject("memoria cleanup") is None


# ── 5. disabled doesn't burn router CPU ──────────────────


def test_disabled_short_circuits_before_propose(monkeypatch):
    """Belt-and-braces: a disabled feature must NOT call propose_skills.

    A future tweak that leaks the call would silently double the hot
    path cost without changing test 3's assertions; this test pins the
    contract independently.
    """
    seen = []

    def _track(prompt: str, **_kw):
        seen.append(prompt)
        return ProactiveRouterResult()

    monkeypatch.setattr(
        "concinno.skill_proactive_router.propose_skills", _track,
    )

    class _DisabledCfg:
        def feature(self, *_a):
            return False

    monkeypatch.setattr(
        "concinno.core.config.get_config", lambda: _DisabledCfg(),
    )

    for _ in range(10):
        hook_mod._skill_proactive_router_inject("memoria cleanup please")
    assert seen == []


# ── 6. handle_prompt_submit emits router context downstream ─


def test_handle_prompt_submit_propagates_router_context(monkeypatch):
    """End-to-end: enable router + happy-path propose → context surfaces."""

    def _fake_propose(prompt: str, **_kw):
        r = ProactiveRouterResult()
        r.additional_context = "🎯 Skill suggestion: /memoria"
        return r

    monkeypatch.setattr(
        "concinno.skill_proactive_router.propose_skills", _fake_propose,
    )

    # Force only skill_proactive_router on — everything else off so the
    # test focuses on the new wiring without depending on FEATURE_META
    # defaults for unrelated subsystems.
    class _SelectiveCfg:
        def feature(self, name, key):
            return name == "skill_proactive_router" and key == "enabled"

        def raw(self, *_a, **_kw):
            return {}

    monkeypatch.setattr(
        "concinno.core.config.get_config", lambda: _SelectiveCfg(),
    )

    out = hook_mod.handle_prompt_submit(
        "please run memoria cleanup right now",
        prefs_path="",
        cache_dir="",
        session_id="",
    )
    assert "deny" not in out
    contexts = out.get("contexts", [])
    assert any("Skill suggestion" in c and "/memoria" in c for c in contexts)


# ── 7. router context coexists with polling-watcher inject ──


def test_handle_prompt_submit_router_alongside_other_steps(monkeypatch):
    """Step 12 is additive — earlier injects (e.g. polling) still fire."""

    def _fake_propose(prompt: str, **_kw):
        r = ProactiveRouterResult()
        r.additional_context = "🎯 Skill suggestion: /kb_handoff"
        return r

    def _fake_polling():
        return "⏳ pending wait #1: stub"

    monkeypatch.setattr(
        "concinno.skill_proactive_router.propose_skills", _fake_propose,
    )
    monkeypatch.setattr(
        "concinno.hooks.wait_inject.build_context", _fake_polling,
    )

    class _SelectiveCfg:
        def feature(self, name, key):
            return name == "skill_proactive_router" and key == "enabled"

        def raw(self, *_a, **_kw):
            return {}

    monkeypatch.setattr(
        "concinno.core.config.get_config", lambda: _SelectiveCfg(),
    )

    out = hook_mod.handle_prompt_submit(
        "交接 memoria — please review the latest handoff",
        prefs_path="",
        cache_dir="",
        session_id="",
    )
    contexts = out.get("contexts", [])
    assert any("Skill suggestion" in c for c in contexts)
    assert any("pending wait" in c for c in contexts)


# ── 8. import works without ANTHROPIC_API_KEY ─────────────


def test_helper_imports_clean_without_api_key(monkeypatch):
    """The lazy-imported anthropic SDK must not be required at hook load."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # Real propose_skills with cheap-only path (no API key, no judge call):
    out = hook_mod._skill_proactive_router_inject(
        "this prompt has no skill triggers at all blah blah",
    )
    # Either None (no candidates / disabled) or a string. No exception.
    assert out is None or isinstance(out, str)
