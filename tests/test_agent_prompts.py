"""Tests for ``concinno.agent.prompts``."""

from __future__ import annotations

from concinno.agent.prompts import (
    AGENT_GUIDANCE_ARITHMETIC,
    AGENT_GUIDANCE_COMPUTE_TOOLS,
    AGENT_GUIDANCE_EXACT_QUOTE,
    AGENT_GUIDANCE_NO_REFUSAL,
    AGENT_GUIDANCE_SEARCH_DISCIPLINE,
    AGENT_GUIDANCE_UNCERTAINTY,
    default_guidance,
)


class TestGuidanceConstants:
    def test_uncertainty_mentions_tools(self) -> None:
        s = AGENT_GUIDANCE_UNCERTAINTY
        assert "web_search" in s
        assert "fetch_url" in s

    def test_arithmetic_mentions_run_bash(self) -> None:
        assert "run_bash" in AGENT_GUIDANCE_ARITHMETIC
        assert "python3" in AGENT_GUIDANCE_ARITHMETIC

    def test_no_refusal_lists_bad_phrases(self) -> None:
        s = AGENT_GUIDANCE_NO_REFUSAL
        assert "I cannot" in s
        assert "I am unable" in s
        assert "Once I have access" in s

    def test_default_guidance_joins_all_three(self) -> None:
        out = default_guidance()
        assert AGENT_GUIDANCE_UNCERTAINTY in out
        assert AGENT_GUIDANCE_ARITHMETIC in out
        assert AGENT_GUIDANCE_NO_REFUSAL in out

    def test_default_guidance_stable(self) -> None:
        assert default_guidance() == default_guidance()


class TestComputeToolsGuidance:
    """Few-shot ICL for the ``date_calc`` / ``python_exec`` builtins.

    The purpose is to teach weak models (Gemma-4) when to select
    these tools and how to shape the call. We assert both tool
    names plus at least one concrete invocation example so a
    regression that drops the few-shot layer fails loudly.
    """

    def test_mentions_both_tool_names(self) -> None:
        s = AGENT_GUIDANCE_COMPUTE_TOOLS
        assert "date_calc" in s
        assert "python_exec" in s

    def test_has_date_calc_few_shot_example(self) -> None:
        s = AGENT_GUIDANCE_COMPUTE_TOOLS
        assert 'op="delta"' in s
        assert 'date_from=' in s and 'date_to=' in s
        assert '"%B %d, %Y"' in s

    def test_has_python_exec_few_shot_example(self) -> None:
        s = AGENT_GUIDANCE_COMPUTE_TOOLS
        assert 'code="sum(' in s
        assert 'round(' in s

    def test_warns_about_expression_only_constraint(self) -> None:
        s = AGENT_GUIDANCE_COMPUTE_TOOLS
        assert "pure expressions only" in s
        assert "no import" in s
        assert "no assignment" in s

    def test_default_guidance_does_not_include_compute_tools(self) -> None:
        assert AGENT_GUIDANCE_COMPUTE_TOOLS not in default_guidance()

    def test_distinct_from_arithmetic_guidance(self) -> None:
        assert AGENT_GUIDANCE_COMPUTE_TOOLS != AGENT_GUIDANCE_ARITHMETIC


class TestPhase1Guidance:
    """Phase 1 GAIA additions: multi-source search + verbatim quotes.

    These guidance blocks target the B/C/G/I FAIL classes from the
    baseline N=20 per-task analysis: search noise, empty-retry, and
    paraphrase-vs-verbatim format mismatches.
    """

    def test_search_discipline_requires_multi_query(self) -> None:
        s = AGENT_GUIDANCE_SEARCH_DISCIPLINE
        assert "web_search" in s
        assert "2 different" in s
        assert "cross-reference" in s

    def test_search_discipline_has_retry_and_cap(self) -> None:
        s = AGENT_GUIDANCE_SEARCH_DISCIPLINE
        assert "reformulate" in s
        assert "5 web_search" in s

    def test_exact_quote_demands_verbatim(self) -> None:
        s = AGENT_GUIDANCE_EXACT_QUOTE
        assert "fetch_url" in s
        assert "EXACT" in s
        assert "Never paraphrase" in s

    def test_phase1_blocks_not_in_default(self) -> None:
        out = default_guidance()
        assert AGENT_GUIDANCE_SEARCH_DISCIPLINE not in out
        assert AGENT_GUIDANCE_EXACT_QUOTE not in out
