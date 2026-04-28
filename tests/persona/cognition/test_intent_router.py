"""Tests for ``concinno.persona.cognition.intent_router``.

Covers Module A starter contract per spec §2.2:

* ``classify`` signal detection (emotional / tool / command / trivial /
  question / memory trigger / length).
* Five priority routing paths (force_layer / tool_or_command / emotional
  / memory_trigger / trivial / default).
* ``execute_background`` async parallel dispatch + handler errors.
* ``build_conscious_context`` truncation + signal-line preservation.
* Frozen dataclass validation (rejects bad inputs).
* 5 NPC fixture archetypes route to expected layers.
* ZIQ outcome emission integrates with the bus (no exceptions).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from concinno.persona.cognition import (
    BackgroundTask,
    DispatchDecision,
    IntentRouteInput,
    IntentRouteOutput,
    IntentRouter,
    MessageSignals,
    ProcessingLayer,
)

# ---------------------------------------------------------------------------
# classify
# ---------------------------------------------------------------------------


class TestClassify:
    """``classify`` is pure, 0-cost regex — every test is a literal string."""

    def test_emotional_cue_detected(self) -> None:
        signals = IntentRouter().classify("I feel really anxious about tomorrow")
        assert signals.is_emotional is True

    def test_tool_cue_detected(self) -> None:
        signals = IntentRouter().classify("Search for the cheapest flight")
        assert signals.needs_tool is True

    def test_command_prefix_detected(self) -> None:
        signals = IntentRouter().classify("/reset memory")
        assert signals.is_command is True

    def test_question_detected(self) -> None:
        signals = IntentRouter().classify("What is the weather today?")
        assert signals.is_question is True

    def test_memory_trigger_detected(self) -> None:
        signals = IntentRouter().classify("Remember what I told you yesterday?")
        assert signals.has_memory_trigger is True

    def test_trivial_short_message(self) -> None:
        signals = IntentRouter().classify("ok")
        assert signals.is_trivial is True
        assert signals.char_length == 2

    def test_non_trivial_long_message(self) -> None:
        text = "I have been thinking about this problem for a while now"
        signals = IntentRouter().classify(text)
        assert signals.is_trivial is False
        assert signals.char_length == len(text)

    def test_empty_string_is_trivial(self) -> None:
        signals = IntentRouter().classify("")
        assert signals.is_trivial is True
        assert signals.char_length == 0


# ---------------------------------------------------------------------------
# Routing priority
# ---------------------------------------------------------------------------


class TestRoute:
    """Five priority paths from spec §2.2 Module A."""

    def _input(self, message: str, **kw: Any) -> IntentRouteInput:
        return IntentRouteInput(
            user_message=message,
            persona_id=kw.pop("persona_id", "alice"),
            **kw,
        )

    def test_force_layer_overrides_heuristic(self) -> None:
        out = IntentRouter().route(
            self._input(
                "Search for tomorrow's weather",
                force_layer=ProcessingLayer.BACKGROUND,
            )
        )
        assert out.decision.layer is ProcessingLayer.BACKGROUND
        assert out.decision.reason == "force_layer"

    def test_tool_routes_to_hybrid(self) -> None:
        out = IntentRouter().route(self._input("Calculate 17 * 23 quickly"))
        assert out.decision.layer is ProcessingLayer.HYBRID
        assert out.decision.reason == "tool_or_command"

    def test_command_routes_to_hybrid(self) -> None:
        out = IntentRouter().route(self._input("/help"))
        assert out.decision.layer is ProcessingLayer.HYBRID
        assert out.decision.reason == "tool_or_command"

    def test_emotional_routes_to_foreground(self) -> None:
        out = IntentRouter().route(
            self._input("I feel hurt by what you said earlier")
        )
        # Emotional + memory cue both fire; emotional priority wins.
        assert out.decision.layer is ProcessingLayer.FOREGROUND
        assert out.decision.reason == "emotional"

    def test_memory_trigger_routes_to_hybrid(self) -> None:
        out = IntentRouter().route(
            self._input("Recall what we discussed previously")
        )
        assert out.decision.layer is ProcessingLayer.HYBRID
        assert out.decision.reason == "memory_trigger"

    def test_trivial_routes_to_foreground(self) -> None:
        out = IntentRouter().route(self._input("ok"))
        assert out.decision.layer is ProcessingLayer.FOREGROUND
        assert out.decision.reason == "trivial"

    def test_default_routes_to_hybrid(self) -> None:
        out = IntentRouter().route(
            self._input("Tell me an interesting fact about octopuses")
        )
        assert out.decision.layer is ProcessingLayer.HYBRID
        assert out.decision.reason == "default"


# ---------------------------------------------------------------------------
# Output shape
# ---------------------------------------------------------------------------


class TestOutputShape:
    def test_route_returns_intent_route_output(self) -> None:
        out = IntentRouter().route(
            IntentRouteInput(user_message="hi", persona_id="alice")
        )
        assert isinstance(out, IntentRouteOutput)
        assert isinstance(out.decision, DispatchDecision)
        assert isinstance(out.foreground_task, str)
        assert isinstance(out.background_tasks, tuple)

    def test_foreground_label_for_emotional(self) -> None:
        out = IntentRouter().route(
            IntentRouteInput(
                user_message="I am really sad right now",
                persona_id="alice",
            )
        )
        assert out.foreground_task == "reply_empathic"

    def test_foreground_label_for_question(self) -> None:
        out = IntentRouter().route(
            IntentRouteInput(
                user_message="Tell me, why does the sky look blue today?",
                persona_id="alice",
            )
        )
        # "why" → question signal; layer = HYBRID default. Label = answer.
        assert out.foreground_task == "reply_answer"

    def test_foreground_label_for_trivial(self) -> None:
        out = IntentRouter().route(
            IntentRouteInput(user_message="ok", persona_id="alice")
        )
        assert out.foreground_task == "reply_brief"

    def test_foreground_label_for_tool(self) -> None:
        out = IntentRouter().route(
            IntentRouteInput(
                user_message="Search for the cheapest flight",
                persona_id="alice",
            )
        )
        assert out.foreground_task == "reply_with_tool"


# ---------------------------------------------------------------------------
# Background task derivation
# ---------------------------------------------------------------------------


class TestBackgroundTasks:
    def test_foreground_emits_affect_when_emotional(self) -> None:
        out = IntentRouter().route(
            IntentRouteInput(
                user_message="I am angry and sad", persona_id="alice"
            )
        )
        types = [t.type for t in out.background_tasks]
        assert "affect_update" in types

    def test_trivial_no_background(self) -> None:
        out = IntentRouter().route(
            IntentRouteInput(user_message="ok", persona_id="alice")
        )
        assert out.background_tasks == ()

    def test_memory_trigger_emits_recall(self) -> None:
        out = IntentRouter().route(
            IntentRouteInput(
                user_message="Recall the last time we spoke",
                persona_id="alice",
                turn_index=2,
            )
        )
        types = [t.type for t in out.background_tasks]
        assert "recall" in types
        assert "memory_consolidation" in types

    def test_first_turn_no_consolidation(self) -> None:
        out = IntentRouter().route(
            IntentRouteInput(
                user_message="Tell me something interesting",
                persona_id="alice",
                turn_index=0,
            )
        )
        types = [t.type for t in out.background_tasks]
        assert "memory_consolidation" not in types


# ---------------------------------------------------------------------------
# execute_background
# ---------------------------------------------------------------------------


class TestExecuteBackground:
    def test_empty_list_returns_empty_dict(self) -> None:
        result = asyncio.run(IntentRouter().execute_background([]))
        assert result == {}

    def test_sync_handler_runs(self) -> None:
        router = IntentRouter()

        def handler(task: BackgroundTask) -> dict[str, Any]:
            return {"handled": task.type}

        router.register_background_handler("memory_consolidation", handler)
        result = asyncio.run(
            router.execute_background(
                [BackgroundTask(type="memory_consolidation")]
            )
        )
        assert result == {"memory_consolidation": {"handled": "memory_consolidation"}}

    def test_async_handler_runs(self) -> None:
        router = IntentRouter()

        async def handler(task: BackgroundTask) -> str:
            await asyncio.sleep(0)
            return f"async-{task.type}"

        router.register_background_handler("recall", handler)
        result = asyncio.run(
            router.execute_background([BackgroundTask(type="recall")])
        )
        assert result == {"recall": "async-recall"}

    def test_unknown_task_yields_none(self) -> None:
        result = asyncio.run(
            IntentRouter().execute_background(
                [BackgroundTask(type="unknown_type")]
            )
        )
        assert result == {"unknown_type": None}

    def test_handler_exception_isolated(self) -> None:
        router = IntentRouter()

        def bad_handler(task: BackgroundTask) -> None:
            raise RuntimeError("boom")

        def good_handler(task: BackgroundTask) -> str:
            return "ok"

        router.register_background_handler("affect_update", bad_handler)
        router.register_background_handler("recall", good_handler)

        result = asyncio.run(
            router.execute_background(
                [
                    BackgroundTask(type="affect_update"),
                    BackgroundTask(type="recall"),
                ]
            )
        )
        assert "error" in result["affect_update"]
        assert result["recall"] == "ok"

    def test_priority_ordering(self) -> None:
        """Higher priority tasks are scheduled first (sort precedes gather)."""
        router = IntentRouter()
        order: list[str] = []

        def make_handler(label: str):  # type: ignore[no-untyped-def]
            def _handler(task: BackgroundTask) -> str:
                order.append(label)
                return label

            return _handler

        router.register_background_handler("recall", make_handler("recall"))
        router.register_background_handler(
            "affect_update", make_handler("affect")
        )
        asyncio.run(
            router.execute_background(
                [
                    BackgroundTask(type="affect_update", priority=1),
                    BackgroundTask(type="recall", priority=99),
                ]
            )
        )
        assert order[0] == "recall"


# ---------------------------------------------------------------------------
# build_conscious_context
# ---------------------------------------------------------------------------


class TestBuildContext:
    def test_empty_history_no_signals_returns_empty_string(self) -> None:
        signals = MessageSignals()
        brief = IntentRouter().build_conscious_context([], signals)
        assert brief == ""

    def test_signal_line_emitted_when_flags_present(self) -> None:
        signals = MessageSignals(is_emotional=True, is_question=True)
        brief = IntentRouter().build_conscious_context([], signals)
        assert "[signals:" in brief
        assert "emotional" in brief
        assert "question" in brief

    def test_recent_history_included(self) -> None:
        history = [
            {"role": "user", "content": "earlier user msg"},
            {"role": "assistant", "content": "earlier assistant msg"},
        ]
        brief = IntentRouter().build_conscious_context(
            history, MessageSignals()
        )
        assert "earlier user msg" in brief
        assert "earlier assistant msg" in brief

    def test_truncation_preserves_signal_line(self) -> None:
        long_msg = "x" * 5000
        history = [{"role": "user", "content": long_msg}]
        signals = MessageSignals(is_emotional=True)
        brief = IntentRouter().build_conscious_context(
            history, signals, max_chars=200
        )
        assert brief.startswith("[signals:")
        assert len(brief) <= 200


# ---------------------------------------------------------------------------
# Dataclass validation
# ---------------------------------------------------------------------------


class TestDataclassValidation:
    def test_intent_route_input_rejects_empty_persona(self) -> None:
        with pytest.raises(ValueError):
            IntentRouteInput(user_message="hi", persona_id="")

    def test_intent_route_input_rejects_negative_turn(self) -> None:
        with pytest.raises(ValueError):
            IntentRouteInput(
                user_message="hi", persona_id="alice", turn_index=-1
            )

    def test_intent_route_input_rejects_non_str_message(self) -> None:
        with pytest.raises(TypeError):
            IntentRouteInput(user_message=42, persona_id="alice")  # type: ignore[arg-type]

    def test_background_task_rejects_empty_type(self) -> None:
        with pytest.raises(ValueError):
            BackgroundTask(type="")

    def test_intent_route_input_is_frozen(self) -> None:
        inp = IntentRouteInput(user_message="hi", persona_id="alice")
        with pytest.raises(Exception):
            inp.user_message = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 5 NPC fixture archetypes
# ---------------------------------------------------------------------------


# Each fixture: (archetype, message, expected_layer, expected_reason).
NPC_FIXTURES: tuple[tuple[str, str, ProcessingLayer, str], ...] = (
    (
        "anxious_friend",
        "I feel worried about the test tomorrow",
        ProcessingLayer.FOREGROUND,
        "emotional",
    ),
    (
        "researcher",
        "Search for recent papers on graph attention networks",
        ProcessingLayer.HYBRID,
        "tool_or_command",
    ),
    (
        "casual_chatter",
        "ok",
        ProcessingLayer.FOREGROUND,
        "trivial",
    ),
    (
        "long_term_companion",
        "Remember the picnic we talked about last month?",
        ProcessingLayer.HYBRID,
        "memory_trigger",
    ),
    (
        "explorer",
        "Tell me an interesting fact about octopus cognition",
        ProcessingLayer.HYBRID,
        "default",
    ),
)


@pytest.mark.parametrize("archetype,message,expected_layer,expected_reason", NPC_FIXTURES)
def test_npc_fixture_routes_correctly(
    archetype: str,
    message: str,
    expected_layer: ProcessingLayer,
    expected_reason: str,
) -> None:
    out = IntentRouter().route(
        IntentRouteInput(user_message=message, persona_id=archetype)
    )
    assert out.decision.layer is expected_layer, (
        f"{archetype}: expected {expected_layer}, got {out.decision.layer}"
    )
    assert out.decision.reason == expected_reason, (
        f"{archetype}: expected reason {expected_reason!r}, "
        f"got {out.decision.reason!r}"
    )


# ---------------------------------------------------------------------------
# ZIQ outcome bus integration
# ---------------------------------------------------------------------------


class TestZIQIntegration:
    def test_route_emits_dispatch_outcome(self) -> None:
        from concinno.ziq_outcome_bus import Outcome, ZIQOutcomeBus, get_bus

        ZIQOutcomeBus._reset_for_testing()
        captured: list[Outcome] = []

        def listener(outcome: Outcome) -> None:
            captured.append(outcome)

        unsubscribe = get_bus().subscribe(
            "persona.intent_router.dispatch_layer", listener
        )
        try:
            IntentRouter().route(
                IntentRouteInput(
                    user_message="Tell me something new",
                    persona_id="alice",
                )
            )
        finally:
            unsubscribe()
            ZIQOutcomeBus._reset_for_testing()

        assert captured, "expected at least one outcome emitted"
        assert captured[0].tunable == "persona.intent_router.dispatch_layer"
        assert captured[0].metadata.get("reason") in {
            "default",
            "tool_or_command",
            "emotional",
            "memory_trigger",
            "trivial",
            "force_layer",
        }

    def test_emit_disabled_path_does_not_emit(self) -> None:
        from concinno.ziq_outcome_bus import Outcome, ZIQOutcomeBus, get_bus

        ZIQOutcomeBus._reset_for_testing()
        captured: list[Outcome] = []

        def listener(outcome: Outcome) -> None:
            captured.append(outcome)

        unsubscribe = get_bus().subscribe(
            "persona.intent_router.dispatch_layer", listener
        )
        try:
            IntentRouter(emit_outcomes=False).route(
                IntentRouteInput(
                    user_message="Tell me something new",
                    persona_id="alice",
                )
            )
        finally:
            unsubscribe()
            ZIQOutcomeBus._reset_for_testing()

        assert captured == []
