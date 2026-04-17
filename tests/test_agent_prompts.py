"""Tests for ``concinno.agent.prompts``."""

from __future__ import annotations

from concinno.agent.prompts import (
    AGENT_GUIDANCE_ARITHMETIC,
    AGENT_GUIDANCE_NO_REFUSAL,
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
