"""Tests for concinno.subagent_identity — dynamic subagent identity assignment."""

from __future__ import annotations

import pytest

from concinno.subagent_identity import (
    Identity,
    IdentityProfile,
    assign_identity,
    build_identity_context,
    get_profile,
)

# ── assign_identity: agent_type hard map ─────────────────


class TestAgentTypeMapping:
    """Agent type → identity hard mapping."""

    def test_explore_becomes_inquirer(self):
        p = assign_identity(agent_type="Explore")
        assert p.identity == Identity.INQUIRER

    def test_plan_becomes_architect(self):
        p = assign_identity(agent_type="Plan")
        assert p.identity == Identity.ARCHITECT

    def test_claude_code_guide_becomes_recorder(self):
        p = assign_identity(agent_type="claude-code-guide")
        assert p.identity == Identity.RECORDER

    def test_statusline_setup_becomes_engineer(self):
        p = assign_identity(agent_type="statusline-setup")
        assert p.identity == Identity.ENGINEER

    def test_agent_type_takes_priority_over_keywords(self):
        """Even if prompt says 'fix bug', Explore → Inquirer."""
        p = assign_identity(agent_type="Explore", task_prompt="fix the bug in auth")
        assert p.identity == Identity.INQUIRER


# ── assign_identity: keyword matching ────────────────────


class TestKeywordMatching:
    """general-purpose subagent: keyword → identity."""

    @pytest.mark.parametrize("prompt,expected", [
        ("fix the authentication bug", Identity.SURGEON),
        ("debug the memory leak", Identity.SURGEON),
        ("troubleshoot the failing test", Identity.SURGEON),
        ("修復登入問題", Identity.SURGEON),
    ])
    def test_debug_keywords_become_surgeon(self, prompt, expected):
        p = assign_identity(agent_type="general-purpose", task_prompt=prompt)
        assert p.identity == expected

    @pytest.mark.parametrize("prompt,expected", [
        ("design the new API architecture", Identity.ARCHITECT),
        ("refactor the module structure", Identity.ARCHITECT),
        ("plan the migration strategy", Identity.ARCHITECT),
        ("架構設計", Identity.ARCHITECT),
    ])
    def test_design_keywords_become_architect(self, prompt, expected):
        p = assign_identity(agent_type="general-purpose", task_prompt=prompt)
        assert p.identity == expected

    @pytest.mark.parametrize("prompt,expected", [
        ("research the best approach for caching", Identity.INQUIRER),
        ("analyze the performance bottleneck", Identity.INQUIRER),
        ("investigate why tests are slow", Identity.INQUIRER),
        ("研究快取策略", Identity.INQUIRER),
    ])
    def test_research_keywords_become_inquirer(self, prompt, expected):
        p = assign_identity(agent_type="general-purpose", task_prompt=prompt)
        assert p.identity == expected

    @pytest.mark.parametrize("prompt,expected", [
        ("document the API endpoints", Identity.RECORDER),
        ("write the handoff notes", Identity.RECORDER),
        ("交接記錄", Identity.RECORDER),
    ])
    def test_doc_keywords_become_recorder(self, prompt, expected):
        p = assign_identity(agent_type="general-purpose", task_prompt=prompt)
        assert p.identity == expected

    @pytest.mark.parametrize("prompt,expected", [
        ("deploy to production", Identity.ENGINEER),
        ("setup the CI pipeline", Identity.ENGINEER),
        ("部署到雲端", Identity.ENGINEER),
    ])
    def test_ops_keywords_become_engineer(self, prompt, expected):
        p = assign_identity(agent_type="general-purpose", task_prompt=prompt)
        assert p.identity == expected

    @pytest.mark.parametrize("prompt,expected", [
        ("implement the new feature", Identity.CRAFTSMAN),
        ("create a module for auth", Identity.CRAFTSMAN),
        ("write tests for the guard", Identity.CRAFTSMAN),
        ("新增元件", Identity.CRAFTSMAN),
    ])
    def test_code_keywords_become_craftsman(self, prompt, expected):
        p = assign_identity(agent_type="general-purpose", task_prompt=prompt)
        assert p.identity == expected


# ── assign_identity: defaults ────────────────────────────


class TestDefaults:
    """Fallback when no match."""

    def test_empty_defaults_to_engineer(self):
        p = assign_identity()
        assert p.identity == Identity.ENGINEER

    def test_unknown_agent_type_no_prompt_defaults_engineer(self):
        p = assign_identity(agent_type="unknown-type")
        assert p.identity == Identity.ENGINEER

    def test_gibberish_prompt_defaults_engineer(self):
        p = assign_identity(task_prompt="xyz qwerty 12345")
        assert p.identity == Identity.ENGINEER


# ── keyword priority (specificity order) ─────────────────


class TestPriority:
    """More specific keywords win over broad ones."""

    def test_fix_wins_over_implement(self):
        """'fix' (Surgeon) should win over 'implement' (Craftsman)."""
        p = assign_identity(task_prompt="fix and implement the auth module")
        assert p.identity == Identity.SURGEON

    def test_debug_wins_over_create(self):
        p = assign_identity(task_prompt="debug the create user flow")
        assert p.identity == Identity.SURGEON


# ── build_identity_context ───────────────────────────────


class TestBuildContext:
    """Identity injection string."""

    def test_contains_label(self):
        p = assign_identity(agent_type="Explore")
        ctx = build_identity_context(p)
        assert "Logic Inquirer" in ctx

    def test_contains_directive(self):
        p = assign_identity(agent_type="Plan")
        ctx = build_identity_context(p)
        assert "Architect" in ctx
        assert "three-layer" in ctx.lower() or "trade-off" in ctx.lower()

    def test_starts_with_emoji(self):
        p = assign_identity()
        ctx = build_identity_context(p)
        assert ctx.startswith("🎭")


# ── cognition_depth ──────────────────────────────────────


class TestCognitionDepth:
    """Each identity has correct cognition depth."""

    @pytest.mark.parametrize("identity,expected_depth", [
        (Identity.CRAFTSMAN, "full"),
        (Identity.ARCHITECT, "full"),
        (Identity.INQUIRER, "full"),
        (Identity.SURGEON, "standard"),
        (Identity.ENGINEER, "standard"),
        (Identity.RECORDER, "minimal"),
    ])
    def test_depth_per_identity(self, identity, expected_depth):
        p = get_profile(identity)
        assert p.cognition_depth == expected_depth


# ── IdentityProfile dataclass ────────────────────────────


class TestIdentityProfile:
    """Dataclass behavior."""

    def test_frozen(self):
        p = get_profile(Identity.CRAFTSMAN)
        with pytest.raises(AttributeError):
            p.label = "hacked"  # type: ignore[misc]

    def test_all_identities_have_profiles(self):
        for ident in Identity:
            p = get_profile(ident)
            assert isinstance(p, IdentityProfile)
            assert p.identity == ident
            assert p.label
            assert p.directive
            assert p.cognition_depth in {"minimal", "standard", "full"}
