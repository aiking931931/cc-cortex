"""Tests for ``concinno.evolution.gepa_adapter`` (HP5 W4 / 4.6.0).

Covers the API-shape-first ship of the GEPA optional-dep adapter:

* lazy import — adapter constructs without ``gepa`` installed.
* missing-extra error — ``EvolutionExtraNotInstalled`` carries the
  install hint and inherits ``ImportError``.
* probe — :func:`is_available` returns the spec-found state.
* validation — ``__init__`` rejects malformed inputs.
* outcome bus seam — :meth:`attach_outcome_bus` is wired but never
  required.
* upstream contract — :meth:`run` accepts dict-shaped *and*
  tuple-shaped frontiers and rejects everything else loudly.
* swallow contract — bus errors during emit do not abort a search.

The actual GEPA package is *not* required for any of these tests;
``gepa`` is patched in via ``sys.modules`` so the suite runs on the
zero-dep core install.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from typing import Any

import pytest

from concinno.evolution import (
    EvolutionExtraNotInstalled,
    GepaAdapter,
    is_available,
)

# ── fakes ────────────────────────────────────────────────


class _FakeGepaModule(types.ModuleType):
    """Stand-in for the real ``gepa`` package."""

    def __init__(
        self,
        result: Any = None,
        version: str = "0.1.1",
    ) -> None:
        super().__init__("gepa")
        # Real packages carry ``__spec__``; mimic it so
        # ``is_available()``'s fast-path treats the fake as a real
        # install instead of raising on missing spec.
        from importlib.machinery import ModuleSpec

        self.__spec__ = ModuleSpec(name="gepa", loader=None)
        self.__version__ = version
        self._result = result if result is not None else []
        self.last_call: dict[str, Any] | None = None

    def run(
        self,
        *,
        seed: str,
        evaluate: Any,
        reflect: Any,
        budget: int,
    ) -> Any:
        self.last_call = {
            "seed": seed,
            "evaluate": evaluate,
            "reflect": reflect,
            "budget": budget,
        }
        return self._result


@pytest.fixture
def fake_gepa(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[_FakeGepaModule]:
    """Inject a fake ``gepa`` module into ``sys.modules``."""
    mod = _FakeGepaModule()
    monkeypatch.setitem(sys.modules, "gepa", mod)
    # ``importlib.util.find_spec`` walks importers; sys.modules is
    # consulted first.
    yield mod


@pytest.fixture
def no_gepa(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure ``gepa`` cannot be imported in this test."""
    monkeypatch.setitem(sys.modules, "gepa", None)


# ── is_available ─────────────────────────────────────────


def test_is_available_when_module_present(
    fake_gepa: _FakeGepaModule,
) -> None:
    assert is_available() is True


def test_is_available_returns_bool() -> None:
    """``is_available`` must always return a bool — never raise.

    The real environment may or may not have gepa installed; this
    test asserts the *type* of the return so callers can rely on
    a boolean response either way.
    """
    out = is_available()
    assert isinstance(out, bool)


# ── EvolutionExtraNotInstalled ───────────────────────────


def test_extra_not_installed_inherits_import_error() -> None:
    err = EvolutionExtraNotInstalled()
    assert isinstance(err, ImportError)


def test_extra_not_installed_carries_install_hint() -> None:
    err = EvolutionExtraNotInstalled()
    assert "concinno[evolution]" in str(err)
    assert "gepa" in str(err)


def test_extra_not_installed_records_module_name() -> None:
    err = EvolutionExtraNotInstalled(missing_module="something-else")
    assert err.missing_module == "something-else"
    assert "something-else" in str(err)


# ── GepaAdapter validation ───────────────────────────────


def test_init_rejects_empty_seed() -> None:
    with pytest.raises(ValueError, match="seed_artefact"):
        GepaAdapter(seed_artefact="", reflection_llm=lambda s: s)


def test_init_rejects_non_callable_reflect() -> None:
    with pytest.raises(TypeError, match="reflection_llm"):
        GepaAdapter(seed_artefact="seed", reflection_llm="not callable")  # type: ignore[arg-type]


def test_init_rejects_non_positive_budget() -> None:
    with pytest.raises(ValueError, match="budget"):
        GepaAdapter(
            seed_artefact="seed", reflection_llm=lambda s: s, budget=0,
        )


def test_init_no_gepa_required() -> None:
    """Construction must NOT trigger an import of ``gepa``."""
    # Even if the module is unavailable, construction succeeds.
    GepaAdapter(seed_artefact="seed", reflection_llm=lambda s: s)


# ── GepaAdapter.run lazy import + raise ─────────────────


def test_run_raises_extra_not_installed_when_missing(
    no_gepa: None,
) -> None:
    adapter = GepaAdapter(
        seed_artefact="seed",
        reflection_llm=lambda s: s,
    )
    with pytest.raises(EvolutionExtraNotInstalled):
        adapter.run(evaluate=lambda x: 1.0)


