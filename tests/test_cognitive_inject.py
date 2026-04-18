"""Tests for concinno.cognitive_inject — three-layer knowledge router."""

from __future__ import annotations

import os
import tempfile
from unittest.mock import patch

from concinno.cognitive_inject import (
    _build_index,
    _build_pointers,
    _build_summaries,
    _extract_task_keywords,
    _load_skill_index,
    build_cognitive_context,
    build_delivery_standards,
    build_rag_context,
    build_thinking_directives,
    route_knowledge,
)

# Verify route_knowledge is the semantic alias
assert route_knowledge is build_rag_context


class TestThinkingDirectives:
    def test_full_returns_all_layers(self):
        result = build_thinking_directives("full")
        assert "CP" in result  # L0
        assert "instinct" in result  # L1
        assert "Counterfactual" in result  # L2

    def test_minimal_returns_l0_only(self):
        result = build_thinking_directives("minimal")
        assert "CP" in result  # L0
        assert "instinct" not in result  # no L1
        assert "Counterfactual" not in result  # no L2

    def test_standard_returns_l0_l1(self):
        result = build_thinking_directives("standard")
        assert "CP" in result  # L0
        assert "instinct" in result  # L1
        assert "Counterfactual" not in result  # no L2

    def test_default_is_full(self):
        result = build_thinking_directives()
        assert "Counterfactual" in result
        assert "instinct" in result

    def test_l0_contains_key_rules(self):
        result = build_thinking_directives("minimal")
        assert "guess" in result.lower()  # anti-guessing
        assert "CP" in result  # CP value ranking

    def test_l1_contains_anti_bias(self):
        result = build_thinking_directives("standard")
        assert "instinct" in result  # anchoring bias
        assert "disprove" in result  # confirmation bias
        assert "direction" in result  # sunk cost
        assert "isn't" in result  # negative evidence
        assert "caused" in result  # causal confusion

    def test_l2_contains_advanced_cognition(self):
        result = build_thinking_directives("full")
        assert "Counterfactual" in result
        assert "Inversion" in result
        assert "drifting" in result  # meta-cognition check


class TestDeliveryStandards:
    def test_code_task_gets_standards(self):
        result = build_delivery_standards("implement a new module")
        assert result
        assert "W" in result

    def test_non_code_task_empty(self):
        result = build_delivery_standards("analyze the data")
        assert result == ""

    def test_chinese_code_keywords(self):
        result = build_delivery_standards("建立一個新的模組")
        assert result


class TestExtractKeywords:
    def test_extracts_words(self):
        kw = _extract_task_keywords("fix the auth module")
        assert "auth" in kw
        assert "the" not in kw

    def test_empty_prompt(self):
        assert _extract_task_keywords("") == []

    def test_max_20(self):
        long_prompt = " ".join(f"word{i}" for i in range(50))
        assert len(_extract_task_keywords(long_prompt)) <= 20


class TestBuildIndex:
    def test_empty_learnings(self):
        assert _build_index([], []) == ""

    def test_filters_low_count(self):
        items = [{"pattern_key": "low", "count": 1}]
        assert _build_index(items, []) == ""

    def test_includes_high_count(self):
        items = [{"pattern_key": "high-item", "count": 3}]
        result = _build_index(items, [])
        assert "high-item" in result
        assert "[3x]" in result

    def test_excludes_promoted(self):
        items = [{"pattern_key": "old", "count": 5, "promoted": True}]
        assert _build_index(items, []) == ""

    def test_sorts_by_count(self):
        items = [
            {"pattern_key": "low", "count": 2},
            {"pattern_key": "high", "count": 8},
        ]
        result = _build_index(items, [])
        assert result.index("high") < result.index("low")


class TestBuildSummaries:
    def test_no_keywords_empty(self):
        items = [{"pattern_key": "x", "correction_text": "y", "count": 3}]
        assert _build_summaries(items, []) == ""

    def test_matching_keyword(self):
        items = [
            {
                "pattern_key": "auth-fix",
                "correction_text": "always check auth token",
                "count": 3,
            },
        ]
        result = _build_summaries(items, ["auth"])
        assert "auth" in result.lower()

    def test_no_match(self):
        items = [
            {
                "pattern_key": "deploy",
                "correction_text": "deploy carefully",
                "count": 3,
            },
        ]
        result = _build_summaries(items, ["unrelated"])
        assert result == ""


class TestRagContext:
    def test_no_learnings_empty(self):
        with patch(
            "concinno.cognitive_inject._load_learnings", return_value=[],
        ):
            result = build_rag_context("task", "/fake")
        assert result == ""

    def test_with_matching_learnings(self):
        learnings = [
            {
                "pattern_key": "test-pattern",
                "correction_text": "always write tests for auth",
                "count": 4,
            },
        ]
        with patch(
            "concinno.cognitive_inject._load_learnings",
            return_value=learnings,
        ):
            result = build_rag_context("fix auth module", "/fake")
        assert "test-pattern" in result


