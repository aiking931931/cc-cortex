"""Tests for concinno.constraint_build (D-1 C0.5 stage, plan §13.3)."""

from __future__ import annotations

import pytest

from concinno.constraint_build import (
    DEFAULT_HARD_PREDICATE_THRESHOLD,
    DEFAULT_KNOB_COUNT_THRESHOLD,
    ConstraintBuildInput,
    build_constraint_graph,
    should_emit_stage,
)

# ── should_emit_stage gate logic ─────────────────────────────────


def test_should_emit_simple_always_skips() -> None:
    assert should_emit_stage("simple", knob_count=10, declared_constraint_count=10) is False
    assert should_emit_stage("Simple", knob_count=10, declared_constraint_count=10) is False


def test_should_emit_complicated_with_thresholds_met() -> None:
    assert (
        should_emit_stage(
            "Complicated",
            knob_count=DEFAULT_KNOB_COUNT_THRESHOLD,
            declared_constraint_count=DEFAULT_HARD_PREDICATE_THRESHOLD,
        )
        is True
    )


def test_should_emit_complicated_below_knob_threshold_skips() -> None:
    assert (
        should_emit_stage(
            "Complicated",
            knob_count=DEFAULT_KNOB_COUNT_THRESHOLD - 1,
            declared_constraint_count=10,
        )
        is False
    )


def test_should_emit_complicated_below_predicate_threshold_skips() -> None:
    assert (
        should_emit_stage(
            "Complicated",
            knob_count=10,
            declared_constraint_count=DEFAULT_HARD_PREDICATE_THRESHOLD - 1,
        )
        is False
    )


def test_should_emit_chaotic_with_thresholds_met() -> None:
    assert should_emit_stage("chaotic", knob_count=3, declared_constraint_count=2) is True


# ── build_constraint_graph happy path ────────────────────────────


def test_build_graph_memoria_style_three_constraints() -> None:
    """Mirror the Memoria 0.4.x exemplar at the heuristic level."""
    inp = ConstraintBuildInput(
        problem_statement=(
            "Build the Memoria onedir Windows .exe. workpath: scripts/memoria/. "
            "distpath: scripts/memoria/build/. spec_basename = memoria. "
            "The path workpath/<spec_basename>/Memoria.exe must be in the AV "
            "whitelist. distpath != workpath."
        ),
        declared_knobs=("workpath", "distpath", "spec_basename"),
        known_constraints=(
            "workpath/<spec_basename>/Memoria.exe MUST lie under Surfshark "
            "AV whitelist path",
        ),
    )
    out = build_constraint_graph(inp)
    assert out.emitted is True
    assert out.graph is not None
    var_names = {v["name"] for v in out.graph["vars"]}
    assert {"workpath", "distpath", "spec_basename"} <= var_names
    assert any(p["kind"] == "hard" for p in out.graph["predicates"])
    # the known_constraint is promoted to hard verbatim
    hard_exprs = [p["expr"] for p in out.graph["predicates"] if p["kind"] == "hard"]
    assert any("Surfshark" in e for e in hard_exprs)


# ── refusal modes (drop silently) ────────────────────────────────


def test_no_declared_knobs_drops_silently() -> None:
    inp = ConstraintBuildInput(
        problem_statement="something must be true and a > 3",
        declared_knobs=(),
    )
    out = build_constraint_graph(inp)
    assert out.emitted is False
    assert out.graph is None
    assert any("no declared_knobs" in n for n in out.notes)


def test_empty_problem_statement_drops() -> None:
    inp = ConstraintBuildInput(
        problem_statement="",
        declared_knobs=("a", "b"),
    )
    out = build_constraint_graph(inp)
    assert out.emitted is False
    assert any("empty problem_statement" in n for n in out.notes)


def test_below_var_threshold_drops() -> None:
    inp = ConstraintBuildInput(
        problem_statement="x must equal 5",
        declared_knobs=("x",),
    )
    out = build_constraint_graph(inp)
    assert out.emitted is False
    assert any("fewer than" in n for n in out.notes)


def test_no_predicates_drops() -> None:
    inp = ConstraintBuildInput(
        problem_statement="a is a thing. b is another thing.",
        declared_knobs=("a", "b"),
    )
    out = build_constraint_graph(inp)
    assert out.emitted is False
    assert any("predicate" in n.lower() for n in out.notes)


# ── modal classification ─────────────────────────────────────────


def test_modal_must_promotes_hard() -> None:
    inp = ConstraintBuildInput(
        problem_statement="a > 0. b must equal 7.",
        declared_knobs=("a", "b"),
        known_constraints=("a >= 0",),
    )
    out = build_constraint_graph(inp)
    assert out.emitted is True
    assert out.graph is not None
    kinds = {p["kind"] for p in out.graph["predicates"]}
    assert "hard" in kinds


def test_modal_should_marks_soft() -> None:
    inp = ConstraintBuildInput(
        problem_statement="x should be greater than 5. y should equal 0.",
        declared_knobs=("x", "y"),
    )
    out = build_constraint_graph(inp)
    assert out.emitted is True
    assert out.graph is not None
    assert all(p["kind"] == "soft" for p in out.graph["predicates"])


# ── opt-out: caller can disable declared_knobs requirement ───────


def test_require_declared_knobs_false_allows_inference() -> None:
    """Disable the strict requirement and let the heuristic try alone."""
    inp = ConstraintBuildInput(
        problem_statement=(
            "Variables are workpath, distpath, and basename. "
            "workpath = scripts_memoria, distpath = scripts_memoria_build, "
            "basename = memoria. workpath != distpath. "
            "workpath must be in whitelist."
        ),
        declared_knobs=(),
    )
    out = build_constraint_graph(inp, require_declared_knobs=False)
    assert out.emitted is True
    assert out.graph is not None
    assert len(out.graph["vars"]) >= DEFAULT_KNOB_COUNT_THRESHOLD


# ── schema compatibility with cigito_v3 ──────────────────────────


def test_graph_dict_shape_matches_cigito_v3_schema() -> None:
    """The graph shape must match cigito_v3.distill.failure_mode.ConstraintGraph.to_dict()."""
    inp = ConstraintBuildInput(
        problem_statement=(
            "x: 0. y: 5. x must equal 0. y must equal 5."
        ),
        declared_knobs=("x", "y"),
    )
    out = build_constraint_graph(inp)
    assert out.graph is not None
    assert set(out.graph.keys()) == {"vars", "predicates", "ground_truth_assignment"}
    for v in out.graph["vars"]:
        assert {"name", "domain", "initial_value"} <= set(v.keys())
    for p in out.graph["predicates"]:
        assert {"expr", "kind"} <= set(p.keys())


@pytest.mark.parametrize(
    "complexity",
    ["simple", "complicated", "complex", "chaotic"],
)
def test_should_emit_complexity_case_insensitive(complexity: str) -> None:
    """Decision is case-insensitive on the complexity label."""
    out_lower = should_emit_stage(complexity.lower(), 5, 5)
    out_upper = should_emit_stage(complexity.upper(), 5, 5)
    assert out_lower == out_upper
