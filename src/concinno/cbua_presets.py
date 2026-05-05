"""concinno.cbua_presets — Curated prompt presets for CBUA complexity tiers.

@module concinno.cbua_presets
@responsibility Exports primary-source-cited prompt presets that callers
    (Cigito v3 weight-layer training, concinno.cognitive_inject downstream
    consumers, Sancio runtime preamble injection) can compose into a
    system prompt without re-deriving the wording.
@dependencies stdlib only.
@exports THINK_MAX_PROMPT, build_chaotic_directives, PRESETS

Design notes
------------

Each preset constant in this module ships with:

1. Provenance — a primary-source citation (paper / docs URL).
2. Verbatim flag — whether the constant is verbatim from the source or
   paraphrased. Verbatim means downstream sessions can cite it as
   first-party.
3. Posterior tier (ZIQ filter, see plan §3.2 of cigito-v3-sota-synthesis-
   ziq-filter-2026-04-29.md) — HIGH / MED / LOW. HIGH means the preset
   is recommended adoption.

This separation keeps the V4-specific content out of
``cognitive_inject.py`` (which carries Concinno's own L0/L1/L2 cognition
directives) — V4 / external presets are isolated so the audit boundary
between "our cognition" and "borrowed from other models" stays visible.

Usage
-----

For a chaotic-radius task, call :func:`build_chaotic_directives` and
prepend the result to the system prompt. The function pairs the V4
Think Max preamble with concinno's existing L0/L1/L2 directives so the
caller does not need to import both modules.

For a Cigito v3 training run, the W2 corpus may include trajectories
where the system prompt was the Think Max preamble — in that case cite
this module by name in the trajectory metadata so the W4 G6 emission
gate can correlate constraint-graph emission with the preset used.

Provenance
----------

Sources in this module are cited per
``feedback_v4_paper_hallucinated_mechanisms.md`` (MEMORY #4j) — every
constant traces to a paper section, never a third-party blog summary.
"""

from __future__ import annotations

from typing import Literal

__all__ = [
    "PRESETS",
    "THINK_MAX_PROMPT",
    "build_chaotic_directives",
]


# ---------------------------------------------------------------------------
# THINK_MAX_PROMPT — DeepSeek V4 §5.1.1 Table 3, verbatim
# ---------------------------------------------------------------------------

THINK_MAX_PROMPT = """\
Reasoning Effort: Absolute maximum with no shortcuts permitted.
You MUST be very thorough in your thinking and comprehensively decompose the \
problem to resolve the root cause, rigorously stress-testing your logic against \
all potential paths, edge cases, and adversarial scenarios.
Explicitly write out your entire deliberation process, documenting every \
intermediate step, considered alternative, and rejected hypothesis to ensure \
absolutely no assumption is left unchecked."""
"""V4 Table 3 — Think Max system-prompt prefix.

Provenance: DeepSeek-V4 Technical Report §5.1.1 Table 3 (the instruction
injected into the system prompt for the "Think Max" reasoning mode of
DeepSeek-V4-Pro). Verbatim from the paper. Cited 2026-04-29 from
``_AI_BRAIN/00_System/external_papers/03_deepseek/v4/DeepSeek_V4.pdf``.

ZIQ posterior tier: HIGH. The preset directly maps to CBUA Chaotic
radius — both demand maximum effort, comprehensive decomposition,
adversarial stress-testing, and full deliberation transcript. V4
authors validated empirically on their reasoning benchmarks.

Cigito v3 use: paired with the W4 G6 constraint-graph emission gate —
the Think Max preamble pushes the model toward the "explicitly write
out every intermediate step" behaviour that constraint-graph emission
is operationalising.
"""


# ---------------------------------------------------------------------------
# Composition helper
# ---------------------------------------------------------------------------


_BUILD_FALLBACK = (
    "- Fix all errors you see now. ✅done ⏸half(where+why).\n"
    "- Unsure → look it up. Don't guess.\n"
    "- Rank by CP: ①likelihood ②ease → highest first.\n\n"
    "- First instinct ≠ best. List 3+ options before choosing.\n"
    "- Find evidence against yourself. Can you disprove your answer?\n"
    "- Time spent ≠ reason to continue. Wrong direction → turn.\n"
    "- What should be here but isn't? What should happen but didn't?\n"
    "- A changed, B improved ≠ A caused B. Without A, would B self-heal?\n\n"
    "- Root cause: diverge → CP(likelihood×ease) → converge → test highest.\n"
    "- User's framing may mislead. Low confidence → say so, then verify.\n"
    "- Sweet spot: simplest + fewest side effects. Stuck ≥2 rounds → escalate.\n"
    "- Counterfactual: A fixed B ≠ root cause. Without A, would B self-heal?\n"
    "- Inversion: how would this fail? Avoid those paths.\n"
    "- Every 3-5 steps: drifting? stuck? repeating? Remove it — what breaks? "
    "Nothing = don't build."
)


def build_chaotic_directives(
    *,
    include_concinno_cognition: bool = True,
    extra_directives: str = "",
) -> str:
    """Compose the Chaotic-radius preamble.

    Returns the V4 Think Max preamble, optionally followed by concinno's
    L0/L1/L2 imperative cognition directives (from
    :mod:`concinno.cognitive_inject`), optionally followed by any extra
    caller-supplied text.

    Parameters
    ----------
    include_concinno_cognition:
        When ``True`` (default), append the L0+L1+L2 directives from
        ``cognitive_inject.build_thinking_directives("full")`` after the
        Think Max preamble, separated by a blank line. When ``False``,
        the Think Max preamble is returned alone — useful when the
        caller already injects concinno cognition elsewhere.

    extra_directives:
        Optional caller-supplied imperative text appended at the end.
        Useful for task-specific addenda (e.g. "Output language: zh-TW").
    """
    parts: list[str] = [THINK_MAX_PROMPT]
    if include_concinno_cognition:
        try:
            from concinno.cognitive_inject import build_thinking_directives

            parts.append(build_thinking_directives("full"))
        except ImportError:
            # Fall back to a baked-in copy when cognitive_inject is not
            # importable (e.g. trimmed deployments). Keeps the module
            # self-contained for the spectral / training rigs that may
            # not import the full concinno surface.
            parts.append(_BUILD_FALLBACK)
    if extra_directives:
        parts.append(extra_directives.strip())
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Preset registry — for callers that want to enumerate available presets
# ---------------------------------------------------------------------------


PRESET_NAMES = Literal["chaotic_v4_think_max"]


PRESETS: dict[str, dict[str, str]] = {
    "chaotic_v4_think_max": {
        "name": "Chaotic — V4 Think Max",
        "source": (
            "DeepSeek-V4 Technical Report §5.1.1 Table 3 "
            "(_AI_BRAIN/00_System/external_papers/03_deepseek/v4/DeepSeek_V4.pdf)"
        ),
        "verbatim": "true",
        "posterior_tier": "HIGH",
        "constant_name": "THINK_MAX_PROMPT",
        "use_when": (
            "Task is classified Chaotic radius by C0Router OR caller "
            "explicitly opts into max-effort reasoning. Pairs with W4 G6 "
            "constraint-graph emission gate for Cigito v3 training "
            "trajectories."
        ),
    },
}
"""Registry of available presets.

Each entry carries provenance, verbatim flag, ZIQ posterior tier, the
exporting constant name, and a use-when condition. Caller can iterate
``PRESETS.values()`` to surface the catalog.
"""
