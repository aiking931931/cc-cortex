"""Asymmetry — epistemic role differentiation for MAS 3-tier (Tier 1).

@module concinno.agent.asymmetry
@responsibility Plan **epistemic axes** across the solver / critic /
    judge roles so the three LLM calls do not collapse to the same
    sampling stream + reading posture. Without deliberate asymmetry the
    critic and judge degenerate into copies of the solver (M3 in the
    red-team) and a measured MAS lift is just "SAS + stylistic
    rewrite".

S5 verdict anchors (see
``_AI_BRAIN/05_Planning/mas-tier-overhaul-commander-verdict-2026-04-22.md``):

* **H1 ACCEPT downgrade** — 5 axes declared but only 3 are "new-novel".
  ``CONTEXT_SLICE`` and ``TOOL_SET`` are already enforced by
  :mod:`concinno.agent.mas_loop`'s signature (solver sees full question +
  owns the tool registry; critic/judge are text-only and see a
  truncated trace). Asking the plan to also "differ on TOOL_SET" double-
  counts. :meth:`AsymmetryPlan.assert_distinct` therefore counts
  distinctness only across ``{SEED_OFFSET, TEMP_DELTA, PROMPT_FRAME}``
  — the three truly new-novel axes.

* **H1 ACCEPT continued** — Per-axis ablation arms are expected at the
  pilot layer (runner exposes ``--ablation-arm seed|temp|prompt|full``).
  If ``full`` plan ≤ strongest single-axis variant → KILL asymmetry
  module (that verdict is a runner / analysis concern — the module just
  has to ship the axes).

Why this lives in Concinno (MEMORY #52 切點):
    Any agent runner eventually needs "make the critic actually
    different from the solver". The plan's shape (seed offsets /
    temperature delta / prompt frame) is benchmark-agnostic; the
    runner picks which axes to enable per pilot arm.

@dependencies stdlib only. No numpy, no LLM, no env reads.
@exports AsymmetryPlan, RolePlan, EpistemicAxis
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# ─────────────────────────── Axis enum ───────────────────────────


class EpistemicAxis(Enum):
    """The 5 declared epistemic axes.

    Only the first 3 are "new-novel" — they extend behaviour beyond
    what ``mas_loop`` already hardcodes. The last 2 are listed because
    they exist (and documenting them keeps the vocabulary aligned with
    the design doc) but :meth:`AsymmetryPlan.assert_distinct` rejects
    them as axis-count contributors, and :meth:`AsymmetryPlan.build`
    refuses to accept them in ``axes_enabled``.
    """

    SEED_OFFSET = "seed_offset"
    """Per-role integer seed offset on top of the outer sampler seed.

    Ships the per-role RNG stream divergence described by
    :func:`concinno.agent.mas_loop.derive_role_seeds`. Asymmetry can
    amplify those offsets (e.g. critic=+13 to span a bigger slice of
    sampler space when the outer seed is 42).
    """

    TEMP_DELTA = "temp_delta"
    """Per-role temperature perturbation forwarded via ``provider_extra.temperature``.

    Typical scales: solver untouched (δ=0.0), critic slightly cooler
    (δ=-0.1) so it stays literal when verifying, judge slightly warmer
    (δ=+0.05) to allow honest draw when the two candidates look tied.
    """

    PROMPT_FRAME = "prompt_frame"
    """Rotate the positional / rhetorical frame of role prompts.

    Not a prompt rewrite — a *frame tag* the caller drops into its
    prompt template. Example frames: ``"literal-reader"``,
    ``"challenger"``, ``"arbiter"``. The template substitutes these
    literally; they are not asked to pass through an LLM rewrite.
    """

    CONTEXT_SLICE = "context_slice"
    """Already-hardcoded by mas_loop: solver sees full question,
    critic sees truncated trace (``trace_top_k``), judge sees blind-
    labelled (response_1, response_2). NOT a new-novel axis.
    """

    TOOL_SET = "tool_set"
    """Already-hardcoded by mas_loop: solver owns tools, critic / judge
    are text-only via ``_build_text_only_llm_call``. NOT a new-novel
    axis.
    """


# Set of axes that contribute to ``assert_distinct`` counting.
NEW_NOVEL_AXES: frozenset[EpistemicAxis] = frozenset({
    EpistemicAxis.SEED_OFFSET,
    EpistemicAxis.TEMP_DELTA,
    EpistemicAxis.PROMPT_FRAME,
})

# Axes the mas_loop signature already bakes in; ``build`` refuses to
# accept them in ``axes_enabled`` so callers don't double-count.
HARDCODED_AXES: frozenset[EpistemicAxis] = frozenset({
    EpistemicAxis.CONTEXT_SLICE,
    EpistemicAxis.TOOL_SET,
})


# ─────────────────────────── RolePlan ───────────────────────────


@dataclass(frozen=True)
class RolePlan:
    """A per-role asymmetry spec.

    ``prompt_frame`` is a frame tag (short token the template swaps in),
    NOT a full rewritten prompt. Keeping it tag-shaped lets the adapter
    interpolate without needing per-role prompt files.

    ``context_slice_strategy`` and ``tool_set`` are kept for
    documentation — the actual slicing / tool gating happens inside
    ``mas_loop``. Callers typically leave these at the sensible defaults
    (``"full_question"`` / ``"text_only"``).
    """

    role_name: str
    """One of ``"solver"`` / ``"critic"`` / ``"judge"``."""

    seed_offset: int = 0
    """Integer offset added on top of :func:`derive_role_seeds`."""

    temp_delta: float = 0.0
    """Temperature delta applied to ``provider_extra.temperature``.
    Bounded at ``[-0.5, +0.5]`` by :meth:`AsymmetryPlan.build` — larger
    deltas flip temperature into territory that corrupts judge
    calibration empirically."""

    context_slice_strategy: str = "full_question"
    """Doc field. Real slicing lives in mas_loop."""

    tool_set: tuple[str, ...] = field(default_factory=tuple)
    """Doc field. Real tool gating lives in mas_loop."""

    prompt_frame: str = ""
    """Short frame tag (empty = "no frame override")."""


# ─────────────────────────── AsymmetryPlan ───────────────────────────


@dataclass(frozen=True)
class AsymmetryPlan:
    """Solver + critic + judge :class:`RolePlan` triple.

    Frozen so a plan can be passed into ``run_mas`` without worrying
    about mutation across async tasks. Construct via :meth:`build` in
    most cases; direct ``__init__`` is supported but does not apply
    axis-count sanity checks.
    """

    solver: RolePlan
    critic: RolePlan
    judge: RolePlan

    # ── Factory ──

    @classmethod
    def build(
        cls,
        solver_profile: dict[str, Any] | None = None,
        axes_enabled: set[EpistemicAxis] | frozenset[EpistemicAxis] | None = None,
    ) -> AsymmetryPlan:
        """Construct a plan with the requested new-novel axes enabled.

        Parameters
        ----------
        solver_profile : optional dict
            Anchor settings for the solver role. Recognised keys:
            ``seed_offset`` (int) / ``temp_delta`` (float) /
            ``prompt_frame`` (str) / ``tool_set`` (tuple[str, ...]).
            Unrecognised keys are ignored for forward-compat.
        axes_enabled : optional set
            Which new-novel axes the plan should actually vary across
            roles. Default: ``{SEED_OFFSET, TEMP_DELTA, PROMPT_FRAME}``
            (all 3 new-novel axes enabled → minimum-asymmetry plan that
            still passes :meth:`assert_distinct` at ``min_axes=2``).

            Passing ``CONTEXT_SLICE`` or ``TOOL_SET`` in ``axes_enabled``
            raises :class:`ValueError` — those axes are enforced by
            :mod:`concinno.agent.mas_loop`'s signature and
            double-counting them is how the phantom-code verdict
            happens.

        Returns
        -------
        AsymmetryPlan
            Immutable plan ready to hand to ``run_mas(asymmetry_plan=...)``.
        """
        profile = dict(solver_profile or {})
        enabled: frozenset[EpistemicAxis]
        if axes_enabled is None:
            enabled = NEW_NOVEL_AXES
        else:
            enabled = frozenset(axes_enabled)

        # S5 H1: reject the already-hardcoded axes early so the caller
        # sees the mistake before a pilot produces phantom lift.
        illegal = enabled & HARDCODED_AXES
        if illegal:
            names = ", ".join(sorted(a.value for a in illegal))
            raise ValueError(
                f"axes_enabled contains {{{names}}} — already enforced "
                "by mas_loop.py signature (solver sees full question + "
                "owns tools; critic/judge are text-only). Use only "
                "new-novel axes: "
                + ", ".join(sorted(a.value for a in NEW_NOVEL_AXES))
            )

        # Anchor solver_profile values (clamped).
        solver_seed_offset = int(profile.get("seed_offset", 0))
        solver_temp_delta = _clamp(float(profile.get("temp_delta", 0.0)), -0.5, 0.5)
        solver_tool_set = tuple(profile.get("tool_set", ()) or ())
        solver_frame = str(profile.get("prompt_frame", "solver-primary"))

        # Per-role offsets. ``SEED_OFFSET`` axis enables per-role
        # divergence; if not enabled, all three share the anchor.
        if EpistemicAxis.SEED_OFFSET in enabled:
            critic_seed_offset = solver_seed_offset + 13
            judge_seed_offset = solver_seed_offset + 29
        else:
            critic_seed_offset = solver_seed_offset
            judge_seed_offset = solver_seed_offset

        if EpistemicAxis.TEMP_DELTA in enabled:
            critic_temp_delta = _clamp(solver_temp_delta - 0.1, -0.5, 0.5)
            judge_temp_delta = _clamp(solver_temp_delta + 0.05, -0.5, 0.5)
        else:
            critic_temp_delta = solver_temp_delta
            judge_temp_delta = solver_temp_delta

        if EpistemicAxis.PROMPT_FRAME in enabled:
            critic_frame = "critic-challenger"
            judge_frame = "judge-arbiter"
        else:
            critic_frame = solver_frame
            judge_frame = solver_frame

        solver = RolePlan(
            role_name="solver",
            seed_offset=solver_seed_offset,
            temp_delta=solver_temp_delta,
            context_slice_strategy="full_question",
            tool_set=solver_tool_set,
            prompt_frame=solver_frame,
        )
        critic = RolePlan(
            role_name="critic",
            seed_offset=critic_seed_offset,
            temp_delta=critic_temp_delta,
            context_slice_strategy="truncated_trace",
            tool_set=(),
            prompt_frame=critic_frame,
        )
        judge = RolePlan(
            role_name="judge",
            seed_offset=judge_seed_offset,
            temp_delta=judge_temp_delta,
            context_slice_strategy="blind_labelled_pair",
            tool_set=(),
            prompt_frame=judge_frame,
        )
        return cls(solver=solver, critic=critic, judge=judge)

    # ── Assertions ──

    def assert_distinct(self, min_axes: int = 2) -> None:
        """Raise if fewer than ``min_axes`` new-novel axes actually differ.

        Only counts axes in :data:`NEW_NOVEL_AXES`. A plan where
        ``critic.seed_offset == solver.seed_offset`` and
        ``critic.temp_delta == solver.temp_delta`` but critic has a
        rotated ``prompt_frame`` has 1 distinct axis — fine for a
        prompt-only ablation arm but rejected by ``min_axes=2`` (the
        default "real asymmetry" guard).

        ``CONTEXT_SLICE`` and ``TOOL_SET`` are NOT counted even when
        they differ on paper — the mas_loop signature enforces them
        uniformly, so they're already part of the baseline, not part
        of any plan delta.
        """
        distinct_axes: set[EpistemicAxis] = set()
        if self.solver.seed_offset != self.critic.seed_offset:
            distinct_axes.add(EpistemicAxis.SEED_OFFSET)
        if not _close(self.solver.temp_delta, self.critic.temp_delta):
            distinct_axes.add(EpistemicAxis.TEMP_DELTA)
        if self.solver.prompt_frame != self.critic.prompt_frame:
            distinct_axes.add(EpistemicAxis.PROMPT_FRAME)

        # Guardrail — filter to new-novel axes so hardcoded ones don't
        # sneak in through future RolePlan field additions.
        distinct_axes &= NEW_NOVEL_AXES

        if len(distinct_axes) < int(min_axes):
            have = sorted(a.value for a in distinct_axes)
            raise ValueError(
                f"AsymmetryPlan distinctness failure: solver vs critic "
                f"differ on {len(distinct_axes)} new-novel axis/axes "
                f"({have}); min_axes={min_axes} required. Enable more "
                "axes in AsymmetryPlan.build(axes_enabled=...)."
            )


# ─────────────────────────── Helpers ───────────────────────────


def _clamp(x: float, lo: float, hi: float) -> float:
    """Clamp ``x`` to ``[lo, hi]`` without importing numpy."""
    return max(lo, min(hi, x))


def _close(a: float, b: float, eps: float = 1e-9) -> bool:
    """Float equality with a tiny epsilon.

    Temperature deltas round-trip through provider_extra serialisation
    so exact-equality comparisons would spuriously succeed after
    JSON → float reparse. ``eps=1e-9`` is orders of magnitude tighter
    than any meaningful temperature perturbation.
    """
    return abs(a - b) <= eps


__all__ = [
    "HARDCODED_AXES",
    "NEW_NOVEL_AXES",
    "AsymmetryPlan",
    "EpistemicAxis",
    "RolePlan",
]
