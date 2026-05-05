"""concinno.meta_skills.workflow — CBUA-aware DAG workflow engine.

@module meta_skills.workflow
@responsibility Run a DAG of workflow nodes with CBUA-style gating:

1. α_t confidence check per node (via ``classify_complexity`` —
   fall-through when unavailable).
2. Per-node retry with escalation — 2 consecutive fails raise an
   on-screen RAG prompt, 3 consecutive fails abort the whole run.
3. Original intent re-injection every ``intent_reinject_every`` nodes
   so scope creep self-corrects.

@dependencies stdlib ``graphlib.TopologicalSorter`` (hard),
    ``concinno.cognitive.router.classify_complexity`` (soft — graceful
    fallback if the cognitive package is trimmed),
    ``concinno.intent_anchor_guard._extract_intent`` (soft).
@exports CBUAWorkflowEngine, WorkflowNode, WorkflowResult,
    WorkflowAborted, NodeFailure

Why this lives in meta_skills (not tasks/ or a first-class engine):
it's an orchestrator that composes existing Concinno primitives. It
never mutates guard state, never writes to handoff files — it's a
pure execution overlay that dispatches to caller-supplied callables
while emitting structured logs.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from graphlib import CycleError, TopologicalSorter
from typing import Any

logger = logging.getLogger("concinno.meta_skills.workflow")


# ── Exceptions ───────────────────────────────────────────────────────


class WorkflowAborted(RuntimeError):
    """Raised when any node fails more than ``max_retries`` times."""

    def __init__(self, node: str, last_error: BaseException) -> None:
        self.node = node
        self.last_error = last_error
        super().__init__(f"workflow aborted at node {node!r}: {last_error}")


@dataclass(frozen=True)
class NodeFailure:
    """One structured failure record."""

    node: str
    attempt: int
    error_type: str
    error_message: str
    ts: float


# ── Node ─────────────────────────────────────────────────────────────


@dataclass
class WorkflowNode:
    """One DAG node.

    Attributes:
        name: Unique identifier. Used as dict key + topological key.
        action: Callable accepting ``(intent: str, prior: dict)`` and
            returning any JSON-safe result. ``prior`` contains results
            of already-completed dependencies keyed by their node name.
        depends_on: Names this node waits on.
        alpha_threshold: α_t (CBUA complexity score) at which this node
            will request ``needs_more_context`` instead of running.
            The engine records the request but still runs the node —
            the signal is advisory, not blocking. Blocking α would
            stall the DAG; advisory α lets the caller decide upstream.
    """

    name: str
    action: Callable[[str, dict[str, Any]], Any]
    depends_on: list[str] = field(default_factory=list)
    alpha_threshold: float = 0.3


@dataclass
class WorkflowResult:
    """Output of :meth:`CBUAWorkflowEngine.run`."""

    completed: list[str]
    results: dict[str, Any]
    failures: list[NodeFailure]
    low_alpha_nodes: list[str]
    aborted_at: str | None = None


# ── Engine ───────────────────────────────────────────────────────────


class CBUAWorkflowEngine:
    """DAG runner with CBUA α + retry + intent re-injection."""

    def __init__(
        self,
        graph: dict[str, WorkflowNode],
        *,
        intent_reinject_every: int = 5,
    ) -> None:
        if not graph:
            msg = "graph must be non-empty"
            raise ValueError(msg)
        for key, node in graph.items():
            if key != node.name:
                msg = f"graph key {key!r} must match node.name {node.name!r}"
                raise ValueError(msg)
            for dep in node.depends_on:
                if dep not in graph:
                    msg = f"node {key!r} depends on missing {dep!r}"
                    raise KeyError(msg)
        self._graph = graph
        self._reinject = max(1, int(intent_reinject_every))
        # Validate the DAG up front — a cycle at run() is confusing.
        self._topo = self._build_topology()

    def _build_topology(self) -> list[str]:
        ts = TopologicalSorter[str]()
        for name, node in self._graph.items():
            ts.add(name, *node.depends_on)
        try:
            return list(ts.static_order())
        except CycleError as exc:
            msg = f"workflow has a cycle: {exc}"
            raise ValueError(msg) from exc

    # ── Run ──────────────────────────────────────────────────────

    def run(
        self,
        intent: str,
        *,
        max_retries: int = 3,
    ) -> WorkflowResult:
        """Execute the DAG.

        Node semantics:

        - α_t < node.alpha_threshold → record ``low_alpha``, run anyway.
        - Node raises → retry up to ``max_retries``-1 more times.
          After ``max_retries-1`` total failures (i.e. 2 fails seen),
          log a RAG-prompt message; after ``max_retries`` (i.e. 3 fails
          seen) raise :class:`WorkflowAborted` and stop the DAG.
        - Successful nodes' return values flow into ``prior`` for
          downstream nodes.
        """
        if max_retries < 1:
            msg = "max_retries must be >= 1"
            raise ValueError(msg)

        alpha_t = _compute_alpha(intent)
        compact_intent = _compact_intent(intent)

        results: dict[str, Any] = {}
        completed: list[str] = []
        failures: list[NodeFailure] = []
        low_alpha: list[str] = []
        aborted_at: str | None = None
        processed = 0

        for name in self._topo:
            node = self._graph[name]
            if alpha_t < node.alpha_threshold:
                low_alpha.append(name)

            if processed and processed % self._reinject == 0:
                logger.info(
                    "workflow: intent re-inject @ node %s/%d -> %s",
                    name,
                    processed,
                    compact_intent,
                )

            prior: dict[str, Any] = {
                dep: results[dep] for dep in node.depends_on if dep in results
            }

            attempt = 0
            while True:
                attempt += 1
                try:
                    value = node.action(intent, prior)
                except Exception as exc:  # noqa: BLE001 - caller-supplied
                    failures.append(
                        NodeFailure(
                            node=name,
                            attempt=attempt,
                            error_type=type(exc).__name__,
                            error_message=str(exc),
                            ts=time.time(),
                        )
                    )
                    consecutive = sum(
                        1 for f in reversed(failures) if f.node == name
                    )
                    if consecutive >= 2 and consecutive < max_retries:
                        logger.warning(
                            "workflow: node %s failed %d times — "
                            "RAG prompt: review %s deps / context",
                            name,
                            consecutive,
                            name,
                        )
                    if consecutive >= max_retries:
                        aborted_at = name
                        logger.error(
                            "workflow: aborting — node %s failed "
                            "%d consecutive times",
                            name,
                            consecutive,
                        )
                        return WorkflowResult(
                            completed=completed,
                            results=results,
                            failures=failures,
                            low_alpha_nodes=low_alpha,
                            aborted_at=aborted_at,
                        )
                    # Retry.
                    continue
                # Success path.
                results[name] = value
                completed.append(name)
                processed += 1
                break

        return WorkflowResult(
            completed=completed,
            results=results,
            failures=failures,
            low_alpha_nodes=low_alpha,
            aborted_at=None,
        )


# ── Helpers ──────────────────────────────────────────────────────────


def _compute_alpha(intent: str) -> float:
    """Map ``classify_complexity`` domain → α_t float.

    Uses CBUA thresholds (rules/L1/cbua.md):
      simple       → 0.10
      complicated  → 0.35
      complex      → 0.70
      chaotic      → 0.95

    Fall back to 0.5 if the cognitive package is not importable (e.g.
    a trimmed install) so node α gating stays advisory rather than
    crashing.
    """
    try:
        from ..cognitive.router import ComplexityDomain, classify_complexity
    except Exception:  # noqa: BLE001 - soft dep
        return 0.5
    try:
        domain, _signals = classify_complexity(intent)
    except Exception:  # noqa: BLE001 - classify raises on weird input
        return 0.5
    if domain == ComplexityDomain.SIMPLE:
        return 0.10
    if domain == ComplexityDomain.COMPLICATED:
        return 0.35
    if domain == ComplexityDomain.COMPLEX:
        return 0.70
    if domain == ComplexityDomain.CHAOTIC:
        return 0.95
    return 0.5


def _compact_intent(intent: str) -> str:
    """Short intent summary for the re-inject log line.

    Prefers :func:`concinno.intent_anchor_guard._extract_intent` for
    consistency with the existing cognitive layer; falls back to
    first-200-chars.
    """
    try:
        from ..intent_anchor_guard import _extract_intent
    except Exception:  # noqa: BLE001 - soft dep
        return (intent or "")[:200]
    try:
        return _extract_intent(intent, max_len=200)
    except Exception:  # noqa: BLE001
        return (intent or "")[:200]
