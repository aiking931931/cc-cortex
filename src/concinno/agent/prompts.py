"""Agent behavior prompt building blocks (benchmark-agnostic).

@module concinno.agent.prompts
@responsibility Centralise reusable agent-guidance prompt fragments
    so benchmark runners compose them rather than copy-paste them.
@dependencies stdlib only
@exports AGENT_GUIDANCE_UNCERTAINTY, AGENT_GUIDANCE_ARITHMETIC,
    AGENT_GUIDANCE_NO_REFUSAL, default_guidance
"""

from __future__ import annotations

AGENT_GUIDANCE_UNCERTAINTY = (
    "If the question mentions any technical specification, domain "
    "term, API, or fact you are not 100% certain about, you MUST "
    "call web_search or fetch_url before answering. Do not guess."
)

AGENT_GUIDANCE_ARITHMETIC = (
    "For any multi-step arithmetic or unit conversion, use the "
    "run_bash tool with `python3 -c \"print(<expression>)\"` to "
    "compute the result. Do not rely on mental math. Redo the "
    "calculation once to verify."
)

AGENT_GUIDANCE_NO_REFUSAL = (
    "Even if you cannot find perfect information, commit to your "
    "best single concrete answer. Never output placeholder text "
    "such as 'I cannot', 'I am unable', 'I need more information', "
    "'Once I have access', or partial sentences — these are all "
    "scored as wrong. A best-guess value beats a refusal."
)


def default_guidance() -> str:
    """Return the default joined agent-guidance prompt."""
    return "\n".join((
        AGENT_GUIDANCE_UNCERTAINTY,
        AGENT_GUIDANCE_ARITHMETIC,
        AGENT_GUIDANCE_NO_REFUSAL,
    ))
