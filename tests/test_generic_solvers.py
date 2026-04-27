"""Tests for generic_solvers public API.

Verifies that the three public solver functions and detector predicates
are importable directly from ``concinno.skills.public.agent.generic_solvers``
without going through ``gaia_agent``, and that the public names match the
``__all__`` contract. Functional unit tests live in the per-solver test
files (test_gaia_polygon_opencv_hybrid, test_gaia_colour_coded_numeric_hybrid,
test_gaia_quiz_scoring_hybrid) which continue to import from gaia_agent for
backward-compat; this file tests the *new* public surface only.
"""
from __future__ import annotations


def test_public_api_importable():
    from concinno.skills.public.agent.generic_solvers import (
        extract_json_object,
        is_colour_coded_numeric_data_question,
        is_image_quiz_scoring_question,
        is_orthogonal_polygon_area_question,
        solve_colour_coded_numeric_via_hybrid,
        solve_image_quiz_scoring_via_hybrid,
        solve_orthogonal_polygon_via_opencv_hybrid,
    )
    assert callable(solve_orthogonal_polygon_via_opencv_hybrid)
    assert callable(solve_colour_coded_numeric_via_hybrid)
    assert callable(solve_image_quiz_scoring_via_hybrid)
    assert callable(is_orthogonal_polygon_area_question)
    assert callable(is_colour_coded_numeric_data_question)
    assert callable(is_image_quiz_scoring_question)
    assert callable(extract_json_object)


def test_all_exports_match_importable():
    import concinno.skills.public.agent.generic_solvers as gs
    for name in gs.__all__:
        assert hasattr(gs, name), f"__all__ lists {name!r} but not present in module"
        assert callable(getattr(gs, name)), f"{name!r} is not callable"


def test_no_circular_import():
    """Importing generic_solvers must not trigger gaia_agent import."""
    import sys
    # Remove cached modules to force fresh import
    for key in list(sys.modules):
        if "gaia_agent" in key or "generic_solvers" in key:
            del sys.modules[key]
    import concinno.skills.public.agent.generic_solvers  # noqa: F401
    assert "concinno.skills.public.agent.gaia_agent" not in sys.modules, (
        "generic_solvers triggered gaia_agent import — circular dep risk"
    )


def test_detector_polygon_area():
    from concinno.skills.public.agent.generic_solvers import (
        is_orthogonal_polygon_area_question,
    )
    assert is_orthogonal_polygon_area_question(
        "What is the area of the polygon in the image?"
    )
    assert is_orthogonal_polygon_area_question(
        "The shape has labelled side lengths. Find its surface area."
    )
    assert not is_orthogonal_polygon_area_question(
        "What is the capital of France?"
    )


def test_detector_colour_coded_numeric():
    from concinno.skills.public.agent.generic_solvers import (
        is_colour_coded_numeric_data_question,
    )
    assert is_colour_coded_numeric_data_question(
        "What is the average of the pstdev of the red numbers "
        "and the stdev of the green numbers?"
    )
    assert not is_colour_coded_numeric_data_question(
        "What is the average of the red and blue squares?"
    )
    assert not is_colour_coded_numeric_data_question(
        "What is the sum of the green numbers?"  # only 1 colour
    )


def test_detector_quiz_scoring_no_file():
    from concinno.skills.public.agent.generic_solvers import (
        is_image_quiz_scoring_question,
    )
    q = (
        "scored as follows: Problems that ask the student to add or subtract "
        "fractions: 2 points. Problems that ask to multiply or divide fractions: "
        "3 points. How many points did the student earn?"
    )
    assert is_image_quiz_scoring_question(q, "quiz.png")
    assert not is_image_quiz_scoring_question(q, None)
    assert not is_image_quiz_scoring_question(q, "quiz.txt")


def test_extract_json_object_pure():
    from concinno.skills.public.agent.generic_solvers import extract_json_object
    assert extract_json_object('{"a": 1}') == '{"a": 1}'
    assert extract_json_object("```json\n{\"x\": 2}\n```") == '{"x": 2}'
    assert extract_json_object("Here is the JSON: {\"y\": 3} done.") == '{"y": 3}'
    assert extract_json_object("no object here") is None
    assert extract_json_object("") is None


def test_gaia_agent_reexports_public_names():
    """gaia_agent must re-export the public solver names for external callers."""
    import sys
    for key in list(sys.modules):
        if "gaia_agent" in key:
            del sys.modules[key]
    import os
    os.environ.setdefault("HF_TOKEN", "test-dummy")
    from concinno.skills.public.agent import gaia_agent as ga
    assert callable(ga.solve_orthogonal_polygon_via_opencv_hybrid)
    assert callable(ga.solve_colour_coded_numeric_via_hybrid)
    assert callable(ga.solve_image_quiz_scoring_via_hybrid)
    # Private names still present (backward compat)
    assert callable(ga._solve_orthogonal_polygon_via_opencv_hybrid)
    assert callable(ga._solve_colour_coded_numeric_via_hybrid)
    assert callable(ga._solve_image_quiz_scoring_via_hybrid)
