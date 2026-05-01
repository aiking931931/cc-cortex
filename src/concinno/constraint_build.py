"""concinno.constraint_build — D-1 C0.5 Constraint Build CBUA stage.

@module concinno.constraint_build
@responsibility Heuristic constraint-graph builder that fires between
    C0 (route) and C1 (orient) for tasks classified ``Complicated`` or
    higher. Produces a structured (vars / domains / predicates) graph
    *before* any action token is emitted, per Cigito v3 plan §13.3 D-1
    binding. The output is recorded into the trajectory so downstream
    consumers (W2 distill curator, W4 G6 emission gate) can score
    whether the stage fires reliably.
@dependencies stdlib only (re + dataclasses + typing).
@exports ConstraintBuildInput, ConstraintBuildOutput,
    ConstraintGraphDict, build_constraint_graph, should_emit_stage,
    DEFAULT_KNOB_COUNT_THRESHOLD, DEFAULT_HARD_PREDICATE_THRESHOLD

Why this stage exists
---------------------

A documented OOD constraint-satisfaction thrash session showed 30+
iterations of pattern-matching on a 3-constraint problem solvable by
paper-and-pencil constraint graph in 5 min. This stage's purpose is
to emit the graph BEFORE acting, so the OOD failure mode is
structurally prevented rather than learned-around. The W2 distill
corpus consumer carries the canonical exemplar (see
``cigito_v3.distill.failure_mode.memoria_044_thrash_exemplar`` in the
Cigito v3 sub-package).

Pass criteria (plan §13 D-4 G6)
-------------------------------

* Emission rate >= 70% on the OOD constraint-sat 50-task slice
* Pearson correlation r(emission_quality, task_success) >= 0.4

This module gives downstream code the tooling to *measure* both. It
does not enforce the gate — that is the W4 verdict's job.

Heuristic-only — no LLM call
----------------------------

In the spirit of ``cigito_v3.distill.curator`` (W2 milestone is $0
CPU, no API key allowed) the constraint-build stage uses regex and
simple parsing on the problem statement and the declared-knobs list.
It is deliberately conservative: when in doubt about whether a phrase
denotes a variable or a constraint, we DO NOT emit a graph rather
than emit a noisy one. False negatives are cheap (caller proceeds
without the stage). False positives are costly (downstream W2 corpus
gets noisy constraint_graph fields).

Phase D will replace the heuristic with an inference-time tool call
to the trained Cigito student model — see plan §Phase D.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "DEFAULT_HARD_PREDICATE_THRESHOLD",
    "DEFAULT_KNOB_COUNT_THRESHOLD",
    "ConstraintBuildInput",
    "ConstraintBuildOutput",
    "ConstraintGraphDict",
    "build_constraint_graph",
    "should_emit_stage",
]


DEFAULT_KNOB_COUNT_THRESHOLD = 2
"""Minimum number of declared variables for D-1 emission to be required."""

DEFAULT_HARD_PREDICATE_THRESHOLD = 1
"""Minimum number of detectable hard predicates for emission to be required."""


# Patterns chosen for low false-positive rate on engineering / SOP prose.
# We match colon-separated declarations ("workpath: scripts/memoria/") and
# explicit knob phrases ("variables / knobs / parameters / paths / flags
# such as a, b, c"). Free-form natural language is intentionally NOT
# parsed — over-extraction was the failure mode of an earlier draft.

_KNOB_LIST_RE = re.compile(
    r"\b(?:variables?|knobs?|parameters?|paths?|flags?|fields?)\s*"
    r"(?:are|=|:)\s*([A-Za-z0-9_,\s/\-]+?)(?:[.;]|$|\n)",
    re.IGNORECASE,
)

_DECLARED_VAR_RE = re.compile(
    r"\b([a-z][a-z0-9_]{1,32})\s*[:=]\s*([^,;\n]{1,80})", re.IGNORECASE
)

_HARD_KEYWORDS = (
    "must",
    "MUST",
    "required",
    "必須",
    "required to",
    "shall",
    "has to",
    "needs to",
)

_SOFT_KEYWORDS = (
    "should",
    "prefer",
    "preferably",
    "ideally",
    "nice to",
    "建議",
)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


ConstraintGraphDict = dict[str, Any]
"""Wire-format graph; matches cigito_v3.distill.failure_mode.ConstraintGraph.to_dict()."""


@dataclass(frozen=True)
class ConstraintBuildInput:
    """Inputs to :func:`build_constraint_graph`.

    The caller is responsible for providing both the natural-language
    problem statement *and* a declared list of knobs / known
    constraints. Forcing the caller to declare reduces the heuristic's
    false-positive rate substantially — without declared knobs we drop
    silently rather than guess.
    """

    problem_statement: str
    declared_knobs: tuple[str, ...] = ()
    known_constraints: tuple[str, ...] = ()


@dataclass
class ConstraintBuildOutput:
    """Output of :func:`build_constraint_graph`.

    The graph follows the same shape as
    :class:`cigito_v3.distill.failure_mode.ConstraintGraph` so it
    serialises directly into W2 distill records and W4 emission-rate
    eval traces.
    """

    emitted: bool
    graph: ConstraintGraphDict | None = None
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _classify_predicate_kind(text: str) -> Literal["hard", "soft"]:
    """Decide hard / soft from a sentence's modal phrasing.

    Order matters: hard keywords win when both are present (we err on
    the side of strictness so the W4 G6 emission rate measures the
    upper bound on detectable hard constraints).
    """
    if any(kw in text for kw in _HARD_KEYWORDS):
        return "hard"
    if any(kw in text for kw in _SOFT_KEYWORDS):
        return "soft"
    return "hard"


def _extract_predicates(text: str) -> list[dict[str, str]]:
    """Pull hard / soft predicate sentences from the problem statement.

    Sentences are split on ``.``, ``;``, or newline. A sentence is kept
    iff it contains at least one hard or soft keyword AND mentions at
    least one declared knob or comparison operator (=, !=, <, >, <=,
    >=, ==, in, not in). The conservatism keeps noise out of the W2
    corpus.
    """
    sentences = re.split(r"[.;\n]+", text)
    predicates: list[dict[str, str]] = []
    for raw in sentences:
        s = raw.strip()
        if not s:
            continue
        has_modal = any(
            kw in s for kw in _HARD_KEYWORDS + _SOFT_KEYWORDS
        )
        if not has_modal:
            continue
        has_predicate_signal = bool(
            re.search(r"[=<>!]=?|\bnot in\b|\bin\b|\bequals?\b", s)
        )
        if not has_predicate_signal:
            continue
        predicates.append({"expr": s[:240], "kind": _classify_predicate_kind(s)})
    return predicates


def _build_vars(
    declared_knobs: tuple[str, ...],
    text: str,
) -> list[dict[str, Any]]:
    """Compose the variables section from declared knobs + lightweight
    ``name: initial_value`` extraction.
    """
    vars_out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for knob in declared_knobs:
        knob = knob.strip()
        if not knob or knob in seen:
            continue
        seen.add(knob)
        vars_out.append(
            {"name": knob, "domain": "any:str", "initial_value": None}
        )
    for match in _DECLARED_VAR_RE.finditer(text):
        name = match.group(1).strip()
        initial = match.group(2).strip()
        if not name or name in seen:
            continue
        # Skip purely numeric or stop-word tokens.
        if name.lower() in {"the", "a", "an", "and", "or", "but"}:
            continue
        if not re.match(r"^[A-Za-z_]", name):
            continue
        seen.add(name)
        vars_out.append(
            {"name": name, "domain": "any:str", "initial_value": initial[:80]}
        )
    return vars_out


# ---------------------------------------------------------------------------
# Public entry-points
# ---------------------------------------------------------------------------


def should_emit_stage(
    complexity: str,
    knob_count: int,
    declared_constraint_count: int,
    *,
    knob_threshold: int = DEFAULT_KNOB_COUNT_THRESHOLD,
    predicate_threshold: int = DEFAULT_HARD_PREDICATE_THRESHOLD,
) -> bool:
    """Decide whether the C0.5 stage should fire for a given task.

    Per plan §13.3 D-1: tasks classified Complicated+ that touch at
    least 2 variables and 1 hard predicate must emit the graph.
    Simple tasks always skip — the spawn overhead is not worth it.
    """
    if complexity.lower() == "simple":
        return False
    return (
        knob_count >= knob_threshold
        and declared_constraint_count >= predicate_threshold
    )


def build_constraint_graph(
    inp: ConstraintBuildInput,
    *,
    require_declared_knobs: bool = True,
) -> ConstraintBuildOutput:
    """Heuristic constraint-graph builder.

    Returns ``ConstraintBuildOutput(emitted=False, graph=None)`` when:

    * ``require_declared_knobs`` is True (default) and
      ``inp.declared_knobs`` is empty — caller failed to declare,
      so we drop silently rather than guess from prose.
    * Fewer than ``DEFAULT_KNOB_COUNT_THRESHOLD`` variables emerge from
      the combined declared + extracted set.
    * No predicates pass the modal + comparison filter.

    On success, returns ``emitted=True`` with a graph dict matching
    :class:`cigito_v3.distill.failure_mode.ConstraintGraph.to_dict()``.
    """
    notes: list[str] = []
    if require_declared_knobs and not inp.declared_knobs:
        notes.append(
            "no declared_knobs supplied — heuristic refuses to guess"
        )
        return ConstraintBuildOutput(emitted=False, notes=notes)

    text = inp.problem_statement.strip()
    if not text:
        notes.append("empty problem_statement")
        return ConstraintBuildOutput(emitted=False, notes=notes)

    vars_out = _build_vars(inp.declared_knobs, text)
    if len(vars_out) < DEFAULT_KNOB_COUNT_THRESHOLD:
        notes.append(
            f"fewer than {DEFAULT_KNOB_COUNT_THRESHOLD} variables resolved "
            f"(got {len(vars_out)}); skipping emission"
        )
        return ConstraintBuildOutput(emitted=False, notes=notes)

    predicates = _extract_predicates(text)
    # Promote known_constraints (caller-supplied) to hard predicates
    # without modal filtering; they are by definition declared.
    for kc in inp.known_constraints:
        if kc.strip():
            predicates.append({"expr": kc.strip()[:240], "kind": "hard"})

    if not predicates:
        notes.append(
            "no hard / soft predicates detected; skipping emission"
        )
        return ConstraintBuildOutput(emitted=False, notes=notes)

    graph: ConstraintGraphDict = {
        "vars": vars_out,
        "predicates": predicates,
        "ground_truth_assignment": None,
    }
    return ConstraintBuildOutput(emitted=True, graph=graph, notes=notes)
