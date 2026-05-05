"""Tests for :mod:`concinno.agent.asymmetry` — epistemic differentiation.

S5 verdict anchors:
* H1 — 5 axes declared, only 3 "new-novel" counted by ``assert_distinct``.
  ``CONTEXT_SLICE`` / ``TOOL_SET`` rejected when included in
  ``axes_enabled`` (already baked into mas_loop.py signature).
* H1 per-axis ablation — build() lets callers enable any single axis
  to support the runner's ``--ablation-arm seed|temp|prompt|full`` grid.
"""

from __future__ import annotations

import pytest

from concinno.agent.asymmetry import (
    HARDCODED_AXES,
    NEW_NOVEL_AXES,
    AsymmetryPlan,
    EpistemicAxis,
    RolePlan,
)

# ─────────────────────────── EpistemicAxis enum ───────────────────────────


class TestEpistemicAxisEnum:
    """The enum declares exactly the 5 axes the design doc lists."""

    def test_enum_has_five_axes(self) -> None:
        assert len(EpistemicAxis) == 5

    def test_enum_contains_seed_offset(self) -> None:
        assert EpistemicAxis.SEED_OFFSET.value == "seed_offset"

    def test_enum_contains_temp_delta(self) -> None:
        assert EpistemicAxis.TEMP_DELTA.value == "temp_delta"

    def test_enum_contains_prompt_frame(self) -> None:
        assert EpistemicAxis.PROMPT_FRAME.value == "prompt_frame"

    def test_enum_contains_context_slice(self) -> None:
        assert EpistemicAxis.CONTEXT_SLICE.value == "context_slice"

    def test_enum_contains_tool_set(self) -> None:
        assert EpistemicAxis.TOOL_SET.value == "tool_set"


class TestAxisGroupings:
    def test_new_novel_axes_has_three(self) -> None:
        assert len(NEW_NOVEL_AXES) == 3

    def test_new_novel_axes_membership(self) -> None:
        assert EpistemicAxis.SEED_OFFSET in NEW_NOVEL_AXES
        assert EpistemicAxis.TEMP_DELTA in NEW_NOVEL_AXES
        assert EpistemicAxis.PROMPT_FRAME in NEW_NOVEL_AXES

    def test_hardcoded_axes_has_two(self) -> None:
        assert len(HARDCODED_AXES) == 2

    def test_hardcoded_axes_membership(self) -> None:
        assert EpistemicAxis.CONTEXT_SLICE in HARDCODED_AXES
        assert EpistemicAxis.TOOL_SET in HARDCODED_AXES

    def test_groupings_disjoint(self) -> None:
        assert not (NEW_NOVEL_AXES & HARDCODED_AXES)

    def test_groupings_cover_all_axes(self) -> None:
        assert NEW_NOVEL_AXES | HARDCODED_AXES == set(EpistemicAxis)


# ─────────────────────────── RolePlan ───────────────────────────


class TestRolePlan:
    def test_frozen(self) -> None:
        p = RolePlan(role_name="solver")
        with pytest.raises((AttributeError, Exception)):
            p.seed_offset = 99  # type: ignore[misc]

    def test_default_values(self) -> None:
        p = RolePlan(role_name="solver")
        assert p.seed_offset == 0
        assert p.temp_delta == 0.0
        assert p.context_slice_strategy == "full_question"
        assert p.tool_set == ()
        assert p.prompt_frame == ""

    def test_override_fields(self) -> None:
        p = RolePlan(
            role_name="critic",
            seed_offset=7,
            temp_delta=-0.2,
            prompt_frame="challenger",
        )
        assert p.role_name == "critic"
        assert p.seed_offset == 7
        assert p.temp_delta == pytest.approx(-0.2)
        assert p.prompt_frame == "challenger"


# ─────────────────────────── AsymmetryPlan.build happy paths ───────────────────────────


class TestBuildDefaults:
    """``build()`` with no args produces a 3-axis new-novel plan."""

    def test_default_axes_enable_all_new_novel(self) -> None:
        plan = AsymmetryPlan.build()
        # Seed offset set
        assert plan.solver.seed_offset == 0
        assert plan.critic.seed_offset == 13
        assert plan.judge.seed_offset == 29

    def test_default_temp_delta(self) -> None:
        plan = AsymmetryPlan.build()
        assert plan.solver.temp_delta == pytest.approx(0.0)
        assert plan.critic.temp_delta == pytest.approx(-0.1)
        assert plan.judge.temp_delta == pytest.approx(0.05)

    def test_default_prompt_frames_differ(self) -> None:
        plan = AsymmetryPlan.build()
        assert plan.solver.prompt_frame != plan.critic.prompt_frame
        assert plan.critic.prompt_frame != plan.judge.prompt_frame

    def test_solver_profile_passthrough_seed(self) -> None:
        plan = AsymmetryPlan.build(solver_profile={"seed_offset": 42})
        assert plan.solver.seed_offset == 42
        # Critic / judge offsets relative to solver
        assert plan.critic.seed_offset == 42 + 13
        assert plan.judge.seed_offset == 42 + 29

    def test_solver_profile_tool_set_is_tuple(self) -> None:
        plan = AsymmetryPlan.build(
            solver_profile={"tool_set": ("web_search", "read_file")},
        )
        assert plan.solver.tool_set == ("web_search", "read_file")
        # Critic and judge are text-only
        assert plan.critic.tool_set == ()
        assert plan.judge.tool_set == ()


