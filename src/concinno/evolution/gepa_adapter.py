"""``concinno.evolution.gepa_adapter`` — thin seam to upstream GEPA.

GEPA (https://github.com/gepa-ai/gepa, MIT 0.1.1) is a Pareto-
efficient evolutionary search over textual artefacts (prompts,
code snippets, etc.) driven by LLM reflection. Concinno already
ships an FTRL on-line learner (``concinno.ziq_outcome_bus``) and
a Skill / SKILL.md artefact pipeline, so the integration is a
small adapter rather than a re-implementation.

Design
------

* **Lazy import** — ``import gepa`` happens inside method bodies,
  not at module import time, so ``concinno.evolution`` is safe to
  ``from concinno.evolution import GepaAdapter`` even when the
  ``[evolution]`` extra is not installed. The adapter only fails
  on first ``run`` / probe call.
* **Distinct exception** — :class:`EvolutionExtraNotInstalled`
  inherits ``ImportError`` for compatibility with callers that
  catch the bare ``ImportError``, but carries a tailored message
  pointing at ``pip install "concinno[evolution]"`` so first-time
  operators do not have to stack-trace the failure.
* **Outcome bus seam** — :meth:`GepaAdapter.attach_outcome_bus`
  takes a callable that maps a GEPA score back to ``ziq_outcome_bus``
  ``emit_*`` calls. Decoupled so the bus shape can evolve without
  touching the adapter's GEPA contract.
* **No upstream patches** — we never monkey-patch GEPA. Anything
  that needs upstream work goes via a PR to ``gepa-ai/gepa``.

This module is **API-shape first** (4.6.0 first publish). The
full search loop wiring is W4 carryover — once landed, the same
class signature stays.
"""

from __future__ import annotations

import importlib
import importlib.util
from collections.abc import Callable
from typing import Any

__all__ = [
    "EvolutionExtraNotInstalled",
    "GepaAdapter",
    "is_available",
]


_INSTALL_HINT: str = (
    'install with `pip install "concinno[evolution]"` '
    "(brings in upstream gepa>=0.1.1, MIT)"
)


class EvolutionExtraNotInstalled(ImportError):
    """Raised when the optional ``[evolution]`` extra is missing.

    Inherits :class:`ImportError` so callers that catch the bare
    error class still work, but carries a tailored message so the
    first-time user does not need to read the stack to know what
    to install.
    """

    def __init__(self, missing_module: str = "gepa") -> None:
        super().__init__(
            f"concinno.evolution requires the {missing_module!r} package; "
            f"{_INSTALL_HINT}",
        )
        self.missing_module = missing_module


def is_available() -> bool:
    """Cheap probe: is upstream ``gepa`` importable?

    Side-effect-free; safe to call from a CLI / GUI status surface
    without paying the cost of an actual GEPA search. Returns
    ``False`` (never raises) if the loader chain raises while
    looking for the module — for example, a stale ``sys.modules``
    entry without ``__spec__`` after a partial install.
    """
    import sys
    # Fast positive: real package already imported.
    mod = sys.modules.get("gepa")
    if mod is not None and getattr(mod, "__spec__", None) is not None:
        return True
    try:
        return importlib.util.find_spec("gepa") is not None
    except (ImportError, ValueError):
        return False


