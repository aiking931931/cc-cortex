"""Tests for gaia_agent._extract_answer last-match + markdown-skip logic.

Origin: GAIA 8f80e01c bass clef — model solved correctly in reasoning
and emitted ``FINAL ANSWER: 90`` at the tail, but an earlier markdown
section header ``**Step 8 — FINAL ANSWER:**`` was captured as an empty
first match, so the pre-fix regex returned ``""`` and failed the task.
"""

from __future__ import annotations

import sys

import pytest


@pytest.fixture
def gaia_agent_mod(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "test-dummy")
    sys.modules.pop("concinno.skills.public.agent.gaia_agent", None)
    from concinno.skills.public.agent import gaia_agent
    return gaia_agent


def test_extract_answer_simple(gaia_agent_mod):
    assert gaia_agent_mod._extract_answer("FINAL ANSWER: 42") == "42"


def test_extract_answer_takes_last_match(gaia_agent_mod):
    raw = (
        "First draft: FINAL ANSWER: wrong\n"
        "After checking: FINAL ANSWER: 90"
    )
    assert gaia_agent_mod._extract_answer(raw) == "90"


def test_extract_answer_skips_markdown_header(gaia_agent_mod):
    """bass clef regression: ``**Step 8 — FINAL ANSWER:**`` then real answer."""
    raw = (
        "Reasoning...\n"
        "**Step 8 — FINAL ANSWER:**\n"
        "The computed value is:\n"
        "FINAL ANSWER: 90\n"
    )
    assert gaia_agent_mod._extract_answer(raw) == "90"


def test_extract_answer_skips_trailing_asterisks(gaia_agent_mod):
    """``FINAL ANSWER: 90**`` should strip the closing ``**``."""
    raw = "...\nFINAL ANSWER: 90**\n"
    assert gaia_agent_mod._extract_answer(raw) == "90"


def test_extract_answer_skips_empty_header_only(gaia_agent_mod):
    """If only header-style match exists, fall back to last non-empty line."""
    raw = (
        "Body reasoning 42.\n"
        "**FINAL ANSWER:**\n"
    )
    # No meaningful sentinel capture → fallback scans lines; last non-poison
    # non-empty line is ``**FINAL ANSWER:**`` which is dropped by the poison
    # filter nothing. Actually the fallback keeps the line since ``FINAL`` is
    # not in the poison list — this documents existing behavior: answer is
    # the last meaningful line available.
    out = gaia_agent_mod._extract_answer(raw)
    # Either the body line or the header-line fallback; assert non-empty so
    # downstream won't get ``""``.
    assert out  # pragma: no branch


def test_extract_answer_handles_backticks(gaia_agent_mod):
    raw = "FINAL ANSWER: `90`"
    assert gaia_agent_mod._extract_answer(raw) == "90"


def test_extract_answer_long_truncated(gaia_agent_mod):
    raw = "FINAL ANSWER: " + ("abc. " * 100)
    out = gaia_agent_mod._extract_answer(raw)
    assert len(out) <= 200


def test_extract_answer_case_insensitive(gaia_agent_mod):
    assert gaia_agent_mod._extract_answer("final answer: yes") == "yes"


def test_extract_answer_last_match_between_markdown(gaia_agent_mod):
    """Bass clef full-shape: header + real answer mixed with markdown."""
    raw = (
        "## Step 1 — Identify word\n"
        "word = DECADE (D-E-C-A-D-E)\n"
        "## Step 2 — Tag letters\n"
        "tags = L,S,S,S,L,S\n"
        "## Step 8 — FINAL ANSWER:**\n"
        "Computation: 5+6-2=9, 10*9=90\n"
        "\n"
        "FINAL ANSWER: 90\n"
    )
    assert gaia_agent_mod._extract_answer(raw) == "90"


def test_extract_answer_empty(gaia_agent_mod):
    assert gaia_agent_mod._extract_answer("") == ""


def test_extract_answer_no_sentinel_falls_back(gaia_agent_mod):
    raw = "Just a number: 42"
    assert "42" in gaia_agent_mod._extract_answer(raw)


def test_extract_answer_skips_placeholder_template(gaia_agent_mod):
    """Prompt-shown placeholder ``<integer>`` must not be returned."""
    raw = (
        "Step 8 — FINAL ANSWER: <integer>\n"
        "Reasoning body 42.\n"
        "**Step 8 — FINAL ANSWER:**\n"
        "42\n"
    )
    assert gaia_agent_mod._extract_answer(raw) == "42"


def test_extract_answer_bass_clef_real_raw_shape(gaia_agent_mod):
    """End-to-end: header + number on next line after placeholder ref."""
    raw = (
        "INSTRUCTIONS:\n"
        "Step 8 — FINAL ANSWER: <integer>\n"
        "\n"
        "Body reasoning...\n"
        "10 * 9 = 90\n"
        "\n"
        "**Step 8 — FINAL ANSWER:**\n"
        "90\n"
    )
    assert gaia_agent_mod._extract_answer(raw) == "90"


def test_extract_answer_header_with_body_before(gaia_agent_mod):
    """Body computed the answer but header is hollow — fall back to body."""
    raw = (
        "Body reasoning 42.\n"
        "**FINAL ANSWER:**\n"
    )
    assert gaia_agent_mod._extract_answer(raw) == "Body reasoning 42"


def test_extract_answer_placeholder_value_variant(gaia_agent_mod):
    raw = (
        "FINAL ANSWER: <value>\n"
        "Here's the actual answer: yellow\n"
        "FINAL ANSWER: yellow\n"
    )
    assert gaia_agent_mod._extract_answer(raw) == "yellow"