# ─────────────────────────── Build axis ablations ───────────────────────────


class TestAxisAblations:
    """Enable exactly one new-novel axis at a time — supports runner arms."""

    def test_seed_only_arm(self) -> None:
        plan = AsymmetryPlan.build(
            axes_enabled={EpistemicAxis.SEED_OFFSET},
        )
        # Seeds differ
        assert plan.solver.seed_offset != plan.critic.seed_offset
        # Temp / frame the same
        assert plan.solver.temp_delta == plan.critic.temp_delta
        assert plan.solver.prompt_frame == plan.critic.prompt_frame

    def test_temp_only_arm(self) -> None:
        plan = AsymmetryPlan.build(
            axes_enabled={EpistemicAxis.TEMP_DELTA},
        )
        assert plan.solver.seed_offset == plan.critic.seed_offset
        assert plan.solver.temp_delta != plan.critic.temp_delta
        assert plan.solver.prompt_frame == plan.critic.prompt_frame

    def test_prompt_only_arm(self) -> None:
        plan = AsymmetryPlan.build(
            axes_enabled={EpistemicAxis.PROMPT_FRAME},
        )
        assert plan.solver.seed_offset == plan.critic.seed_offset
        assert plan.solver.temp_delta == plan.critic.temp_delta
        assert plan.solver.prompt_frame != plan.critic.prompt_frame

    def test_empty_axes_set_is_identity(self) -> None:
        """Empty set = identity plan (solver / critic / judge identical on new-novel axes)."""
        plan = AsymmetryPlan.build(axes_enabled=set())
        assert plan.solver.seed_offset == plan.critic.seed_offset == plan.judge.seed_offset
        assert plan.solver.temp_delta == plan.critic.temp_delta == plan.judge.temp_delta
        assert plan.solver.prompt_frame == plan.critic.prompt_frame == plan.judge.prompt_frame


# ─────────────────────────── Hardcoded-axis rejection ───────────────────────────


class TestRejectHardcodedAxes:
    """S5 H1: CONTEXT_SLICE / TOOL_SET in axes_enabled → ValueError."""

    def test_context_slice_in_axes_enabled_raises(self) -> None:
        with pytest.raises(ValueError) as exc:
            AsymmetryPlan.build(axes_enabled={EpistemicAxis.CONTEXT_SLICE})
        assert "mas_loop" in str(exc.value).lower()

    def test_tool_set_in_axes_enabled_raises(self) -> None:
        with pytest.raises(ValueError) as exc:
            AsymmetryPlan.build(axes_enabled={EpistemicAxis.TOOL_SET})
        assert "mas_loop" in str(exc.value).lower()

    def test_mixed_legal_and_illegal_axes_raises(self) -> None:
        with pytest.raises(ValueError):
            AsymmetryPlan.build(
                axes_enabled={
                    EpistemicAxis.SEED_OFFSET,
                    EpistemicAxis.CONTEXT_SLICE,
                },
            )

    def test_error_names_illegal_axes(self) -> None:
        with pytest.raises(ValueError) as exc:
            AsymmetryPlan.build(
                axes_enabled={
                    EpistemicAxis.CONTEXT_SLICE,
                    EpistemicAxis.TOOL_SET,
                },
            )
        msg = str(exc.value)
        assert "context_slice" in msg
        assert "tool_set" in msg


# ─────────────────────────── Determinism ───────────────────────────


class TestDeterminism:
    """Same inputs → same plan. Matters for paired-seed McNemar."""

    def test_same_inputs_produce_same_seeds(self) -> None:
        plan_a = AsymmetryPlan.build(solver_profile={"seed_offset": 100})
        plan_b = AsymmetryPlan.build(solver_profile={"seed_offset": 100})
        assert plan_a.critic.seed_offset == plan_b.critic.seed_offset

    def test_same_inputs_produce_same_temp_deltas(self) -> None:
        plan_a = AsymmetryPlan.build()
        plan_b = AsymmetryPlan.build()
        assert plan_a.critic.temp_delta == plan_b.critic.temp_delta


# ─────────────────────────── Temperature bounds ───────────────────────────


