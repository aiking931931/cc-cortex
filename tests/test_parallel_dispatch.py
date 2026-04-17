"""Tests for concinno.agent.parallel_dispatch."""

from __future__ import annotations

import pytest

from concinno.agent.fork_context import (
    FileStateCache,
    ForkDepthExceeded,
    SubagentContext,
    create_cache_safe_params,
)
from concinno.agent.parallel_dispatch import (
    COORDINATOR_PROMPT_SNIPPET,
    DEFAULT_MAX_FORK_DEPTH,
    DEFAULT_MAX_PARALLEL,
    DispatchPlan,
    DispatchRejected,
    ForkInForkRejected,
    ParallelDispatcher,
    ParallelLimitExceeded,
    SimpleFeatures,
    SpawnRequest,
    TeammateCannotSpawnBackground,
    TeammateCannotSpawnTeammate,
)

# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


class FakeFeatures:
    """Configurable DispatchFeatures double used by several tests."""

    def __init__(
        self,
        *,
        swarm: bool = True,
        fork: bool = True,
        coordinator: bool = False,
    ) -> None:
        self.swarm = swarm
        self.fork = fork
        self.coordinator = coordinator

    def swarm_enabled(self) -> bool:
        return self.swarm

    def fork_enabled(self) -> bool:
        return self.fork

    def coordinator_mode(self) -> bool:
        return self.coordinator


def make_parent_context(
    *,
    fork_depth: int = 0,
    is_teammate: bool = False,
) -> SubagentContext:
    """Tiny helper that builds a SubagentContext fixture."""
    params = create_cache_safe_params(
        system_prompt="SYS",
        tool_defs=[{"name": "Read"}, {"name": "Grep"}],
        betas=("beta-1",),
        effort="medium",
        parent_session_id="sess-1",
        fork_depth=fork_depth,
    )
    metadata: dict[str, object] = {}
    if is_teammate:
        metadata["is_teammate"] = True
    return SubagentContext(
        params=params,
        file_state=FileStateCache(),
        parent_transcript_ref="transcript:root",
        metadata=metadata,
    )


