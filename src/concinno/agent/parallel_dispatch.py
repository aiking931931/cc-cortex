"""concinno.agent.parallel_dispatch — Subagent dispatch router.

@module agent.parallel_dispatch
@responsibility Decide which of four spawn paths (teammate / fork /
    regular / background) a subagent request takes, and enforce the
    invariants that Claude Code's AgentTool enforces at call time.
    Pure policy layer — this module NEVER spawns subprocesses, never
    calls an LLM, never touches the network. It produces a
    :class:`DispatchPlan` object describing WHAT should be spawned;
    the caller is responsible for actually doing the spawn.

    Also exports :data:`COORDINATOR_PROMPT_SNIPPET`, the coordinator
    mode prompt pattern that trains Claude to emit multiple
    ``tool_use`` blocks in a single assistant message. This is the
    single most important CC parallelism insight: parallelism is
    MODEL-driven via prompt training, not code-driven via asyncio.
    Libraries wanting CC-style parallelism inject this into their
    assistant system prompt and then use this dispatcher to route
    each resulting spawn request.

@dependencies stdlib only + :mod:`concinno.agent.fork_context`
@exports SpawnPath, SpawnRequest, DispatchPlan, DispatchFeatures,
    SimpleFeatures, ParallelDispatcher, DispatchRejected,
    TeammateCannotSpawnTeammate, TeammateCannotSpawnBackground,
    ForkInForkRejected, ParallelLimitExceeded,
    COORDINATOR_PROMPT_SNIPPET, DEFAULT_MAX_FORK_DEPTH,
    DEFAULT_MAX_PARALLEL

Ported from Claude Code's TypeScript source:
  - tools/AgentTool/AgentTool.tsx  (call() lines 239-420 — path
    selection, teammate guard, fork guard, background gating)
  - tools/AgentTool/prompt.ts  (lines 240-286 — concurrency training
    prompt, foreground/background semantics)
  - tools/AgentTool/forkSubagent.ts  (fork path CacheSafeParams
    inheritance)
  - coordinator/coordinatorMode.ts  (coordinator-mode prompt gating)

Design notes:
  - Four spawn paths map to four input shapes. Path selection is a
    priority cascade: teammate (if name + swarm) → fork (if no
    subagent_type + fork feature) → background (if flag set) →
    regular. The cascade is deterministic — no hidden state.
  - Teammate guards mirror CC's flat-roster invariant: a teammate
    cannot spawn another teammate, and a teammate cannot spawn a
    background agent. These are enforced here by inspecting
    ``parent_context.metadata["is_teammate"]`` (or the request's own
    metadata when the dispatcher is running at the outer level with
    no parent context — useful for driving the dispatcher from a
    test harness).
  - Fork guards: :func:`is_in_fork_child` from
    :mod:`concinno.agent.fork_context` checks depth > 0. Fork
    without a parent context is silently downgraded to the regular
    path (with a diagnostic note) because there is nothing to inherit
    from — this mirrors CC's behavior when the fork feature is gated
    off and ``GENERAL_PURPOSE_AGENT`` is used instead.
  - In-flight tracking is caller-driven. The dispatcher does NOT own
    any locks, threads, or async primitives. Callers that need
    concurrency-safety must wrap :meth:`ParallelDispatcher.plan_many`,
    :meth:`mark_spawned`, and :meth:`mark_completed` in their own
    synchronisation. This is intentional: the module has to be usable
    from both sync and async harnesses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, Sequence, runtime_checkable

from concinno.agent.fork_context import (
    CacheSafeParams,
    ForkDepthExceeded,
    SubagentContext,
    create_subagent_context,
    is_in_fork_child,
)

__all__ = [
    "COORDINATOR_PROMPT_SNIPPET",
    "DEFAULT_MAX_FORK_DEPTH",
    "DEFAULT_MAX_PARALLEL",
    "DispatchFeatures",
    "DispatchPlan",
    "DispatchRejected",
    "ForkInForkRejected",
    "ParallelDispatcher",
    "ParallelLimitExceeded",
    "SimpleFeatures",
    "SpawnPath",
    "SpawnRequest",
    "TeammateCannotSpawnBackground",
    "TeammateCannotSpawnTeammate",
]

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

SpawnPath = Literal["teammate", "fork", "regular", "background"]
"""The four mutually exclusive spawn paths this dispatcher routes to."""

DEFAULT_MAX_FORK_DEPTH: int = 3
"""Default maximum fork chain depth. Root = 0, first fork = 1, etc."""

DEFAULT_MAX_PARALLEL: int = 10
"""Default maximum number of concurrently in-flight spawns across all
paths. Matches CC's observed practical ceiling before the UI becomes
unreadable and rate limits start biting."""

COORDINATOR_PROMPT_SNIPPET: str = """\
When you need to run multiple independent investigations or tasks,
launch them in parallel by emitting multiple tool_use blocks in a
single assistant message. Do NOT run them serially across turns.

