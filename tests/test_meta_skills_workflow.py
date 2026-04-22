"""tests.test_meta_skills_workflow — CBUAWorkflowEngine unit tests.

Verifies:
  - Linear DAG runs in topological order
  - Diamond DAG passes prior results to downstream nodes
  - Retry succeeds if node works before max_retries
  - max_retries exceeded → aborted_at set, no raise, later nodes skipped
  - Cycle in graph raises at construction
  - Intent re-inject log fires every N nodes
  - Low-α nodes tracked but not blocked
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from concinno.meta_skills.workflow import (
    CBUAWorkflowEngine,
    WorkflowNode,
    WorkflowResult,
)


def _node(
    name: str,
    *,
    action: Any,
    depends_on: list[str] | None = None,
    alpha_threshold: float = 0.0,
) -> WorkflowNode:
    return WorkflowNode(
        name=name,
        action=action,
        depends_on=list(depends_on or []),
        alpha_threshold=alpha_threshold,
    )


def test_linear_dag_order() -> None:
    order: list[str] = []

    def record(tag: str):
        def _act(_intent: str, _prior: dict) -> str:
            order.append(tag)
            return tag
        return _act

    graph = {
        "a": _node("a", action=record("a")),
        "b": _node("b", action=record("b"), depends_on=["a"]),
        "c": _node("c", action=record("c"), depends_on=["b"]),
    }
    engine = CBUAWorkflowEngine(graph)
    result = engine.run("do a then b then c")
    assert isinstance(result, WorkflowResult)
    assert order == ["a", "b", "c"]
    assert result.completed == ["a", "b", "c"]
    assert result.aborted_at is None
    assert result.failures == []


def test_diamond_dag_passes_priors() -> None:
    def action_a(_intent: str, _prior: dict) -> int:
        return 1

    def action_b(_intent: str, prior: dict) -> int:
        return prior["a"] + 10

    def action_c(_intent: str, prior: dict) -> int:
        return prior["a"] + 100

    def action_d(_intent: str, prior: dict) -> int:
        return prior["b"] + prior["c"]

    graph = {
        "a": _node("a", action=action_a),
        "b": _node("b", action=action_b, depends_on=["a"]),
        "c": _node("c", action=action_c, depends_on=["a"]),
        "d": _node("d", action=action_d, depends_on=["b", "c"]),
    }
    result = CBUAWorkflowEngine(graph).run("diamond")
    assert result.results["d"] == 11 + 101


def test_retry_succeeds_before_limit() -> None:
    state = {"calls": 0}

    def flaky(_i: str, _p: dict) -> str:
        state["calls"] += 1
        if state["calls"] < 3:
            msg = f"fail#{state['calls']}"
            raise RuntimeError(msg)
        return "ok"

    graph = {"x": _node("x", action=flaky)}
    result = CBUAWorkflowEngine(graph).run("flaky", max_retries=3)
    assert result.aborted_at is None
    assert result.results["x"] == "ok"
    assert len(result.failures) == 2
    assert state["calls"] == 3


def test_max_retries_aborts_without_raising() -> None:
    def always_fail(_i: str, _p: dict) -> str:
        msg = "nope"
        raise RuntimeError(msg)

    graph = {
        "a": _node("a", action=always_fail),
        "b": _node(
            "b",
            action=lambda _i, _p: "never_runs",
            depends_on=["a"],
        ),
    }
    result = CBUAWorkflowEngine(graph).run("fail", max_retries=3)
    assert result.aborted_at == "a"
    assert "b" not in result.results
    assert len([f for f in result.failures if f.node == "a"]) == 3


def test_cycle_rejected_at_construction() -> None:
    graph = {
        "a": _node("a", action=lambda _i, _p: 1, depends_on=["b"]),
        "b": _node("b", action=lambda _i, _p: 2, depends_on=["a"]),
    }
    with pytest.raises(ValueError, match="cycle"):
        CBUAWorkflowEngine(graph)


def test_missing_dependency_rejected() -> None:
    graph = {
        "a": _node("a", action=lambda _i, _p: 1, depends_on=["does_not_exist"]),
    }
    with pytest.raises(KeyError, match="does_not_exist"):
        CBUAWorkflowEngine(graph)


def test_empty_graph_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        CBUAWorkflowEngine({})


def test_key_must_match_node_name() -> None:
    graph = {
        "wrong_key": _node("actual_name", action=lambda _i, _p: 1),
    }
    with pytest.raises(ValueError, match="match node.name"):
        CBUAWorkflowEngine(graph)


def test_intent_reinject_logs(caplog: pytest.LogCaptureFixture) -> None:
    nodes = {
        f"n{i}": _node(f"n{i}", action=lambda _i, _p, i=i: i)
        for i in range(6)
    }
    # Force linear order via chained deps.
    for i in range(1, 6):
        nodes[f"n{i}"].depends_on = [f"n{i - 1}"]
    engine = CBUAWorkflowEngine(nodes, intent_reinject_every=3)
    with caplog.at_level(logging.INFO, logger="concinno.meta_skills.workflow"):
        engine.run("test intent message")
    injected = [r for r in caplog.records if "intent re-inject" in r.getMessage()]
    # 6 completed nodes / every 3 → inject at n3 (processed==3 before n3).
    assert len(injected) >= 1


def test_low_alpha_nodes_recorded_not_blocked() -> None:
    # Simple query → α ≈ 0.10, so a node with threshold 0.5 triggers
    # the low-alpha record but still runs.
    def echo(_i: str, _p: dict) -> str:
        return "ran"

    graph = {
        "hard": _node("hard", action=echo, alpha_threshold=0.9),
    }
    result = CBUAWorkflowEngine(graph).run("read one file")  # simple
    assert "hard" in result.low_alpha_nodes
    assert result.results["hard"] == "ran"
    assert result.aborted_at is None


def test_max_retries_invalid() -> None:
    graph = {"a": _node("a", action=lambda _i, _p: 1)}
    engine = CBUAWorkflowEngine(graph)
    with pytest.raises(ValueError, match=">= 1"):
        engine.run("x", max_retries=0)