class TestBuildCognitiveContext:
    def test_parent_gets_full_cognition(self):
        """Parent session (no agent_type) gets L0+L1+L2."""
        with patch(
            "concinno.cognitive_inject._load_learnings", return_value=[],
        ):
            result = build_cognitive_context()
        assert "CP" in result  # L0
        assert "instinct" in result  # L1
        assert "Counterfactual" in result  # L2

    def test_research_subagent_gets_minimal(self):
        """Research subagent gets L0 only (minimal attention cost)."""
        with patch(
            "concinno.cognitive_inject._load_learnings", return_value=[],
        ):
            result = build_cognitive_context(agent_type="Explore")
        assert "CP" in result  # L0
        assert "instinct" not in result  # no L1
        assert "Counterfactual" not in result  # no L2

    def test_execution_subagent_gets_standard(self):
        """Execution subagent gets L0+L1 (anti-bias, no deep cognition)."""
        with patch(
            "concinno.cognitive_inject._load_learnings", return_value=[],
        ):
            result = build_cognitive_context(agent_type="general-purpose")
        assert "CP" in result  # L0
        assert "instinct" in result  # L1
        assert "Counterfactual" not in result  # L2 is parent-only

    def test_execution_subagent_gets_delivery(self):
        """Execution subagent gets delivery standards even without prompt."""
        with patch(
            "concinno.cognitive_inject._load_learnings", return_value=[],
        ):
            result = build_cognitive_context(agent_type="general-purpose")
        assert "dead code" in result or "wired" in result

    def test_code_task_includes_delivery(self):
        with patch(
            "concinno.cognitive_inject._load_learnings", return_value=[],
        ):
            result = build_cognitive_context(task_prompt="implement feature")
        assert "W" in result

    def test_empty_prompt_no_crash(self):
        with patch(
            "concinno.cognitive_inject._load_learnings", return_value=[],
        ):
            result = build_cognitive_context(task_prompt="", workspace="")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_with_workspace_and_learnings(self):
        learnings = [
            {
                "pattern_key": "rag-hit",
                "correction_text": "test rag integration",
                "count": 4,
            },
        ]
        with (
            patch(
                "concinno.cognitive_inject._load_learnings",
                return_value=learnings,
            ),
            tempfile.TemporaryDirectory() as td,
        ):
            result = build_cognitive_context(
                task_prompt="test the rag", workspace=td,
            )
        assert "rag-hit" in result


class TestLoadSkillIndex:
    def test_empty_dir(self):
        with tempfile.TemporaryDirectory() as td:
            assert _load_skill_index(td) == []

    def test_finds_skills(self):
        with tempfile.TemporaryDirectory() as td:
            skill_dir = os.path.join(td, ".claude", "skills", "kb_audio")
            os.makedirs(skill_dir)
            with open(
                os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8",
            ) as f:
                f.write(
                    "---\nname: kb_audio\n"
                    'description: Audio knowledge. Triggers on "audio".\n---\n',
                )
            result = _load_skill_index(td)
            assert len(result) == 1
            assert result[0]["name"] == "kb_audio"
            assert "audio" in result[0]["desc"].lower()


class TestBuildPointers:
    def test_no_keywords_empty(self):
        assert _build_pointers("/fake", [], None) == ""

    def test_matches_skill_by_description(self):
        with tempfile.TemporaryDirectory() as td:
            skill_dir = os.path.join(td, ".claude", "skills", "kb_deploy")
            os.makedirs(skill_dir)
            with open(
                os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8",
            ) as f:
                f.write(
                    "---\nname: kb_deploy\n"
                    'description: Deploy VPS SSH. Triggers on "deploy".\n---\n',
                )
            result = _build_pointers(td, ["deploy"], None)
            assert "kb_deploy" in result
            assert "Skill" in result

    def test_rag_domains_link_to_skills(self):
        """RAG hit domains should route to matching Skills."""
        with tempfile.TemporaryDirectory() as td:
            skill_dir = os.path.join(td, ".claude", "skills", "kb_audio")
            os.makedirs(skill_dir)
            with open(
                os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8",
            ) as f:
                f.write(
                    "---\nname: kb_audio\n"
                    'description: Audio STT TTS. Triggers on "audio".\n---\n',
                )
            # Task has no audio keywords, but RAG domain does
            result = _build_pointers(td, ["unrelated"], ["audio-fix"])
            assert "kb_audio" in result

    def test_no_match_empty(self):
        with tempfile.TemporaryDirectory() as td:
            skill_dir = os.path.join(td, ".claude", "skills", "kb_word")
            os.makedirs(skill_dir)
            with open(
                os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8",
            ) as f:
                f.write(
                    "---\nname: kb_word\n"
                    'description: Word docx. Triggers on "word".\n---\n',
                )
            result = _build_pointers(td, ["quantum"], None)
            assert result == ""