- Foreground parallel: multiple tool_use blocks in one message; the
  harness awaits all before the next assistant turn.
- Background parallel: set run_in_background=true on each block; the
  harness delivers results later as user-role task-notification blocks.

Prefer background when any branch might take >30s. Use foreground
for read-only investigations that return quickly.
"""
"""Coordinator-mode prompt pattern. Inject into the assistant system
prompt to train the model to emit parallel ``tool_use`` blocks. This
is what makes CC-style parallelism actually work — without this
training, the model defaults to serialising tool calls across turns
and the dispatcher never sees a parallel batch."""


# --------------------------------------------------------------------------- #
# Request / plan data
# --------------------------------------------------------------------------- #


@dataclass
class SpawnRequest:
    """What a caller hands the dispatcher.

    The four path-selection inputs are ``name`` (teammate trigger),
    ``subagent_type`` (None triggers fork eligibility),
    ``run_in_background`` (background trigger), and
    :class:`DispatchFeatures` (which gates can fire). All other fields
    are forwarded verbatim onto the :class:`DispatchPlan` for the
    caller's own bookkeeping.

    Attributes:
        prompt: The task prompt the spawned agent will receive.
        description: A short 3-5 word summary. Optional but callers
            should supply it for UI display.
        subagent_type: Explicit agent type. ``None`` makes the request
            eligible for the fork path when the fork feature is on.
        name: Teammate name. When set AND the swarm feature is on,
            routes to the teammate path.
        model: Optional model override (e.g. ``"sonnet"``).
        run_in_background: When ``True`` and not already routed as
            teammate/fork, routes to the background path.
        team_name: Team name for teammate spawns.
        mode: Permission mode (e.g. ``"plan"``).
        metadata: Free-form dict. ``metadata["is_teammate"] = True``
            at the outer level forces the dispatcher to treat the
            request as coming from a teammate context, enabling the
            flat-roster guard even without a ``parent_context``.
    """

    prompt: str
    description: str = ""
    subagent_type: str | None = None
    name: str | None = None
    model: str | None = None
    run_in_background: bool = False
    team_name: str | None = None
    mode: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DispatchPlan:
    """What the dispatcher returns. Caller uses this to actually spawn.

    Attributes:
        path: Which of the four paths was selected.
        request: The original request, unchanged.
        subagent_context: For the ``"fork"`` path, a fresh
            :class:`SubagentContext` cloned from the parent with
            ``fork_depth`` incremented. ``None`` for every other path.
        prepared_prompt: The request prompt with any harness preamble
            prepended (e.g. the coordinator snippet).
        notes: Tuple of short diagnostic strings explaining why this
            path was chosen. Useful for logging and tests.
    """

    path: SpawnPath
    request: SpawnRequest
    subagent_context: SubagentContext | None
    prepared_prompt: str
    notes: tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# Reject exceptions
# --------------------------------------------------------------------------- #


class DispatchRejected(RuntimeError):
    """Base for all reject-by-invariant errors raised by the dispatcher.

    Catch this if you want to treat any dispatcher rejection
    uniformly; catch a specific subclass to distinguish teammate
    invariants from fork invariants from concurrency limits.
    """


class TeammateCannotSpawnTeammate(DispatchRejected):
    """A teammate is trying to spawn another teammate.

    The flat-roster invariant says every teammate reports to the
    team lead, not to another teammate. Nested teammates land in the
    roster with no provenance and confuse the lead's routing. Ported
    from ``AgentTool.tsx`` L272-L274.
    """


class TeammateCannotSpawnBackground(DispatchRejected):
    """A teammate is trying to spawn a background agent.

    An in-process teammate's lifecycle is bound to the team lead's
    process; a background agent outliving that process would become
    unreachable. Ported from ``AgentTool.tsx`` L277-L280.
    """


class ForkInForkRejected(DispatchRejected):
    """A fork child is trying to spawn another fork.

    Recursive forking would chain cache-inheritance relationships in
    a way that makes ``fork_depth`` accounting ambiguous and quickly
    blows past any sane ``max_fork_depth``. Ported from
    ``AgentTool.tsx`` L325-L333 (the ``isInForkChild`` guard).
    """


class ParallelLimitExceeded(DispatchRejected):
    """Too many concurrent spawns in flight.

    Raised by :meth:`ParallelDispatcher.plan_many` when the current
    in-flight count plus the batch size would exceed
    ``max_parallel``. Not raised by single-request :meth:`plan`
    because a single spawn is always allowed past the soft ceiling
    (the caller may intentionally be replacing one that just
    completed).
    """


# --------------------------------------------------------------------------- #
# Feature gates
# --------------------------------------------------------------------------- #


@runtime_checkable
class DispatchFeatures(Protocol):
    """Caller-provided feature gates.

    CCC does NOT hard-code CC's feature flags because CC's gating
    infrastructure (GrowthBook, internal env vars) is not portable.
    Libraries consuming this dispatcher implement the protocol using
    whatever gating mechanism they have, or they use the convenience
    :class:`SimpleFeatures` class below.
    """

    def swarm_enabled(self) -> bool:
        """Return ``True`` if the swarm/teammate feature is available.

        When ``False``, requests with ``name`` set skip the teammate
        path and fall through to fork/background/regular routing."""
        ...

    def fork_enabled(self) -> bool:
        """Return ``True`` if the fork-subagent feature is available.

        When ``False``, requests with ``subagent_type=None`` do not
        take the fork path — they fall through to regular routing
        using the general-purpose agent type."""
        ...

    def coordinator_mode(self) -> bool:
        """Return ``True`` if coordinator mode is on.

        When ``True``, the dispatcher prepends
        :data:`COORDINATOR_PROMPT_SNIPPET` to every prepared prompt
        that doesn't already contain the ``COORDINATOR`` marker."""
        ...


