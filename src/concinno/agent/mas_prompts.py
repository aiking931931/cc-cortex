"""Default critic / judge / fallback prompts for :mod:`concinno.agent.mas_loop`.

@module concinno.agent.mas_prompts
@responsibility Ship minimal, benchmark-agnostic prompt templates the
    orchestrator can fall back on when a consumer did not supply its
    own. Consumers (e.g. ``persona.benchmark.mas_prompts`` in the
    Sancio GAIA adapter) override these with domain-specific wording
    —  FINAL ANSWER sentinels, EXACT quote rules, etc.

Positive framing rule (H2 in the design-phase red-team):
    MEMORY #5 / #89 record Gemma-class failure modes where the model
    *echoes* refusal phrases in its own answer slot ("I cannot find
    ...", "solver produced no answer"). Fallback prompts in this
    module are deliberately positive-framed: they ask the model what
    it *can* produce rather than describing what it doesn't know.
    The regression test (``test_mas_prompts.py::test_fallback_has_no_refusal``)
    hard-codes the prohibited phrases so a future well-meaning edit
    cannot silently re-introduce the regression.

Slot contract:
    ``{question}`` — the raw user question.
    ``{solver_answer}`` — the solver's final answer string (may be
    empty when the empty-cascade short-circuit fires; in that path
    the orchestrator uses :data:`DEFAULT_CRITIC_FALLBACK_PROMPT`
    instead, which does not reference the solver at all).
    ``{solver_trace_summary}`` — a compact textual summary of the
    solver's top-``k`` tool results, joined on newlines.
    ``{response_1}`` / ``{response_2}`` — blind-shuffled candidates
    for the judge. Order is randomised per task_id (see
    :func:`concinno.agent.mas_loop.blind_label_order`).

@dependencies stdlib only
@exports DEFAULT_CRITIC_PROMPT, DEFAULT_CRITIC_FALLBACK_PROMPT,
    DEFAULT_JUDGE_PROMPT
"""

from __future__ import annotations

DEFAULT_CRITIC_PROMPT = (
    "You are the critic in a 3-role adversarial agent stack. The "
    "solver produced an answer using tools. Your job: consider the "
    "question and the solver's trace, then commit to your own "
    "single concrete answer — which may agree with or diverge from "
    "the solver's.\n\n"
    "Question:\n{question}\n\n"
    "Solver's final answer:\n{solver_answer}\n\n"
    "Solver's top tool results (summarised):\n{solver_trace_summary}\n\n"
    "Return exactly one concrete answer. Do not hedge. Do not "
    "echo the solver if you would answer differently.\n"
    "FINAL ANSWER: <value>"
)


DEFAULT_CRITIC_FALLBACK_PROMPT = (
    "Based on the question alone, what is your best-effort single "
    "concrete answer?\n\n"
    "Question:\n{question}\n\n"
    "Commit to one concrete value — a number, a name, or a short "
    "phrase. A best guess beats an empty response.\n"
    "FINAL ANSWER: <value>"
)


DEFAULT_JUDGE_PROMPT = (
    "You are the judge in a 3-role adversarial agent stack. Two "
    "candidate answers are below, shown in randomised order so the "
    "order carries no signal. Pick the one you assess as correct.\n\n"
    "Question:\n{question}\n\n"
    "Response 1:\n{response_1}\n\n"
    "Response 2:\n{response_2}\n\n"
    "Output your final decision as the chosen answer value (not "
    "the label). If both are plausible, pick the one with more "
    "verifiable reasoning.\n"
    "FINAL ANSWER: <value>"
)


__all__ = [
    "DEFAULT_CRITIC_FALLBACK_PROMPT",
    "DEFAULT_CRITIC_PROMPT",
    "DEFAULT_JUDGE_PROMPT",
]