class TestThreeLayerRouting:
    """Test the full index→summary→pointer routing chain."""

    def test_rag_routes_to_skill(self):
        """RAG correction match should produce Skill pointer."""
        learnings = [
            {
                "pattern_key": "deploy-error",
                "correction_text": "always check deploy config",
                "count": 4,
            },
        ]
        with tempfile.TemporaryDirectory() as td:
            skill_dir = os.path.join(td, ".claude", "skills", "kb_deploy")
            os.makedirs(skill_dir)
            with open(
                os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8",
            ) as f:
                f.write(
                    "---\nname: kb_deploy\n"
                    'description: Deploy VPS. Triggers on "deploy".\n---\n',
                )
            with patch(
                "concinno.cognitive_inject._load_learnings",
                return_value=learnings,
            ):
                result = build_rag_context("fix deploy issue", td)
            # All three layers should be present
            assert "deploy-error" in result  # Layer 1: index
            assert "deploy config" in result  # Layer 2: summary
            assert "kb_deploy" in result  # Layer 3: pointer

    def test_no_rag_hit_no_skill_pointer(self):
        """Without RAG match, no Skill pointer either."""
        learnings = [
            {
                "pattern_key": "auth-bug",
                "correction_text": "auth token check",
                "count": 3,
            },
        ]
        with tempfile.TemporaryDirectory() as td:
            skill_dir = os.path.join(td, ".claude", "skills", "kb_deploy")
            os.makedirs(skill_dir)
            with open(
                os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8",
            ) as f:
                f.write(
                    "---\nname: kb_deploy\n"
                    'description: Deploy VPS. Triggers on "deploy".\n---\n',
                )
            with patch(
                "concinno.cognitive_inject._load_learnings",
                return_value=learnings,
            ):
                # Task about "quantum" — no match to auth or deploy
                result = build_rag_context("quantum physics", td)
            assert "kb_deploy" not in result

    def test_l0_contains_no_guessing_rule(self):
        """L0 should contain the anti-guessing rule."""
        result = build_thinking_directives("minimal")
        assert "guess" in result.lower()


class TestCognitivePoolIntegration:
    """2.7.0 F1 regression — pool inject wired into build_cognitive_context.

    Before 2.7.0 the import at line 446 targeted a module that did not
    exist, so the ``try/except Exception`` swallowed the ImportError
    and the entire cross-session pool was unreachable from subagent
    injection. These three tests prove the happy path, the empty-pool
    path, and the crash-fail-open path now all go through
    ``cognitive_pool_inject.build_pool_context``.
    """

    def test_pool_injects_when_sections_available(self):
        """Happy path: a pool with one section surfaces in the context."""
        import time
        from concinno.cache.cognitive_pool import CognitivePool

        with tempfile.TemporaryDirectory() as td:
            pool = CognitivePool(root=td)
            pool.upsert_section(
                title="session.blockers",
                body="Wire pool inject before 2.7.0 ships.",
                tags=("pin",),
                now=time.time(),
            )
            # Patch the lazy-imported class at its source module so the
            # default constructor inside build_pool_context hands back
            # our temp-rooted pool instead of the real ~/.claude one.
            with patch(
                "concinno.cognitive_inject._load_learnings",
                return_value=[],
            ), patch(
                "concinno.cache.cognitive_pool.CognitivePool",
                return_value=pool,
            ):
                result = build_cognitive_context(
                    task_prompt="pool inject wire",
                    workspace="",
                    agent_type="Explore",
                )
        assert "session.blockers" in result, \
            f"Pool section missing from inject output:\n{result}"

    def test_pool_empty_returns_no_pool_block(self):
        """Empty pool: inject still succeeds, just omits the pool block."""
        from concinno.cache.cognitive_pool import CognitivePool

        with tempfile.TemporaryDirectory() as td:
            pool = CognitivePool(root=td)
            with patch(
                "concinno.cognitive_inject._load_learnings",
                return_value=[],
            ), patch(
                "concinno.cache.cognitive_pool.CognitivePool",
                return_value=pool,
            ):
                result = build_cognitive_context(
                    task_prompt="anything",
                    workspace="",
                    agent_type="Explore",
                )
        # Thinking directives always ship — just no pool marker.
        assert "Cross-session pool" not in result
        assert "CP" in result  # L0 directives present — fail-open works.

    def test_pool_crash_is_fail_open(self):
        """Pool load blows up → inject still returns a usable context."""

        class _Broken:
            def read_all(self):
                raise RuntimeError("simulated pool corruption")

        with patch(
            "concinno.cognitive_inject._load_learnings",
            return_value=[],
        ), patch(
            "concinno.cache.cognitive_pool.CognitivePool",
            return_value=_Broken(),
        ):
            result = build_cognitive_context(
                task_prompt="x",
                workspace="",
                agent_type="Explore",
            )
        # Must NOT raise; must NOT include a pool block; MUST return
        # core thinking directives.
        assert "Cross-session pool" not in result
        assert "CP" in result