@dataclass
class SimpleFeatures:
    """Default :class:`DispatchFeatures` implementation.

    Convenience for callers that don't want a full Protocol impl.
    ``swarm`` and ``fork`` default to ``True`` so library users get
    the full set of paths unless they opt out. ``coordinator``
    defaults to ``False`` so the coordinator snippet is NOT injected
    by default — callers who understand the training implication
    opt in explicitly.
    """

    swarm: bool = True
    fork: bool = True
    coordinator: bool = False

    def swarm_enabled(self) -> bool:
        return self.swarm

    def fork_enabled(self) -> bool:
        return self.fork

    def coordinator_mode(self) -> bool:
        return self.coordinator


# --------------------------------------------------------------------------- #
# Dispatcher
# --------------------------------------------------------------------------- #


class ParallelDispatcher:
    """Route subagent spawn requests to the correct path.

    A dispatcher instance owns:
      - An optional ``parent_context`` describing the caller's fork
        depth and metadata (e.g. whether the caller is itself a
        teammate). ``None`` at the outer level.
      - A :class:`DispatchFeatures` bundle gating which paths are
        even eligible.
      - ``max_fork_depth`` and ``max_parallel`` thresholds.
      - A small in-flight counter driven by ``mark_spawned`` and
        ``mark_completed``. This is the ONLY mutable state on the
        dispatcher.

    The dispatcher is NOT thread-safe. Callers running it from
    concurrent contexts must wrap the counter-mutating methods in
    their own lock.
    """

    def __init__(
        self,
        *,
        parent_context: SubagentContext | None = None,
        features: DispatchFeatures | None = None,
        max_fork_depth: int = DEFAULT_MAX_FORK_DEPTH,
        max_parallel: int = DEFAULT_MAX_PARALLEL,
    ) -> None:
        if max_fork_depth < 0:
            raise ValueError(f"max_fork_depth must be >= 0, got {max_fork_depth}")
        if max_parallel < 1:
            raise ValueError(f"max_parallel must be >= 1, got {max_parallel}")
        self._parent_context: SubagentContext | None = parent_context
        self._features: DispatchFeatures = features or SimpleFeatures()
        self._max_fork_depth: int = max_fork_depth
        self._max_parallel: int = max_parallel
        self._in_flight: int = 0
        self._total_spawned: int = 0
        self._total_completed: int = 0
        self._rejects_by_class: dict[str, int] = {}

    # ----- path selection -------------------------------------------------- #

    def plan(self, request: SpawnRequest) -> DispatchPlan:
        """Route a single request to its spawn path.

        The cascade is:

          1. Teammate eligible (``name`` set AND swarm feature on)?
             - Caller already a teammate?
               ``TeammateCannotSpawnTeammate``.
             - Request asks for background? ``TeammateCannotSpawnBackground``.
             - Else → ``path="teammate"``.

          2. Fork eligible (``subagent_type`` None AND fork feature on)?
             - Caller is in a fork child? ``ForkInForkRejected``.
             - No parent context? Fall through to regular with a
               diagnostic note (nothing to inherit from).
             - ``fork_depth >= max_fork_depth``? :class:`ForkDepthExceeded`.
             - Else → ``path="fork"`` with a fresh child context.

          3. Background requested? → ``path="background"``.

          4. Default → ``path="regular"``.

        Raises:
            TeammateCannotSpawnTeammate: flat-roster invariant broken.
            TeammateCannotSpawnBackground: teammate background invariant.
            ForkInForkRejected: recursive fork attempt.
            ForkDepthExceeded: max_fork_depth exceeded.
        """
        notes: list[str] = []
        caller_is_teammate = self._caller_is_teammate(request)

        # Path 1: teammate
        if request.name is not None and self._features.swarm_enabled():
            if caller_is_teammate:
                self._record_reject(TeammateCannotSpawnTeammate)
                raise TeammateCannotSpawnTeammate(
                    "Teammates cannot spawn other teammates — the team "
                    "roster is flat. Omit `name` to spawn a subagent instead."
                )
            if request.run_in_background:
                self._record_reject(TeammateCannotSpawnBackground)
                raise TeammateCannotSpawnBackground(
                    "Teammates cannot spawn background agents. Use "
                    "run_in_background=False for synchronous subagents."
                )
            notes.append("name set + swarm enabled → teammate path")
            return DispatchPlan(
                path="teammate",
                request=request,
                subagent_context=None,
                prepared_prompt=self._prepare_prompt(request),
                notes=tuple(notes),
            )
        if request.name is not None and not self._features.swarm_enabled():
            notes.append("name set but swarm disabled → fall through")

        # Path 2: fork
        if request.subagent_type is None and self._features.fork_enabled():
            if is_in_fork_child(self._parent_context):
                self._record_reject(ForkInForkRejected)
                raise ForkInForkRejected(
                    "Fork is not available inside a forked worker. "
                    "Complete your task directly using your tools."
                )
            if self._parent_context is None:
                notes.append(
                    "fork eligible but no parent context → regular fallback"
                )
            else:
                current_depth = self._parent_context.params.fork_depth
                if current_depth >= self._max_fork_depth:
                    self._record_reject(ForkDepthExceeded)
                    raise ForkDepthExceeded(
                        f"Fork depth {current_depth} would exceed "
                        f"max_fork_depth={self._max_fork_depth}."
                    )
                child_ctx = self._build_child_context()
                notes.append(
                    f"subagent_type=None + fork enabled → fork path "
                    f"(depth {child_ctx.params.fork_depth})"
                )
                return DispatchPlan(
                    path="fork",
                    request=request,
                    subagent_context=child_ctx,
                    prepared_prompt=self._prepare_prompt(request),
                    notes=tuple(notes),
                )
        elif request.subagent_type is None and not self._features.fork_enabled():
            notes.append(
                "subagent_type=None but fork disabled → regular (general-purpose)"
            )

        # Path 3: background
        if request.run_in_background:
            # Background-from-teammate would have been caught by path 1
            # above ONLY if the request also had `name` set. A teammate
            # spawning a nameless background agent is ALSO illegal under
            # CC's invariant, so guard here too.
            if caller_is_teammate:
                self._record_reject(TeammateCannotSpawnBackground)
                raise TeammateCannotSpawnBackground(
                    "Teammates cannot spawn background agents. Use "
                    "run_in_background=False for synchronous subagents."
                )
            notes.append("run_in_background=True → background path")
            return DispatchPlan(
                path="background",
                request=request,
                subagent_context=None,
                prepared_prompt=self._prepare_prompt(request),
                notes=tuple(notes),
            )

        # Path 4: regular
        notes.append("default fallback → regular path")
        return DispatchPlan(
            path="regular",
            request=request,
            subagent_context=None,
            prepared_prompt=self._prepare_prompt(request),
            notes=tuple(notes),
        )

    def plan_many(self, requests: Sequence[SpawnRequest]) -> list[DispatchPlan]:
        """Batch variant of :meth:`plan`.

        Checks that ``in_flight + len(batch) <= max_parallel`` BEFORE
        planning any request. If the batch would overflow, raises
        :class:`ParallelLimitExceeded` without mutating any state or
        planning any request in the batch.

        Individual per-request rejections still surface as their
        specific exception types.
        """
        projected = self._in_flight + len(requests)
        if projected > self._max_parallel:
            self._record_reject(ParallelLimitExceeded)
            raise ParallelLimitExceeded(
                f"Batch of {len(requests)} plus {self._in_flight} in-flight "
                f"would exceed max_parallel={self._max_parallel}."
            )
        return [self.plan(req) for req in requests]

    # ----- in-flight tracking --------------------------------------------- #

    def mark_spawned(self, plan: DispatchPlan) -> None:
        """Record that ``plan`` has been handed off to a real spawner.

        Increments the in-flight counter and the cumulative spawn
        total. Call this from the caller immediately after the
        underlying spawn primitive returns a handle.
        """
        del plan  # currently unused; reserved for per-path bookkeeping
        self._in_flight += 1
        self._total_spawned += 1

    def mark_completed(self, plan: DispatchPlan) -> None:
        """Record that a previously spawned plan has finished.

        Decrements the in-flight counter (clamped at zero so a
        double-complete does not underflow) and increments the
        cumulative completion total.
        """
        del plan
        if self._in_flight > 0:
            self._in_flight -= 1
        self._total_completed += 1

    def stats(self) -> dict[str, Any]:
        """Return a snapshot of dispatcher counters.

        Keys:
            ``in_flight``: currently running spawns.
            ``total_spawned``: cumulative :meth:`mark_spawned` calls.
            ``total_completed``: cumulative :meth:`mark_completed` calls.
            ``rejects_by_class``: dict mapping exception class name to
                the number of times that rejection fired.
        """
        return {
            "in_flight": self._in_flight,
            "total_spawned": self._total_spawned,
            "total_completed": self._total_completed,
            "rejects_by_class": dict(self._rejects_by_class),
        }

    # ----- internals ------------------------------------------------------- #

    def _caller_is_teammate(self, request: SpawnRequest) -> bool:
        """Decide whether the caller is itself a teammate.

        Source of truth priority:
          1. ``parent_context.metadata["is_teammate"]`` — authoritative
             when a parent context is threaded through.
          2. ``request.metadata["is_teammate"]`` — outer-level override
             for harnesses / tests that don't build a full context.

        Either source being truthy is enough.
        """
        if self._parent_context is not None:
            flag = self._parent_context.metadata.get("is_teammate")
            if flag:
                return True
        return bool(request.metadata.get("is_teammate"))

    def _build_child_context(self) -> SubagentContext:
        """Build a fresh :class:`SubagentContext` for the fork path.

        Uses :func:`create_subagent_context` to clone the parent's
        file state, then rebuilds :class:`CacheSafeParams` with
        ``fork_depth`` incremented by 1 so the new context is
        distinguishable at the ``is_in_fork_child`` check.
        """
        assert self._parent_context is not None  # guarded by caller
        parent = self._parent_context
        # Clone file state + metadata first.
        cloned = create_subagent_context(
            parent_params=parent.params,
            parent_file_state=parent.file_state,
            parent_transcript_ref=parent.parent_transcript_ref,
            metadata=dict(parent.metadata),
        )
        # Rebuild params with depth + 1 (CacheSafeParams is frozen).
        new_params = CacheSafeParams(
            system_prompt=parent.params.system_prompt,
            tool_defs_hash=parent.params.tool_defs_hash,
            tool_names=parent.params.tool_names,
            betas=parent.params.betas,
            effort_value=parent.params.effort_value,
            parent_session_id=parent.params.parent_session_id,
            fork_depth=parent.params.fork_depth + 1,
        )
        cloned.params = new_params
        return cloned

    def _prepare_prompt(self, request: SpawnRequest) -> str:
        """Return the request prompt with coordinator preamble, if any.

        The snippet is prepended only when:
          - ``features.coordinator_mode()`` is ``True``, AND
          - the raw prompt does NOT already contain the literal
            marker ``COORDINATOR`` (case-sensitive).

        The marker check lets callers hand-craft a prompt that
        already addresses coordination without getting the boilerplate
        appended twice.
        """
        if not self._features.coordinator_mode():
            return request.prompt
        if "COORDINATOR" in request.prompt:
            return request.prompt
        return f"{COORDINATOR_PROMPT_SNIPPET}\n{request.prompt}"

    def _record_reject(self, cls: type[BaseException]) -> None:
        """Bump the per-exception-class reject counter."""
        name = cls.__name__
        self._rejects_by_class[name] = self._rejects_by_class.get(name, 0) + 1