class TestTempDeltaBounds:
    """Temperature delta clamped to [-0.5, 0.5] — larger flips behaviour."""

    def test_oversized_solver_delta_clamped(self) -> None:
        plan = AsymmetryPlan.build(solver_profile={"temp_delta": 5.0})
        assert plan.solver.temp_delta == pytest.approx(0.5)

    def test_oversized_negative_delta_clamped(self) -> None:
        plan = AsymmetryPlan.build(solver_profile={"temp_delta": -5.0})
        assert plan.solver.temp_delta == pytest.approx(-0.5)

    def test_critic_judge_deltas_stay_bounded(self) -> None:
        plan = AsymmetryPlan.build(solver_profile={"temp_delta": 0.45})
        assert -0.5 <= plan.critic.temp_delta <= 0.5
        assert -0.5 <= plan.judge.temp_delta <= 0.5


# ─────────────────────────── assert_distinct ───────────────────────────


class TestAssertDistinct:
    """``assert_distinct`` counts only new-novel axes."""

    def test_full_plan_passes_default_min_axes(self) -> None:
        plan = AsymmetryPlan.build()
        # Should not raise
        plan.assert_distinct(min_axes=2)

    def test_full_plan_passes_min_axes_3(self) -> None:
        plan = AsymmetryPlan.build()
        plan.assert_distinct(min_axes=3)

    def test_empty_plan_fails_min_axes_2(self) -> None:
        plan = AsymmetryPlan.build(axes_enabled=set())
        with pytest.raises(ValueError) as exc:
            plan.assert_distinct(min_axes=2)
        assert "distinctness" in str(exc.value).lower()

    def test_seed_only_plan_fails_min_axes_2(self) -> None:
        """Only 1 new-novel axis varies — below default threshold."""
        plan = AsymmetryPlan.build(
            axes_enabled={EpistemicAxis.SEED_OFFSET},
        )
        with pytest.raises(ValueError):
            plan.assert_distinct(min_axes=2)

    def test_prompt_only_plan_fails_min_axes_2(self) -> None:
        plan = AsymmetryPlan.build(
            axes_enabled={EpistemicAxis.PROMPT_FRAME},
        )
        with pytest.raises(ValueError):
            plan.assert_distinct(min_axes=2)

    def test_seed_and_prompt_pair_passes_min_axes_2(self) -> None:
        plan = AsymmetryPlan.build(
            axes_enabled={
                EpistemicAxis.SEED_OFFSET,
                EpistemicAxis.PROMPT_FRAME,
            },
        )
        plan.assert_distinct(min_axes=2)

    def test_temp_only_plan_fails_min_axes_2(self) -> None:
        """Pure temperature-jitter variant (red-team M3 baseline) fails."""
        plan = AsymmetryPlan.build(
            axes_enabled={EpistemicAxis.TEMP_DELTA},
        )
        with pytest.raises(ValueError):
            plan.assert_distinct(min_axes=2)

    def test_min_axes_one_accepts_single_axis(self) -> None:
        plan = AsymmetryPlan.build(
            axes_enabled={EpistemicAxis.TEMP_DELTA},
        )
        plan.assert_distinct(min_axes=1)

    def test_error_message_lists_observed_axes(self) -> None:
        plan = AsymmetryPlan.build(
            axes_enabled={EpistemicAxis.PROMPT_FRAME},
        )
        with pytest.raises(ValueError) as exc:
            plan.assert_distinct(min_axes=2)
        msg = str(exc.value)
        assert "prompt_frame" in msg


# ─────────────────────────── Construction shape ───────────────────────────


class TestConstruction:
    def test_direct_construction_from_roleplans(self) -> None:
        s = RolePlan(role_name="solver")
        c = RolePlan(role_name="critic", seed_offset=7)
        j = RolePlan(role_name="judge", seed_offset=14)
        plan = AsymmetryPlan(solver=s, critic=c, judge=j)
        assert plan.solver is s
        assert plan.critic is c
        assert plan.judge is j

    def test_asymmetry_plan_frozen(self) -> None:
        plan = AsymmetryPlan.build()
        with pytest.raises((AttributeError, Exception)):
            plan.solver = plan.critic  # type: ignore[misc]

    def test_role_name_consistency(self) -> None:
        plan = AsymmetryPlan.build()
        assert plan.solver.role_name == "solver"
        assert plan.critic.role_name == "critic"
        assert plan.judge.role_name == "judge"


# ─────────────────────────── Forbidden imports regression ───────────────────────────


class TestForbiddenImports:
    """Regression: asymmetry module is deterministic stdlib only."""

    @staticmethod
    def _import_lines() -> list[str]:
        import inspect

        import concinno.agent.asymmetry as m
        src = inspect.getsource(m)
        return [
            line.strip().lower()
            for line in src.splitlines()
            if line.lstrip().startswith(("import ", "from "))
        ]

    def test_no_sklearn(self) -> None:
        for line in self._import_lines():
            assert "sklearn" not in line

    def test_no_numpy(self) -> None:
        for line in self._import_lines():
            assert "numpy" not in line

    def test_no_scipy(self) -> None:
        for line in self._import_lines():
            assert "scipy" not in line
