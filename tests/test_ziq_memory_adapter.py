"""Tests for concinno.ziq_memory_adapter — A↔F lazy bridge.

Sub-agent K wave-2 (4.4.0).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from concinno.ziq_memory_adapter import (
    NOISE_FILTER_OUTCOME_NAME,
    is_memory_skills_available,
    register_memory_noise_filter,
)
from concinno.ziq_outcome_bus import Outcome, ZIQOutcomeBus, get_bus


@pytest.fixture(autouse=True)
def _isolated_bus(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pin_file = tmp_path / "ziq_pinned.json"
    monkeypatch.setenv("CONCINNO_ZIQ_PIN_FILE", str(pin_file))
    monkeypatch.delenv("CONCINNO_ZIQ_BUS_DISABLED", raising=False)
    monkeypatch.delenv("CONCINNO_ZIQ_BUS_MAX_HZ", raising=False)
    ZIQOutcomeBus._reset_for_testing()
    yield
    ZIQOutcomeBus._reset_for_testing()


def test_canonical_name_constant() -> None:
    """The adapter exposes the same string as the sub-package contract."""
    assert NOISE_FILTER_OUTCOME_NAME == "memory.noise_filter"


def test_register_returns_none_when_pkg_missing() -> None:
    """``concinno-skills-memory`` is not installed in CI — silent no-op."""
    # The sub-package is intentionally not a hard dep; expect None.
    if is_memory_skills_available():
        pytest.skip("concinno-skills-memory IS installed; skip the missing-dep test")
    result = register_memory_noise_filter()
    assert result is None
    # The bus has no subscriber for memory.noise_filter.
    assert get_bus().subscriber_count(NOISE_FILTER_OUTCOME_NAME) == 0


def test_register_with_callback_override_no_pkg() -> None:
    """Override path also hits the ImportError early-return when pkg absent."""
    if is_memory_skills_available():
        pytest.skip("concinno-skills-memory IS installed; skip the missing-dep test")

    def fake_callback(query: str, layer: int, rel: float) -> float:
        return 0.5

    # Even with override, register is gated on the import succeeding so it
    # can resolve the canonical name. Returns None when pkg is absent.
    result = register_memory_noise_filter(callback_override=fake_callback)
    assert result is None


def test_register_subscribes_with_stub_pkg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stub the sub-package import; verify subscription + dispatch."""
    import sys
    import types

    seen: list[tuple[str, int, float]] = []

    def stub_callback(query: str, fetched_layer: int, fetched_relevance: float) -> float:
        seen.append((query, fetched_layer, fetched_relevance))
        return 0.75

    stub_mod = types.ModuleType("concinno_skills_memory.ziq_outcome")
    stub_mod.NOISE_FILTER_OUTCOME_NAME = NOISE_FILTER_OUTCOME_NAME
    stub_mod.reference_noise_filter = stub_callback

    parent = types.ModuleType("concinno_skills_memory")
    monkeypatch.setitem(sys.modules, "concinno_skills_memory", parent)
    monkeypatch.setitem(
        sys.modules, "concinno_skills_memory.ziq_outcome", stub_mod
    )

    unsub = register_memory_noise_filter()
    assert unsub is not None
    assert get_bus().subscriber_count(NOISE_FILTER_OUTCOME_NAME) == 1

    # Emit; the wrapper should pull metadata fields and call the stub.
    get_bus().emit(
        Outcome(
            tunable=NOISE_FILTER_OUTCOME_NAME,
            value="probe",
            reward=0.0,
            metadata={
                "query": "what is X",
                "fetched_layer": 2,
                "fetched_relevance": 0.4,
            },
        )
    )
    assert seen == [("what is X", 2, 0.4)]

    # Unsubscribe is callable and idempotent.
    unsub()
    unsub()
    assert get_bus().subscriber_count(NOISE_FILTER_OUTCOME_NAME) == 0


def test_callback_override_with_stub_pkg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``callback_override`` replaces ``reference_noise_filter`` cleanly."""
    import sys
    import types

    stub_mod = types.ModuleType("concinno_skills_memory.ziq_outcome")
    stub_mod.NOISE_FILTER_OUTCOME_NAME = NOISE_FILTER_OUTCOME_NAME

    def reference(query: str, layer: int, rel: float) -> float:
        return 0.0

    stub_mod.reference_noise_filter = reference
    parent = types.ModuleType("concinno_skills_memory")
    monkeypatch.setitem(sys.modules, "concinno_skills_memory", parent)
    monkeypatch.setitem(
        sys.modules, "concinno_skills_memory.ziq_outcome", stub_mod
    )

    seen: list[tuple[str, int, float]] = []

    def my_override(query: str, layer: int, rel: float) -> float:
        seen.append((query, layer, rel))
        return 0.99

    unsub = register_memory_noise_filter(callback_override=my_override)
    assert unsub is not None

    get_bus().emit(
        Outcome(
            tunable=NOISE_FILTER_OUTCOME_NAME,
            value="probe",
            reward=0.0,
            metadata={
                "query": "abc",
                "fetched_layer": 1,
                "fetched_relevance": 0.9,
            },
        )
    )
    assert seen == [("abc", 1, 0.9)]
    unsub()