def test_run_passes_args_to_gepa(
    fake_gepa: _FakeGepaModule,
) -> None:
    fake_gepa._result = [("seed", 1.0)]
    adapter = GepaAdapter(
        seed_artefact="seed",
        reflection_llm=lambda s: f"reflect:{s}",
        budget=7,
    )
    adapter.run(evaluate=lambda c: float(len(c)))
    assert fake_gepa.last_call is not None
    assert fake_gepa.last_call["seed"] == "seed"
    assert fake_gepa.last_call["budget"] == 7
    assert fake_gepa.last_call["reflect"]("hi") == "reflect:hi"


# ── frontier normalisation ──────────────────────────────


def test_run_accepts_tuple_frontier(
    fake_gepa: _FakeGepaModule,
) -> None:
    fake_gepa._result = [("a", 0.3), ("b", 0.9), ("c", 0.6)]
    adapter = GepaAdapter(
        seed_artefact="seed", reflection_llm=lambda s: s,
    )
    out = adapter.run(evaluate=lambda c: 0.0)
    # Sorted descending by score.
    assert out == [("b", 0.9), ("c", 0.6), ("a", 0.3)]


def test_run_accepts_dict_frontier(
    fake_gepa: _FakeGepaModule,
) -> None:
    fake_gepa._result = [
        {"candidate": "a", "score": 0.3},
        {"candidate": "b", "score": 0.9},
    ]
    adapter = GepaAdapter(
        seed_artefact="seed", reflection_llm=lambda s: s,
    )
    out = adapter.run(evaluate=lambda c: 0.0)
    assert out == [("b", 0.9), ("a", 0.3)]


def test_run_rejects_unknown_frontier_shape(
    fake_gepa: _FakeGepaModule,
) -> None:
    fake_gepa._result = [42, "string-only", None]
    adapter = GepaAdapter(
        seed_artefact="seed", reflection_llm=lambda s: s,
    )
    with pytest.raises(RuntimeError, match="frontier shape"):
        adapter.run(evaluate=lambda c: 0.0)


def test_run_rejects_dict_with_wrong_field_types(
    fake_gepa: _FakeGepaModule,
) -> None:
    fake_gepa._result = [{"candidate": 123, "score": "high"}]
    adapter = GepaAdapter(
        seed_artefact="seed", reflection_llm=lambda s: s,
    )
    with pytest.raises(RuntimeError, match=r"\(candidate:str, score:float\)"):
        adapter.run(evaluate=lambda c: 0.0)


def test_run_rejects_when_upstream_lacks_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a future ``gepa`` ships without ``run``, surface the
    version mismatch loudly rather than continue."""
    fake = types.ModuleType("gepa")
    fake.__version__ = "9.9.9"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gepa", fake)
    adapter = GepaAdapter(
        seed_artefact="seed", reflection_llm=lambda s: s,
    )
    with pytest.raises(RuntimeError, match="version mismatch"):
        adapter.run(evaluate=lambda c: 0.0)


# ── outcome bus seam ────────────────────────────────────


def test_attach_outcome_bus_rejects_non_callable() -> None:
    adapter = GepaAdapter(
        seed_artefact="seed", reflection_llm=lambda s: s,
    )
    with pytest.raises(TypeError):
        adapter.attach_outcome_bus("nope")  # type: ignore[arg-type]


def test_attach_outcome_bus_emits_per_candidate(
    fake_gepa: _FakeGepaModule,
) -> None:
    fake_gepa._result = [("a", 1.0), ("b", 0.5)]
    adapter = GepaAdapter(
        seed_artefact="seed", reflection_llm=lambda s: s,
    )
    seen: list[tuple[str, float]] = []
    adapter.attach_outcome_bus(lambda cid, score: seen.append((cid, score)))
    adapter.run(evaluate=lambda c: 0.0)
    # Order matches the *sorted* frontier.
    assert seen == [("a", 1.0), ("b", 0.5)]


def test_attach_outcome_bus_swallows_emit_errors(
    fake_gepa: _FakeGepaModule,
) -> None:
    """A flaky bus must not abort a paid-for search."""
    fake_gepa._result = [("a", 1.0)]
    adapter = GepaAdapter(
        seed_artefact="seed", reflection_llm=lambda s: s,
    )

    def boom(_cid: str, _score: float) -> None:
        raise RuntimeError("bus down")

    adapter.attach_outcome_bus(boom)
    # No raise — search returns the frontier untouched.
    out = adapter.run(evaluate=lambda c: 0.0)
    assert out == [("a", 1.0)]


# ── run argument validation ─────────────────────────────


def test_run_rejects_non_callable_evaluate(
    fake_gepa: _FakeGepaModule,
) -> None:
    adapter = GepaAdapter(
        seed_artefact="seed", reflection_llm=lambda s: s,
    )
    with pytest.raises(TypeError, match="evaluate"):
        adapter.run(evaluate="not callable")  # type: ignore[arg-type]
