"""concinno.decision — architect red/blue decision primitives.

Commander-level data structures for high-blast-radius decisions. Blueprint
lifted from the internal redteam.md SOP: 1 red-team Opus (architecture
attacker) + 1 blue-team Opus (architecture defender) + commander
adjudication. concinno provides the prompt templates + result shape; the
consumer wires in its own LLM calls.

Public API::

    from concinno.decision import (
        RedBlueDecision,
        build_redblue_prompt,
        adjudicate,
    )

    red_prompt = build_redblue_prompt("red", proposal="...", context={...})
    blue_prompt = build_redblue_prompt("blue", proposal="...", context={...})
    # ... consumer calls LLM with these prompts ...

    decision = RedBlueDecision(
        proposal="...",
        radius="high",
        red_attacks=[{"attack": "...", "evidence": "...", "severity": "FATAL"}],
        blue_defense=[{"claim": "...", "evidence": "..."}],
        commander_verdict="CONDITIONAL_GO",
        must_run_experiments=["..."],
    )
    print(adjudicate(decision))
"""

from concinno.decision.redblue import (
    RedBlueDecision,
    adjudicate,
    build_redblue_prompt,
)

__all__ = [
    "RedBlueDecision",
    "adjudicate",
    "build_redblue_prompt",
]