class GepaAdapter:
    """Wraps a GEPA evolutionary-search run for a concinno artefact.

    Lifecycle:

    1. Construct with the artefact text + the LLM callable that
       GEPA will use for its reflection step.
    2. Optionally :meth:`attach_outcome_bus` to forward the
       per-candidate score into the FTRL learner.
    3. Call :meth:`run` to start the search and receive the
       Pareto frontier.

    The adapter does **not** persist the frontier — that is the
    caller's job (e.g. write to ``~/.concinno/evolution_runs/<ts>/``).
    Keeping the I/O outside the adapter means tests can drive it
    with an in-memory artefact and no disk traffic.
    """

    def __init__(
        self,
        *,
        seed_artefact: str,
        reflection_llm: Callable[[str], str],
        budget: int = 10,
    ) -> None:
        """Build an adapter without importing GEPA.

        Args:
            seed_artefact: The starting prompt / Skill body to evolve.
            reflection_llm: A callable that takes a prompt and returns
                the LLM's reflection text. Concinno ships none — the
                operator wires their own (anthropic / openai / local
                gguf, all already in concinno extras).
            budget: Maximum evolution iterations. GEPA runs until the
                Pareto frontier stabilises or this cap is hit.
        """
        if not isinstance(seed_artefact, str) or not seed_artefact:
            raise ValueError("seed_artefact must be a non-empty string")
        if not callable(reflection_llm):
            raise TypeError("reflection_llm must be callable")
        if not isinstance(budget, int) or budget <= 0:
            raise ValueError("budget must be a positive int")

        self._seed_artefact = seed_artefact
        self._reflection_llm = reflection_llm
        self._budget = budget
        self._outcome_emitter: Callable[[str, float], None] | None = None

    def attach_outcome_bus(
        self,
        emit: Callable[[str, float], None],
    ) -> None:
        """Wire FTRL outcome emission for each candidate score.

        ``emit(candidate_id, score)`` is called once per evaluated
        candidate. The downstream learner is concinno-side
        (``ziq_outcome_bus``); this adapter never imports it
        directly so the two ship cycles stay decoupled.
        """
        if not callable(emit):
            raise TypeError("emit must be callable")
        self._outcome_emitter = emit

    @staticmethod
    def _import_gepa() -> Any:
        """Lazy import; raise the friendly exception when missing."""
        try:
            return importlib.import_module("gepa")
        except ImportError as exc:
            raise EvolutionExtraNotInstalled(missing_module="gepa") from exc

    def run(
        self,
        *,
        evaluate: Callable[[str], float],
    ) -> list[tuple[str, float]]:
        """Execute the GEPA search loop.

        Args:
            evaluate: Maps a candidate artefact to its scalar score.
                The caller owns this — concinno never imposes a
                metric. Higher = better, by GEPA convention.

        Returns:
            The Pareto frontier as ``[(candidate, score), ...]``,
            sorted descending by score.

        Raises:
            EvolutionExtraNotInstalled: when ``gepa`` is not
                installed.
        """
        if not callable(evaluate):
            raise TypeError("evaluate must be callable")
        gepa = self._import_gepa()
        # ``gepa.run`` is the upstream entry point per its 0.1.x
        # README. The contract here is intentionally narrow: any
        # change to the upstream signature bubbles up through this
        # adapter's tests rather than silently corrupting downstream
        # behaviour.
        if not hasattr(gepa, "run"):
            raise RuntimeError(
                "upstream gepa package does not expose ``run``; "
                "version mismatch — pin to gepa>=0.1.1, got "
                f"{getattr(gepa, '__version__', '<unknown>')!r}",
            )
        run_fn = gepa.run
        result = run_fn(
            seed=self._seed_artefact,
            evaluate=evaluate,
            reflect=self._reflection_llm,
            budget=self._budget,
        )
        frontier = self._normalise_frontier(result)
        if self._outcome_emitter is not None:
            for cid, score in frontier:
                try:
                    self._outcome_emitter(cid, score)
                except Exception:
                    # Bus errors must never escape — concinno's
                    # FTRL bus already swallows in best-effort
                    # mode, but we double-guard here so a flaky
                    # downstream cannot corrupt a paid-for search.
                    pass
        return frontier

    @staticmethod
    def _normalise_frontier(
        result: Any,
    ) -> list[tuple[str, float]]:
        """Coerce the upstream return value into ``list[(str,float)]``.

        GEPA 0.1.x returns either a list of dicts with ``candidate``
        and ``score`` keys or a list of 2-tuples; we accept either
        and bail with a clear error on anything else so a future
        upstream signature break is loud, not silent.
        """
        if not hasattr(result, "__iter__"):
            raise RuntimeError(
                "upstream gepa.run returned non-iterable; cannot "
                "extract Pareto frontier",
            )
        normalised: list[tuple[str, float]] = []
        for entry in result:
            if isinstance(entry, dict):
                cand = entry.get("candidate")
                score = entry.get("score")
                if not isinstance(cand, str) or not isinstance(
                    score, (int, float),
                ):
                    raise RuntimeError(
                        "gepa.run returned dict without "
                        "(candidate:str, score:float)",
                    )
                normalised.append((cand, float(score)))
            elif isinstance(entry, tuple) and len(entry) == 2:
                cand, score = entry
                if not isinstance(cand, str) or not isinstance(
                    score, (int, float),
                ):
                    raise RuntimeError(
                        "gepa.run returned tuple without "
                        "(str, float) shape",
                    )
                normalised.append((cand, float(score)))
            else:
                raise RuntimeError(
                    "gepa.run returned unknown frontier shape; "
                    "expected list[dict] or list[(str, float)]",
                )
        normalised.sort(key=lambda x: x[1], reverse=True)
        return normalised
