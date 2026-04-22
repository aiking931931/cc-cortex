"""Tests for :mod:`concinno.agent.mas_loop`."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from pydantic import ValidationError

from concinno.agent.mas_loop import (
    MASConfig,
    MASResult,
    blind_label_order,
    derive_role_seeds,
    run_mas,
    truncate_trace_top_k,
)
from concinno.agent.mas_prompts import (
    DEFAULT_CRITIC_FALLBACK_PROMPT,
    DEFAULT_CRITIC_PROMPT,
    DEFAULT_JUDGE_PROMPT,
)

# ─────────────────────────── MASConfig schema ───────────────────────────


class TestMASConfigSchema:
    """Pydantic schema behaviour under :class:`MASConfig`."""

    def test_minimal_valid_config(self) -> None:
        cfg = MASConfig(roles=["solver", "critic", "judge"])
        assert cfg.roles == ["solver", "critic", "judge"]
        assert cfg.vote == "judge"
        assert cfg.critic_model is None
        assert cfg.seeds is None

    def test_extra_forbid_rejects_unknown_keys(self) -> None:
        """``extra="forbid"`` catches wire-contract drift on day 1."""
        with pytest.raises(ValidationError) as exc:
            MASConfig(
                roles=["solver"],
                rolez=["fake"],  # type: ignore[call-arg]
            )
        # Pydantic 2 reports extra_forbidden in error payload
        errors = exc.value.errors()
        assert any("extra" in str(e.get("type", "")).lower() for e in errors)

    def test_vote_majority_rejected_with_friendly_message(self) -> None:
        """M2 in the verdict — ``majority`` is deferred to 0.5+."""
        with pytest.raises(ValidationError) as exc:
            MASConfig(roles=["solver"], vote="majority")  # type: ignore[arg-type]
        msg = str(exc.value)
        assert "majority" in msg.lower()

    def test_roles_empty_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MASConfig(roles=[])

    def test_vote_judge_accepted(self) -> None:
        cfg = MASConfig(roles=["solver"], vote="judge")
        assert cfg.vote == "judge"

    def test_per_role_seeds_carry_through(self) -> None:
        cfg = MASConfig(
            roles=["solver", "critic", "judge"],
            seeds={"solver": 42, "critic": 43, "judge": 44},
        )
        assert cfg.seeds == {"solver": 42, "critic": 43, "judge": 44}


# ─────────────────────────── Helpers ───────────────────────────


class TestDeriveRoleSeeds:
    def test_offsets_plus_zero_one_two(self) -> None:
        out = derive_role_seeds(42)
        assert out == {"solver": 42, "critic": 43, "judge": 44}

    def test_accepts_zero(self) -> None:
        assert derive_role_seeds(0) == {
            "solver": 0,
            "critic": 1,
            "judge": 2,
        }

    def test_negative_seed_passed_through(self) -> None:
        # Odd, but deterministic — no silent normalisation.
        assert derive_role_seeds(-7) == {
            "solver": -7,
            "critic": -6,
            "judge": -5,
        }

    def test_int_coercion_from_str_like_numeric(self) -> None:
        # ``int(<str>)`` covers cases where provider_extra carries a
        # numeric string from YAML / env var; prove we don't explode.
        assert derive_role_seeds(int("99")) == {
            "solver": 99,
            "critic": 100,
            "judge": 101,
        }


class TestTruncateTraceTopK:
    def test_keeps_first_k_records(self) -> None:
        items = [1, 2, 3, 4, 5]
        assert truncate_trace_top_k(items, k=3) == [1, 2, 3]

    def test_shorter_than_k_returns_full_list(self) -> None:
        assert truncate_trace_top_k([1, 2], k=5) == [1, 2]

    def test_default_k_is_three(self) -> None:
        assert truncate_trace_top_k([1, 2, 3, 4]) == [1, 2, 3]

    def test_empty_list(self) -> None:
        assert truncate_trace_top_k([], k=3) == []

    def test_k_zero_returns_empty(self) -> None:
        assert truncate_trace_top_k([1, 2, 3], k=0) == []

    def test_k_negative_returns_empty(self) -> None:
        assert truncate_trace_top_k([1, 2, 3], k=-1) == []

    def test_returns_new_list_not_original(self) -> None:
        original = [1, 2, 3, 4]
        out = truncate_trace_top_k(original, k=2)
        out.append(999)
        assert original == [1, 2, 3, 4]


class TestBlindLabelOrder:
    def test_deterministic_per_task_id(self) -> None:
        a = blind_label_order("solverA", "criticB", "task-123")
        b = blind_label_order("solverA", "criticB", "task-123")
        assert a == b

    def test_produces_both_orderings_across_task_ids(self) -> None:
        """Across many task_ids we see both solver-first and critic-first."""
        seen_solver_first = False
        seen_critic_first = False
        for i in range(200):
            _labelled, mapping = blind_label_order(
                "S", "C", f"task-{i}",
            )
            if mapping["response_1"] == "solver":
                seen_solver_first = True
            else:
                seen_critic_first = True
            if seen_solver_first and seen_critic_first:
                break
        assert seen_solver_first, "never mapped solver to response_1"
        assert seen_critic_first, "never mapped critic to response_1"

    def test_labelled_has_exactly_two_keys(self) -> None:
        labelled, _ = blind_label_order("a", "b", "tid")
        assert set(labelled.keys()) == {"response_1", "response_2"}

    def test_audit_mapping_inverts_cleanly(self) -> None:
        """Audit mapping allows un-blinding for swap tests."""
        labelled, mapping = blind_label_order("S-answer", "C-answer", "t")
        if mapping["response_1"] == "solver":
            assert labelled["response_1"] == "S-answer"
            assert labelled["response_2"] == "C-answer"
        else:
            assert labelled["response_1"] == "C-answer"
            assert labelled["response_2"] == "S-answer"

    def test_empty_task_id_still_produces_valid_mapping(self) -> None:
        labelled, mapping = blind_label_order("x", "y", "")
        assert set(labelled.keys()) == {"response_1", "response_2"}
        assert set(mapping.values()) == {"solver", "critic"}


# ─────────────────────────── run_mas orchestrator ───────────────────────────


def _make_emit_capture() -> tuple[list[tuple[str, dict[str, Any]]], Callable[..., None]]:
    """Build an emit hook that stashes events into a list for assertions."""
    events: list[tuple[str, dict[str, Any]]] = []

    def _emit(name: str, payload: dict[str, Any]) -> None:
        events.append((name, payload))

    return events, _emit


def _make_solver(
    answer: str, trace: list[Any] | None = None,
) -> Callable[[], Awaitable[dict[str, Any]]]:
    async def _run() -> dict[str, Any]:
        return {
            "answer": answer,
            "raw_len": len(answer),
            "trace": trace or [],
        }

    return _run


def _make_text_call(
    fixed: str,
) -> Callable[[str], Awaitable[str]]:
    async def _call(prompt: str) -> str:  # noqa: ARG001
        return fixed

    return _call


def _make_echo_call() -> Callable[[str], Awaitable[str]]:
    async def _call(prompt: str) -> str:
        return f"ECHO::{prompt[:30]}"

    return _call


class TestRunMASHappyPath:
    def test_three_role_dispatch_returns_judge_answer(self) -> None:
        events, emit = _make_emit_capture()
        cfg = MASConfig(roles=["solver", "critic", "judge"])
        result = asyncio.run(run_mas(
            config=cfg,
            solver_loop=_make_solver("solver says 4"),
            critic_call=_make_text_call("critic says 3"),
            judge_call=_make_text_call("FINAL ANSWER: 3"),
            question="riddle: 3 or 4?",
            task_id="task-happy",
            emit=emit,
            critic_prompt=DEFAULT_CRITIC_PROMPT,
            critic_fallback_prompt=DEFAULT_CRITIC_FALLBACK_PROMPT,
            judge_prompt=DEFAULT_JUDGE_PROMPT,
        ))
        assert isinstance(result, MASResult)
        assert result.final_answer == "FINAL ANSWER: 3"
        assert result.skipped_judge is False
        assert result.cascade_empty is False
        roles = [r["role"] for r in result.per_role]
        assert roles == ["solver", "critic", "judge"]
        # Audit mapping surfaced for un-blinding.
        assert set(result.audit_mapping.keys()) == {
            "response_1", "response_2",
        }

    def test_emits_role_start_role_end_and_vote(self) -> None:
        events, emit = _make_emit_capture()
        cfg = MASConfig(roles=["solver", "critic", "judge"])
        asyncio.run(run_mas(
            config=cfg,
            solver_loop=_make_solver("solver out"),
            critic_call=_make_text_call("critic out"),
            judge_call=_make_text_call("judge out"),
            question="q",
            task_id="task-events",
            emit=emit,
            critic_prompt=DEFAULT_CRITIC_PROMPT,
            critic_fallback_prompt=DEFAULT_CRITIC_FALLBACK_PROMPT,
            judge_prompt=DEFAULT_JUDGE_PROMPT,
        ))
        names = [n for n, _ in events]
        # Three role_start + three role_end + one vote.
        assert names.count("role_start") == 3
        assert names.count("role_end") == 3
        assert names.count("vote") == 1
        # Ordering: solver role_start first, vote last.
        assert names[0] == "role_start"
        assert names[-1] == "vote"


class TestRunMASEmptyCascade:
    def test_solver_blank_triggers_fallback_prompt_to_critic(self) -> None:
        seen_prompts: list[str] = []

        async def _critic(prompt: str) -> str:
            seen_prompts.append(prompt)
            return "critic best guess"

        events, emit = _make_emit_capture()
        cfg = MASConfig(roles=["solver", "critic", "judge"])
        result = asyncio.run(run_mas(
            config=cfg,
            solver_loop=_make_solver("", trace=["big trace data"]),
            critic_call=_critic,
            judge_call=_make_text_call("FINAL ANSWER: guess"),
            question="the question body",
            task_id="task-empty-solver",
            emit=emit,
            critic_prompt=DEFAULT_CRITIC_PROMPT,
            critic_fallback_prompt=DEFAULT_CRITIC_FALLBACK_PROMPT,
            judge_prompt=DEFAULT_JUDGE_PROMPT,
        ))
        assert result.skipped_judge is False  # critic rescued
        assert result.final_answer == "FINAL ANSWER: guess"
        # Solver answer cleared on the record to reflect honest state.
        solver_rec = next(r for r in result.per_role if r["role"] == "solver")
        assert solver_rec["answer"] == ""
        # Critic did NOT receive the solver trace (H2 fix).
        assert len(seen_prompts) == 1
        assert "big trace data" not in seen_prompts[0]
        # But it DID receive the question.
        assert "the question body" in seen_prompts[0]

    def test_solver_and_critic_blank_skips_judge(self) -> None:
        judge_called = {"count": 0}

        async def _judge(prompt: str) -> str:
            judge_called["count"] += 1
            return "should not run"

        events, emit = _make_emit_capture()
        cfg = MASConfig(roles=["solver", "critic", "judge"])
        result = asyncio.run(run_mas(
            config=cfg,
            solver_loop=_make_solver(""),
            critic_call=_make_text_call(""),
            judge_call=_judge,
            question="q",
            task_id="task-cascade",
            emit=emit,
            critic_prompt=DEFAULT_CRITIC_PROMPT,
            critic_fallback_prompt=DEFAULT_CRITIC_FALLBACK_PROMPT,
            judge_prompt=DEFAULT_JUDGE_PROMPT,
        ))
        assert judge_called["count"] == 0
        assert result.skipped_judge is True
        assert result.cascade_empty is True
        assert result.final_answer == ""
        # Judge record still present but ``skipped=True``.
        judge_rec = next(r for r in result.per_role if r["role"] == "judge")
        assert judge_rec["skipped"] is True
        # vote event carries cascade signals.
        vote_events = [p for n, p in events if n == "vote"]
        assert len(vote_events) == 1
        assert vote_events[0]["skipped_judge"] is True
        assert vote_events[0]["cascade_empty"] is True

    def test_solver_whitespace_only_treated_as_blank(self) -> None:
        events, emit = _make_emit_capture()
        cfg = MASConfig(roles=["solver", "critic", "judge"])
        seen_prompts: list[str] = []

        async def _critic(prompt: str) -> str:
            seen_prompts.append(prompt)
            return "c"

        asyncio.run(run_mas(
            config=cfg,
            solver_loop=_make_solver("   \n\t  "),
            critic_call=_critic,
            judge_call=_make_text_call("j"),
            question="q",
            task_id="t",
            emit=emit,
            critic_prompt=DEFAULT_CRITIC_PROMPT,
            critic_fallback_prompt=DEFAULT_CRITIC_FALLBACK_PROMPT,
            judge_prompt=DEFAULT_JUDGE_PROMPT,
        ))
        # Fallback prompt fired — critic saw question-only template.
        assert len(seen_prompts) == 1
        assert "Solver's final answer" not in seen_prompts[0]


class TestRunMASTraceTruncation:
    def test_critic_gets_truncated_trace_only(self) -> None:
        seen: list[str] = []

        async def _critic(prompt: str) -> str:
            seen.append(prompt)
            return "c"

        events, emit = _make_emit_capture()
        cfg = MASConfig(roles=["solver", "critic", "judge"])
        trace = [
            "TR-ITEM-1-keep",
            "TR-ITEM-2-keep",
            "TR-ITEM-3-keep",
            "TR-ITEM-4-DROP",
            "TR-ITEM-5-DROP",
        ]
        asyncio.run(run_mas(
            config=cfg,
            solver_loop=_make_solver("solver A", trace=trace),
            critic_call=_critic,
            judge_call=_make_text_call("j"),
            question="q",
            task_id="t",
            emit=emit,
            critic_prompt=DEFAULT_CRITIC_PROMPT,
            critic_fallback_prompt=DEFAULT_CRITIC_FALLBACK_PROMPT,
            judge_prompt=DEFAULT_JUDGE_PROMPT,
            trace_top_k=3,
        ))
        assert "TR-ITEM-1-keep" in seen[0]
        assert "TR-ITEM-3-keep" in seen[0]
        assert "TR-ITEM-4-DROP" not in seen[0]
        assert "TR-ITEM-5-DROP" not in seen[0]


class TestRunMASBlindOrdering:
    def test_judge_receives_response_1_and_response_2_labels(self) -> None:
        seen_prompts: list[str] = []

        async def _judge(prompt: str) -> str:
            seen_prompts.append(prompt)
            return "verdict"

        events, emit = _make_emit_capture()
        cfg = MASConfig(roles=["solver", "critic", "judge"])
        asyncio.run(run_mas(
            config=cfg,
            solver_loop=_make_solver("ALPHA_TOKEN"),
            critic_call=_make_text_call("BETA_TOKEN"),
            judge_call=_judge,
            question="q",
            task_id="consistency-t",
            emit=emit,
            critic_prompt=DEFAULT_CRITIC_PROMPT,
            critic_fallback_prompt=DEFAULT_CRITIC_FALLBACK_PROMPT,
            judge_prompt=DEFAULT_JUDGE_PROMPT,
        ))
        prompt = seen_prompts[0]
        # Labels never leak solver/critic identity to the judge.
        # The prompt template itself may discuss a "3-role" stack in
        # the system role context — we only assert that the answers
        # are presented as Response 1 / Response 2 with no role hint
        # inline with the candidate values themselves.
        assert "Response 1:" in prompt
        assert "Response 2:" in prompt
        # Both answers landed in the prompt.
        assert "ALPHA_TOKEN" in prompt
        assert "BETA_TOKEN" in prompt


# ─────────────────────────── asymmetry_plan kwarg ───────────────────────────


class TestRunMASWithAsymmetryPlan:
    """Tier 1 ``asymmetry_plan`` kwarg: backward-compat + frame injection."""

    def test_none_asymmetry_plan_is_default(self) -> None:
        """``asymmetry_plan=None`` (the default) reproduces 0.4.x behaviour.

        Byte-identical prompt — no frame tag injected.
        """
        seen_critic: list[str] = []

        async def _critic(prompt: str) -> str:
            seen_critic.append(prompt)
            return "critic out"

        cfg = MASConfig(roles=["solver", "critic", "judge"])
        _, emit = _make_emit_capture()
        asyncio.run(run_mas(
            config=cfg,
            solver_loop=_make_solver("solver out"),
            critic_call=_critic,
            judge_call=_make_text_call("FINAL ANSWER: ok"),
            question="q",
            task_id="plan-none",
            emit=emit,
            critic_prompt=DEFAULT_CRITIC_PROMPT,
            critic_fallback_prompt=DEFAULT_CRITIC_FALLBACK_PROMPT,
            judge_prompt=DEFAULT_JUDGE_PROMPT,
        ))
        assert seen_critic
        # The default critic prompt does NOT start with ``[frame:``.
        assert not seen_critic[0].startswith("[frame:")

    def test_asymmetry_plan_injects_critic_frame(self) -> None:
        from concinno.agent.asymmetry import AsymmetryPlan, EpistemicAxis

        seen_critic: list[str] = []

        async def _critic(prompt: str) -> str:
            seen_critic.append(prompt)
            return "critic out"

        cfg = MASConfig(roles=["solver", "critic", "judge"])
        _, emit = _make_emit_capture()
        plan = AsymmetryPlan.build(
            axes_enabled={EpistemicAxis.PROMPT_FRAME},
        )
        asyncio.run(run_mas(
            config=cfg,
            solver_loop=_make_solver("solver out"),
            critic_call=_critic,
            judge_call=_make_text_call("FINAL ANSWER: ok"),
            question="q",
            task_id="plan-frame",
            emit=emit,
            critic_prompt=DEFAULT_CRITIC_PROMPT,
            critic_fallback_prompt=DEFAULT_CRITIC_FALLBACK_PROMPT,
            judge_prompt=DEFAULT_JUDGE_PROMPT,
            asymmetry_plan=plan,
        ))
        assert seen_critic
        assert seen_critic[0].startswith("[frame: critic-challenger]")

    def test_asymmetry_plan_injects_judge_frame(self) -> None:
        from concinno.agent.asymmetry import AsymmetryPlan, EpistemicAxis

        seen_judge: list[str] = []

        async def _judge(prompt: str) -> str:
            seen_judge.append(prompt)
            return "FINAL ANSWER: ok"

        cfg = MASConfig(roles=["solver", "critic", "judge"])
        _, emit = _make_emit_capture()
        plan = AsymmetryPlan.build(
            axes_enabled={EpistemicAxis.PROMPT_FRAME},
        )
        asyncio.run(run_mas(
            config=cfg,
            solver_loop=_make_solver("solver out"),
            critic_call=_make_text_call("critic out"),
            judge_call=_judge,
            question="q",
            task_id="plan-judge",
            emit=_make_emit_capture()[1],
            critic_prompt=DEFAULT_CRITIC_PROMPT,
            critic_fallback_prompt=DEFAULT_CRITIC_FALLBACK_PROMPT,
            judge_prompt=DEFAULT_JUDGE_PROMPT,
            asymmetry_plan=plan,
        ))
        assert seen_judge
        assert seen_judge[0].startswith("[frame: judge-arbiter]")

    def test_asymmetry_plan_empty_frames_passthrough(self) -> None:
        """Plan with ``axes_enabled=set()`` gives empty frames → no injection."""
        from concinno.agent.asymmetry import AsymmetryPlan

        seen_critic: list[str] = []

        async def _critic(prompt: str) -> str:
            seen_critic.append(prompt)
            return "critic out"

        cfg = MASConfig(roles=["solver", "critic", "judge"])
        _, emit = _make_emit_capture()
        plan = AsymmetryPlan.build(axes_enabled=set())
        asyncio.run(run_mas(
            config=cfg,
            solver_loop=_make_solver("solver out"),
            critic_call=_critic,
            judge_call=_make_text_call("FINAL ANSWER: ok"),
            question="q",
            task_id="plan-empty",
            emit=emit,
            critic_prompt=DEFAULT_CRITIC_PROMPT,
            critic_fallback_prompt=DEFAULT_CRITIC_FALLBACK_PROMPT,
            judge_prompt=DEFAULT_JUDGE_PROMPT,
            asymmetry_plan=plan,
        ))
        assert seen_critic
        # Solver frame is "solver-primary" default, critic gets same when
        # PROMPT_FRAME axis disabled, so both roles match → not treated as
        # "challenger" specifically. Frame should still be injected
        # (non-empty) but with the solver-primary tag.
        assert seen_critic[0].startswith("[frame: solver-primary]")

    def test_asymmetry_plan_applied_to_fallback_prompt(self) -> None:
        """Frame injection covers both ``critic_prompt`` and fallback."""
        from concinno.agent.asymmetry import AsymmetryPlan, EpistemicAxis

        seen_critic: list[str] = []

        async def _critic(prompt: str) -> str:
            seen_critic.append(prompt)
            return "c"

        cfg = MASConfig(roles=["solver", "critic", "judge"])
        _, emit = _make_emit_capture()
        plan = AsymmetryPlan.build(
            axes_enabled={EpistemicAxis.PROMPT_FRAME},
        )
        # Solver blank triggers fallback prompt branch.
        asyncio.run(run_mas(
            config=cfg,
            solver_loop=_make_solver(""),
            critic_call=_critic,
            judge_call=_make_text_call("FINAL ANSWER: ok"),
            question="q",
            task_id="plan-fallback",
            emit=emit,
            critic_prompt=DEFAULT_CRITIC_PROMPT,
            critic_fallback_prompt=DEFAULT_CRITIC_FALLBACK_PROMPT,
            judge_prompt=DEFAULT_JUDGE_PROMPT,
            asymmetry_plan=plan,
        ))
        assert seen_critic
        assert seen_critic[0].startswith("[frame: critic-challenger]")
