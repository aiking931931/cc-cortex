"""Multi-agent System (MAS) loop — generic 3-role orchestrator.

@module concinno.agent.mas_loop
@responsibility Provide a reusable sequential dispatcher for
    solver → critic → judge role stacks. Consumers inject the
    concrete callables (solver loop, critic/judge text-only LLM
    calls, SSE emitter) and the orchestrator drives the sequence,
    handles empty-cascade short-circuits, blind-labels solver/critic
    outputs before the judge sees them, truncates solver traces to
    a bounded top-k, and surfaces per-role records + audit mapping.

Why this lives in Concinno (per MEMORY #52 切點):
    The orchestration is benchmark-agnostic — any agent consumer
    (Sancio GAIA harness today, Perpetuo agent-of-agents tomorrow)
    can hand in its own role prompts via :mod:`concinno.agent.mas_prompts`
    defaults or its own overrides. The Sancio adapter in persona-api
    stays thin (~50 LOC) and only knows about GAIA-flavored wording.

Why blind labels (H1 in the design-phase red-team):
    Gemma-class models exhibit primacy bias — the first coherent
    candidate in context often wins regardless of merit. The judge
    must receive ``{"response_1": ..., "response_2": ...}`` where
    the (solver, critic) → (1, 2) mapping is drawn per-task from a
    seeded RNG keyed on ``task_id``. The mapping is surfaced back
    as ``audit_mapping`` so post-hoc analysis can un-blind.

Why empty-cascade short-circuit (H2 in the design-phase red-team):
    When the solver produces a blank final answer, feeding its
    trace to the critic re-triggers the same prompt-bloat failure
    mode that killed Phase 1 (MEMORY #89). Instead: when solver is
    blank we build the critic prompt from **the raw question only**
    and set ``solver_answer=""`` so the judge sees an honest cascade
    outcome. If the critic also blanks we skip the judge entirely
    — calling a text-only arbiter on two blanks is token waste.

@dependencies stdlib + pydantic (already a transitive dep via
    ``anthropic``; pinned as a direct dep in ``pyproject.toml``
    so concinno never installs without it).
@exports MASConfig, MASResult, run_mas, derive_role_seeds,
    truncate_trace_top_k, blind_label_order
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, field_validator

# ─────────────────────────── Config schema ───────────────────────────


class MASConfig(BaseModel):
    """Typed per-request MAS config (populated from ``mas_config`` field).

    ``extra="forbid"`` catches wire-contract drift on day 1 — a typo
    like ``{"rolez": [...]}`` or a stale field name raises during
    Pydantic validation rather than silently defaulting. This is the
    L1 fix for the red-team's H1 observation (``dict[str, Any]``
    bypasses ``extra="forbid"`` on the outer request model).

    ``vote="majority"`` is explicitly rejected with a :class:`ValueError`
    (M2 in the verdict). The ``majority`` branch is deferred to 0.5.0;
    shipping the Literal without enforcement would silently accept a
    value with no downstream implementation.
    """

    roles: list[str]
    """Execution order. Canonical today: ``["solver", "critic", "judge"]``.
    Order is preserved — ``run_mas`` dispatches in this sequence."""

    vote: Literal["judge"] = "judge"
    """Vote policy. 0.4.0 ships only ``"judge"`` (text-only tie-break).
    Pydantic's ``Literal`` does the heavy lifting — ``"majority"`` fails
    validation at the field level. The custom validator below adds a
    human-readable error message for the common mis-set case."""

    critic_model: str | None = None
    """Optional per-role model override (e.g. swap critic to a
    larger Gemma variant). Defaults to the outer request's model."""

    judge_model: str | None = None
    """Optional per-role judge model override. Same semantics."""

    seeds: dict[str, int] | None = None
    """Per-role sampler seeds. When ``None`` the caller should call
    :func:`derive_role_seeds` on the outer ``provider_extra.seed``
    to produce a deterministic offset triple. Surfaced here so a
    paranoid operator can pin all three explicitly."""

    model_config = {"extra": "forbid"}

    @field_validator("vote", mode="before")
    @classmethod
    def _reject_majority_explicitly(cls, value: Any) -> Any:
        """Return a human-friendly error for the ``"majority"`` case.

        Pydantic's built-in ``Literal["judge"]`` error reads
        ``Input should be 'judge'`` which is fine but doesn't signal
        the deferred-feature intent. This validator intercepts the
        common mistake first so API clients know *why* the value is
        refused.
        """
        if isinstance(value, str) and value.lower() == "majority":
            raise ValueError(
                "vote='majority' is deferred to concinno 0.5 / Sancio "
                "0.5. 0.4.0 accepts only vote='judge' — tie-break "
                "via a single text-only LLM arbiter."
            )
        return value

    @field_validator("roles")
    @classmethod
    def _roles_non_empty(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("roles must contain at least one role name")
        return value


# ─────────────────────────── Result record ───────────────────────────


@dataclass
class MASResult:
    """Aggregate output of :func:`run_mas`.

    ``per_role`` is a list of dicts shaped like
    ``{"role": "solver"|"critic"|"judge", "answer": str, "raw_len": int,
    "skipped": bool}``. Consumers layer additional telemetry
    (tokens, elapsed_s) via the ``emit`` callback rather than mutating
    this struct, so the core stays stable across call sites.

    ``skipped_judge`` is ``True`` when the empty-cascade short-circuit
    fires (both solver and critic blank). Consumers can attribute
    FAILs to this mode without parsing traces.

    ``audit_mapping`` records which blind label (response_1/response_2)
    mapped to solver vs critic for the judge call. Post-hoc swap-bias
    analysis needs this to un-blind.
    """

    final_answer: str
    per_role: list[dict[str, Any]] = field(default_factory=list)
    skipped_judge: bool = False
    cascade_empty: bool = False
    audit_mapping: dict[str, str] = field(default_factory=dict)


# ─────────────────────────── Helpers ───────────────────────────


def derive_role_seeds(solver_seed: int) -> dict[str, int]:
    """Derive deterministic per-role seeds from a single solver seed.

    Offsets are ``+0 / +1 / +2`` so a paired-seed McNemar analysis
    keyed on ``solver_seed=42`` still yields reproducible critic and
    judge sampling. M3 in the red-team: shared-seed sampling on
    identical input can produce sampling-identical output across
    roles, which short-circuits the adversarial pressure. Per-role
    offsets guarantee the RNG stream diverges.
    """
    return {
        "solver": int(solver_seed),
        "critic": int(solver_seed) + 1,
        "judge": int(solver_seed) + 2,
    }


def truncate_trace_top_k(tool_results: list[Any], k: int = 3) -> list[Any]:
    """Keep at most the first ``k`` tool-result records.

    H4 in the red-team: passing the full solver trace to the critic
    amplifies input tokens 2-3× rather than the design's quoted 1.4×.
    Truncating to ``k=3`` by original index keeps the highest-signal
    early tool results (typically the primary web_search + first
    follow-up) while bounding the critic's input.

    Tolerates ``len(tool_results) < k`` by returning the full list
    unchanged — callers don't need to guard the short-trace case.
    """
    if k <= 0:
        return []
    return list(tool_results[:k])


def blind_label_order(
    response_a: str,
    response_b: str,
    task_id: str,
) -> tuple[dict[str, str], dict[str, str]]:
    """Blind-shuffle ``(solver, critic)`` into ``(response_1, response_2)``.

    H1 in the red-team: baking ``solver_answer`` before ``critic_answer``
    into every judge prompt exploits Gemma-class primacy bias — the
    judge picks the first coherent candidate regardless of merit, and
    any measured lift is SAS + stylistic rewrite rather than real
    arbitration.

    Mitigation: per-task seeded RNG (seed derived from SHA-256 of
    ``task_id``) decides which of (a, b) gets mapped to response_1 vs
    response_2. The mapping is returned alongside the labelled dict so
    post-hoc analysis can un-blind for swap-test auditing.

    The RNG is deterministic on ``task_id`` alone — repeated calls for
    the same task always produce the same mapping. This lets the swap
    ablation ("flip the mapping on N=5") be implemented by the caller
    by perturbing ``task_id`` (e.g. appending ``"_swap"``) rather than
    by seeding through the MAS API.

    Parameters
    ----------
    response_a : str
        Canonically the solver's answer.
    response_b : str
        Canonically the critic's answer.
    task_id : str
        Opaque task identifier. Only its string content matters;
        the RNG ignores case / surrounding whitespace differences
        are significant (intentionally — you can deliberately
        perturb the ID to shuffle).

    Returns
    -------
    (labelled, audit_mapping) :
        ``labelled = {"response_1": ..., "response_2": ...}`` —
        exactly the shape the judge prompt template expects.
        ``audit_mapping = {"response_1": "solver"|"critic",
        "response_2": "solver"|"critic"}`` — the inverse lookup for
        un-blinding.
    """
    # Hash the task_id into a 64-bit seed so arbitrarily long /
    # structured task_ids don't skew the RNG.
    digest = hashlib.sha256(task_id.encode("utf-8")).digest()
    seed = int.from_bytes(digest[:8], "big", signed=False)
    rng = random.Random(seed)

    # Coin-flip which goes first. Deterministic given ``task_id``.
    swap = rng.random() < 0.5
    if swap:
        labelled = {"response_1": response_b, "response_2": response_a}
        audit_mapping = {"response_1": "critic", "response_2": "solver"}
    else:
        labelled = {"response_1": response_a, "response_2": response_b}
        audit_mapping = {"response_1": "solver", "response_2": "critic"}
    return labelled, audit_mapping


# ─────────────────────────── Orchestrator ───────────────────────────


# Type aliases for the callables the consumer injects. Kept narrow
# so the orchestrator doesn't accidentally couple to persona-api's
# ``AgentLoop`` / provider shape.

SolverResult = dict[str, Any]
"""Solver output contract: ``{"answer": str, "raw_len": int, "trace": list}``.

``trace`` is a list of tool_result records (shape opaque to this
module — the consumer picks what to include; :func:`truncate_trace_top_k`
just slices the list). Consumers typically populate this from the
SSE ``tool_result`` events collected during the solver loop.
"""

SolverCallable = Callable[[], Awaitable[SolverResult]]
TextOnlyCall = Callable[[str], Awaitable[str]]
EmitCallable = Callable[[str, dict[str, Any]], None]


def _format_or_passthrough(template: str, /, **slots: Any) -> str:
    """Format ``template`` with ``slots`` or return the template raw.

    Some callers may hand in a pre-formatted prompt (no ``{...}``
    slots). Using ``str.format_map`` with a defaulting mapping lets
    both shapes work without fragile try/except branches on
    ``KeyError``.
    """

    class _DefaultDict(dict[str, Any]):
        def __missing__(self, key: str) -> str:
            return "{" + key + "}"

    try:
        return template.format_map(_DefaultDict(**slots))
    except (ValueError, IndexError):
        # Brace-escape anomalies — return the raw template so the
        # caller sees their own bug rather than us silently mangling.
        return template


async def run_mas(
    *,
    config: MASConfig,
    solver_loop: SolverCallable,
    critic_call: TextOnlyCall,
    judge_call: TextOnlyCall,
    question: str,
    task_id: str,
    emit: EmitCallable,
    critic_prompt: str,
    critic_fallback_prompt: str,
    judge_prompt: str,
    trace_top_k: int = 3,
) -> MASResult:
    """Drive the 3-role MAS sequence.

    ``solver_loop`` is the only callable that may use tools
    (web_search, read_file, etc). ``critic_call`` / ``judge_call``
    are text-only; the adapter in persona-api wraps ``provider.generate``
    without tool schemas so the blast radius stays empty-set under
    ``SANCIO_DEFAULT_DECISION=ask``.

    Cascade semantics
    -----------------
    1. Solver runs (iterative, with tools). Its raw output is captured
       plus its tool-result trace.
    2. If solver answer is blank → critic is prompted **with the
       raw question only** (not the trace). This breaks the H2
       prompt-bloat feedback loop.
    3. Critic runs (single-shot text-only).
    4. If critic also blank → skip judge entirely, return the
       sentinel result. ``skipped_judge=True`` and
       ``cascade_empty=True``.
    5. Else → judge sees ``{question, response_1, response_2}``
       with blind-shuffled labels (H1 mitigation).
    6. Judge output is the ``final_answer``.

    Events emitted
    --------------
    ``role_start`` / ``role_end`` for each of the three roles (with
    ``{"role": ..., "answer": ..., "raw_len": ..., "skipped": bool}``
    in the end payload). ``vote`` once the judge commits.
    Consumers hook these into their SSE stream; the orchestrator
    emits via the injected callback so it stays transport-agnostic.
    """
    per_role: list[dict[str, Any]] = []
    audit_mapping: dict[str, str] = {}

    # ─── Solver ───
    emit("role_start", {"role": "solver"})
    solver = await solver_loop()
    solver_answer = str(solver.get("answer", "") or "")
    solver_trace = solver.get("trace", []) or []
    solver_raw_len = int(solver.get("raw_len", len(solver_answer)))

    solver_blank = not solver_answer.strip()
    per_role.append({
        "role": "solver",
        "answer": solver_answer,
        "raw_len": solver_raw_len,
        "skipped": False,
    })
    emit("role_end", {
        "role": "solver",
        "answer": solver_answer,
        "raw_len": solver_raw_len,
        "skipped": False,
    })

    # ─── Critic ───
    emit("role_start", {"role": "critic"})
    if solver_blank:
        # H2 fix: do NOT pass the solver trace when solver is blank.
        # The fallback prompt is positive-framed (no refusal echo
        # phrases) to avoid reproducing the MEMORY #89 regression
        # where Gemma echoes "solver produced no answer" back as its
        # own final answer.
        critic_input = _format_or_passthrough(
            critic_fallback_prompt,
            question=question,
        )
        # Also zero out the solver answer on the record so the judge
        # sees the cascade state honestly, not a pretend solver.
        solver_answer = ""
    else:
        truncated = truncate_trace_top_k(solver_trace, trace_top_k)
        critic_input = _format_or_passthrough(
            critic_prompt,
            question=question,
            solver_answer=solver_answer,
            solver_trace_summary="\n".join(
                str(item) for item in truncated
            ),
        )

    critic_raw = await critic_call(critic_input)
    critic_answer = str(critic_raw or "")
    critic_blank = not critic_answer.strip()
    per_role.append({
        "role": "critic",
        "answer": critic_answer,
        "raw_len": len(critic_answer),
        "skipped": False,
    })
    emit("role_end", {
        "role": "critic",
        "answer": critic_answer,
        "raw_len": len(critic_answer),
        "skipped": False,
    })

    # ─── Empty-cascade short-circuit ───
    if solver_blank and critic_blank:
        # Both upstream roles blank → no signal for the judge to
        # arbitrate on. Returning early saves tokens + surfaces the
        # cascade as a distinct failure mode.
        per_role.append({
            "role": "judge",
            "answer": "",
            "raw_len": 0,
            "skipped": True,
        })
        emit("role_end", {
            "role": "judge",
            "answer": "",
            "raw_len": 0,
            "skipped": True,
        })
        emit("vote", {
            "final_answer": "",
            "skipped_judge": True,
            "cascade_empty": True,
        })
        return MASResult(
            final_answer="",
            per_role=per_role,
            skipped_judge=True,
            cascade_empty=True,
            audit_mapping={},
        )

    # ─── Judge ───
    emit("role_start", {"role": "judge"})
    labelled, audit_mapping = blind_label_order(
        response_a=solver_answer,
        response_b=critic_answer,
        task_id=task_id,
    )
    judge_input = _format_or_passthrough(
        judge_prompt,
        question=question,
        response_1=labelled["response_1"],
        response_2=labelled["response_2"],
    )
    judge_raw = await judge_call(judge_input)
    judge_answer = str(judge_raw or "")
    per_role.append({
        "role": "judge",
        "answer": judge_answer,
        "raw_len": len(judge_answer),
        "skipped": False,
    })
    emit("role_end", {
        "role": "judge",
        "answer": judge_answer,
        "raw_len": len(judge_answer),
        "skipped": False,
    })

    emit("vote", {
        "final_answer": judge_answer,
        "skipped_judge": False,
        "cascade_empty": False,
        "audit_mapping": audit_mapping,
    })

    return MASResult(
        final_answer=judge_answer,
        per_role=per_role,
        skipped_judge=False,
        cascade_empty=False,
        audit_mapping=audit_mapping,
    )


__all__ = [
    "MASConfig",
    "MASResult",
    "blind_label_order",
    "derive_role_seeds",
    "run_mas",
    "truncate_trace_top_k",
]