def req(**overrides: object) -> SpawnRequest:
    """Factory for SpawnRequests with sane defaults."""
    base: dict[str, object] = {
        "prompt": "do the thing",
        "description": "do thing",
        "subagent_type": "general-purpose",
    }
    base.update(overrides)
    return SpawnRequest(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Path selection tests
# --------------------------------------------------------------------------- #


def test_default_path_regular() -> None:
    d = ParallelDispatcher(features=FakeFeatures())
    plan = d.plan(req())
    assert plan.path == "regular"
    assert plan.subagent_context is None


def test_teammate_path_when_name_and_swarm() -> None:
    d = ParallelDispatcher(features=FakeFeatures(swarm=True))
    plan = d.plan(req(name="scout"))
    assert plan.path == "teammate"
    assert plan.subagent_context is None


def test_teammate_path_disabled_without_swarm() -> None:
    d = ParallelDispatcher(features=FakeFeatures(swarm=False, fork=False))
    plan = d.plan(req(name="scout"))
    # name set but swarm off → falls through to regular
    assert plan.path == "regular"
    assert any("swarm disabled" in n for n in plan.notes)


def test_teammate_cannot_spawn_teammate() -> None:
    parent = make_parent_context(is_teammate=True)
    d = ParallelDispatcher(parent_context=parent, features=FakeFeatures())
    with pytest.raises(TeammateCannotSpawnTeammate):
        d.plan(req(name="nested"))


def test_teammate_cannot_spawn_background() -> None:
    d = ParallelDispatcher(features=FakeFeatures())
    with pytest.raises(TeammateCannotSpawnBackground):
        d.plan(req(name="bg-team", run_in_background=True))


def test_fork_path_when_subagent_type_none_and_feature() -> None:
    parent = make_parent_context(fork_depth=0)
    d = ParallelDispatcher(parent_context=parent, features=FakeFeatures())
    plan = d.plan(req(subagent_type=None))
    assert plan.path == "fork"
    assert plan.subagent_context is not None
    assert plan.subagent_context.params.fork_depth == 1


def test_fork_path_falls_through_when_no_parent_context() -> None:
    d = ParallelDispatcher(parent_context=None, features=FakeFeatures())
    plan = d.plan(req(subagent_type=None))
    assert plan.path == "regular"
    assert any("no parent context" in n for n in plan.notes)


def test_fork_in_fork_rejected() -> None:
    parent = make_parent_context(fork_depth=1)  # already a fork child
    d = ParallelDispatcher(parent_context=parent, features=FakeFeatures())
    with pytest.raises(ForkInForkRejected):
        d.plan(req(subagent_type=None))


def test_fork_respects_max_depth() -> None:
    # Not yet in fork child (depth 0), but max_depth=0 so depth >= max.
    parent = make_parent_context(fork_depth=0)
    d = ParallelDispatcher(
        parent_context=parent,
        features=FakeFeatures(),
        max_fork_depth=0,
    )
    with pytest.raises(ForkDepthExceeded):
        d.plan(req(subagent_type=None))


def test_background_path_when_flag_set() -> None:
    d = ParallelDispatcher(features=FakeFeatures())
    plan = d.plan(req(run_in_background=True))
    assert plan.path == "background"


# --------------------------------------------------------------------------- #
# Prompt preparation tests
# --------------------------------------------------------------------------- #


def test_prepared_prompt_unchanged_default() -> None:
    d = ParallelDispatcher(features=FakeFeatures(coordinator=False))
    plan = d.plan(req(prompt="hello world"))
    assert plan.prepared_prompt == "hello world"


def test_coordinator_mode_prepends_snippet() -> None:
    d = ParallelDispatcher(features=FakeFeatures(coordinator=True))
    plan = d.plan(req(prompt="analyze repo"))
    assert plan.prepared_prompt.startswith(COORDINATOR_PROMPT_SNIPPET)
    assert "analyze repo" in plan.prepared_prompt


def test_coordinator_mode_skips_when_already_present() -> None:
    d = ParallelDispatcher(features=FakeFeatures(coordinator=True))
    handcrafted = "You are a COORDINATOR. Launch branches in parallel."
    plan = d.plan(req(prompt=handcrafted))
    assert plan.prepared_prompt == handcrafted


# --------------------------------------------------------------------------- #
# Batch / concurrency tests
# --------------------------------------------------------------------------- #


def test_plan_many_under_limit_returns_all() -> None:
    d = ParallelDispatcher(features=FakeFeatures(), max_parallel=5)
    plans = d.plan_many([req() for _ in range(3)])
    assert len(plans) == 3
    assert all(p.path == "regular" for p in plans)


def test_plan_many_over_limit_raises_parallel_limit() -> None:
    d = ParallelDispatcher(features=FakeFeatures(), max_parallel=2)
    with pytest.raises(ParallelLimitExceeded):
        d.plan_many([req() for _ in range(3)])


def test_mark_spawned_increments_inflight() -> None:
    d = ParallelDispatcher(features=FakeFeatures())
    plan = d.plan(req())
    assert d.stats()["in_flight"] == 0
    d.mark_spawned(plan)
    assert d.stats()["in_flight"] == 1
    assert d.stats()["total_spawned"] == 1


def test_mark_completed_decrements_inflight() -> None:
    d = ParallelDispatcher(features=FakeFeatures())
    plan = d.plan(req())
    d.mark_spawned(plan)
    d.mark_completed(plan)
    s = d.stats()
    assert s["in_flight"] == 0
    assert s["total_completed"] == 1


def test_stats_tracks_rejects_by_class() -> None:
    d = ParallelDispatcher(features=FakeFeatures())
    with pytest.raises(TeammateCannotSpawnBackground):
        d.plan(req(name="x", run_in_background=True))
    with pytest.raises(TeammateCannotSpawnBackground):
        d.plan(req(name="y", run_in_background=True))
    rejects = d.stats()["rejects_by_class"]
    assert rejects.get("TeammateCannotSpawnBackground") == 2


# --------------------------------------------------------------------------- #
# Snippet / constant tests
# --------------------------------------------------------------------------- #


def test_coordinator_prompt_snippet_contains_parallel_training() -> None:
    # The snippet must actually train for parallel tool_use blocks —
    # that's its entire reason for existence.
    assert "parallel" in COORDINATOR_PROMPT_SNIPPET.lower()
    assert "tool_use" in COORDINATOR_PROMPT_SNIPPET
    assert "single" in COORDINATOR_PROMPT_SNIPPET.lower()


def test_dispatcher_respects_custom_max_fork_depth() -> None:
    # At depth=0 with max=0, the fork arm must raise ForkDepthExceeded
    # — proves the custom limit is actually honored.
    parent = make_parent_context(fork_depth=0)
    strict = ParallelDispatcher(
        parent_context=parent,
        features=FakeFeatures(),
        max_fork_depth=0,
    )
    with pytest.raises(ForkDepthExceeded):
        strict.plan(req(subagent_type=None))

    # With a generous limit, the same request produces a fork plan.
    generous = ParallelDispatcher(
        parent_context=parent,
        features=FakeFeatures(),
        max_fork_depth=10,
    )
    plan = generous.plan(req(subagent_type=None))
    assert plan.path == "fork"


def test_all_reject_classes_subclass_dispatch_rejected() -> None:
    assert issubclass(TeammateCannotSpawnTeammate, DispatchRejected)
    assert issubclass(TeammateCannotSpawnBackground, DispatchRejected)
    assert issubclass(ForkInForkRejected, DispatchRejected)
    assert issubclass(ParallelLimitExceeded, DispatchRejected)


def test_simple_features_honors_flags() -> None:
    f = SimpleFeatures(swarm=False, fork=True, coordinator=True)
    assert f.swarm_enabled() is False
    assert f.fork_enabled() is True
    assert f.coordinator_mode() is True
    # Defaults
    d = SimpleFeatures()
    assert d.swarm_enabled() is True
    assert d.fork_enabled() is True
    assert d.coordinator_mode() is False


def test_fork_path_carries_subagent_context() -> None:
    parent = make_parent_context(fork_depth=0)
    d = ParallelDispatcher(parent_context=parent, features=FakeFeatures())
    plan = d.plan(req(subagent_type=None))
    assert plan.path == "fork"
    assert isinstance(plan.subagent_context, SubagentContext)
    # The child must carry a DEEPER cache-safe params fingerprint.
    assert plan.subagent_context.params.fork_depth == 1
    assert plan.subagent_context.params.system_prompt == "SYS"
    # file_state must be a CLONE, not the parent object.
    assert plan.subagent_context.file_state is not parent.file_state


def test_regular_path_when_subagent_type_explicit() -> None:
    parent = make_parent_context(fork_depth=0)
    d = ParallelDispatcher(parent_context=parent, features=FakeFeatures())
    plan = d.plan(req(subagent_type="code-reviewer"))
    # Explicit subagent_type short-circuits the fork path even though
    # fork is enabled and we have a parent context.
    assert plan.path == "regular"
    assert plan.subagent_context is None


# --------------------------------------------------------------------------- #
# Module constants sanity
# --------------------------------------------------------------------------- #


def test_module_constants_have_expected_defaults() -> None:
    assert DEFAULT_MAX_FORK_DEPTH == 3
    assert DEFAULT_MAX_PARALLEL == 10


def test_dispatch_plan_is_dataclass_instance() -> None:
    d = ParallelDispatcher(features=FakeFeatures())
    plan = d.plan(req())
    assert isinstance(plan, DispatchPlan)
    assert plan.request.prompt == "do the thing"
